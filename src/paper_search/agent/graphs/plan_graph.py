"""Plan Graph — 主 Agent 的 LangGraph Plan-then-Execute 状态图。

节点（7个）:
  1. parse_intent    → LLM 深度解析用户需求
  2. clarify         → LLM 生成澄清问题
  3. await_clarify   → interrupt() 暂停，等待用户回答
  4. generate_plan   → LLM 生成结构化执行方案
  5. await_approval  → interrupt() 暂停，等待用户确认
  6. execute_plan    → 遍历 plan.steps，调用 ToolRegistry 工具
  7. overall_evaluate → LLM 评估执行结果

边（含条件路由）:
  START → parse_intent
    → needs_clarify? {yes→clarify, no→generate_plan}
  clarify → await_clarify → generate_plan
  generate_plan → await_approval
    → user_approved? {yes→execute_plan, no→END}
  execute_plan → overall_evaluate
    → decide_overall? {satisfied→END, adjust→generate_plan}

Thread ID: {agent_id}-{session_id}
Checkpoint: SqliteSaver (langgraph-checkpoint-sqlite)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 自定义 add_messages reducer（LangGraph 需要 Annotated）
# ═══════════════════════════════════════════════════════════════

def _add_messages(left: list, right: list) -> list:
    """合并消息列表——LangGraph 用于 Annotated 累加器。"""
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class MainAgentState(TypedDict):
    """主 Agent Plan Graph 的全局 State。

    所有字段按 LangGraph StateGraph 语义自动持久化到 SqliteSaver checkpoint。
    """
    messages: Annotated[list, _add_messages]   # 累加式对话消息
    session_id: str
    agent_id: str
    plan: Optional[dict]                       # GeneratedPlan (JSON)
    plan_status: str                           # "pending" | "awaiting_clarify" | "awaiting_approval" | "executing" | "done" | "needs_adjustment"
    pending_review: Optional[dict]             # 等待用户的消息（review envelope）
    user_approval: Optional[dict]              # 用户审批回复
    user_clarification: Optional[dict]         # 用户澄清回复
    evaluate_assessment: Optional[str]         # "satisfied" | "adjust"
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# Plan Graph
# ═══════════════════════════════════════════════════════════════


class PlanGraph:
    """主 Agent 的 Plan-then-Execute 状态图。

    用法:
        from paper_search.agent.graphs.plan_graph import PlanGraph

        pg = PlanGraph(llm=llm_client, tools=registry, memory=memory_manager, db=agent_db)
        graph = pg.compile(checkpointer=AsyncSqliteSaver(conn=db_path))

        # 首次消息
        async for event in graph.astream_events(input, config=thread_config):
            ...
        # 恢复暂停的图（回答澄清或审批）
        await graph.aresume(thread_config, {"user_clarification": {...}})
    """

    def __init__(self, llm, tools, memory, db, task_adapter=None):
        """
        Args:
            llm: LLMClientV2 实例
            tools: ToolRegistry 实例
            memory: MemoryManager 实例
            db: AgentDB 实例
            task_adapter: TaskEventAdapter 实例（可选，用于推送 task WS 消息）
        """
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._db = db
        self._task_adapter = task_adapter
        self._graph = None

    def compile(self, checkpointer: Optional[SqliteSaver] = None) -> StateGraph:
        """编译 StateGraph。

        Args:
            checkpointer: SqliteSaver 实例。若为 None 则使用内存 checkpoint。

        Returns:
            编译好的 StateGraph（支持 astream_events / ainvoke / aresume）
        """
        builder = StateGraph(MainAgentState)

        # 添加节点
        builder.add_node("parse_intent", self._parse_intent)
        builder.add_node("clarify", self._clarify)
        builder.add_node("await_clarify", self._await_clarify)
        builder.add_node("generate_plan", self._generate_plan)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("execute_plan", self._execute_plan)
        builder.add_node("overall_evaluate", self._overall_evaluate)

        # 边
        builder.add_edge(START, "parse_intent")
        builder.add_conditional_edges(
            "parse_intent", self._needs_clarify,
            {"yes": "clarify", "no": "generate_plan"},
        )
        builder.add_edge("clarify", "await_clarify")
        builder.add_edge("await_clarify", "generate_plan")
        builder.add_edge("generate_plan", "await_approval")
        builder.add_conditional_edges(
            "await_approval", self._user_approved,
            {"yes": "execute_plan", "no": END},
        )
        builder.add_edge("execute_plan", "overall_evaluate")
        builder.add_conditional_edges(
            "overall_evaluate", self._decide_overall,
            {"satisfied": END, "adjust": "generate_plan"},
        )

        if checkpointer is None:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        """获取编译好的图（必须先调用 compile）。"""
        if self._graph is None:
            raise RuntimeError("PlanGraph not compiled — call compile() first")
        return self._graph

    # ══════════════════════════════════════════════════════════
    # 节点实现
    # ══════════════════════════════════════════════════════════

    async def _parse_intent(self, state: MainAgentState) -> dict:
        """Stage 1: LLM 深度解析用户意图。"""
        messages = state.get("messages", [])
        if not messages:
            return {"plan_status": "pending", "error": "No messages in state"}

        user_msg = messages[-1].get("content", "") if isinstance(messages[-1], dict) else str(messages[-1])
        logger.info(f"parse_intent: {user_msg[:100]}...")

        # 使用 PromptOptimizer Stage 1 解析
        try:
            from ..prompt_optimizer import PromptOptimizer
            opt = PromptOptimizer(self._llm)
            parsed = await opt.parse(user_msg)

            # 将解析结果加入短期记忆
            self._memory.short_term.add_message("user", user_msg)

            return {
                "plan_status": ("awaiting_clarify" if parsed.missing_info or parsed.ambiguities else "pending"),
                "plan": {
                    "domain": parsed.domain,
                    "sub_domains": parsed.sub_domains,
                    "ambiguities": parsed.ambiguities,
                    "missing_info": parsed.missing_info,
                    "suggested_sources": parsed.suggested_sources,
                    "complexity_score": parsed.complexity_score,
                    "original_query": user_msg,
                },
                "error": None,
            }
        except Exception as e:
            logger.error(f"parse_intent failed: {e}", exc_info=True)
            # 降级：跳过澄清，直接进入 plan 生成
            return {
                "plan_status": "pending",
                "plan": {"original_query": user_msg, "complexity_score": 0.3},
                "error": None,
            }

    async def _clarify(self, state: MainAgentState) -> dict:
        """Stage 2: LLM 生成澄清问题，设置 pending_review。"""
        plan = state.get("plan", {}) or {}
        logger.info("clarify: generating clarification questions")

        try:
            from ..prompt_optimizer import PromptOptimizer, ParseResult, ClarifyResult

            opt = PromptOptimizer(self._llm)

            # 构造 ParseResult
            parse_result = ParseResult(
                original_query=plan.get("original_query", ""),
                domain=plan.get("domain", ""),
                sub_domains=plan.get("sub_domains", []),
                ambiguities=plan.get("ambiguities", []),
                missing_info=plan.get("missing_info", []),
                complexity_score=plan.get("complexity_score", 0.5),
                suggested_sources=plan.get("suggested_sources", []),
            )

            clarify_result = await opt.clarify(parse_result)

            if not clarify_result.requires_clarification or not clarify_result.questions:
                return {
                    "pending_review": None,
                    "plan_status": "pending",
                }

            # 构建 protocol-compliant review 信封
            review_envelope = {
                "type": "review",
                "subType": "clarify",
                "payload": {
                    "message": "为了更准确地理解你的研究需求，请确认以下问题：",
                    "questions": clarify_result.questions,
                    "education_hints": clarify_result.education_hints,
                },
            }

            logger.info(f"clarify: {len(clarify_result.questions)} questions generated")
            return {
                "pending_review": review_envelope,
                "plan_status": "awaiting_clarify",
            }

        except Exception as e:
            logger.error(f"clarify failed: {e}", exc_info=True)
            return {"pending_review": None, "plan_status": "pending"}

    async def _await_clarify(self, state: MainAgentState) -> dict:
        """Interrupt 节点 — LangGraph 在此暂停，等待用户通过 aresume 回答。"""
        logger.info("await_clarify: graph paused, waiting for user response")
        return {}

    async def _generate_plan(self, state: MainAgentState) -> dict:
        """Stage 3: LLM 生成结构化执行方案。"""
        plan = state.get("plan", {}) or {}
        user_clarification = state.get("user_clarification") or {}

        # 从用户回复中提取答案
        answers = user_clarification.get("answers", []) if isinstance(user_clarification, dict) else []

        logger.info(f"generate_plan: generating plan with {len(answers)} answers")

        try:
            from ..prompt_optimizer import PromptOptimizer, ParseResult

            opt = PromptOptimizer(self._llm)
            parse_result = ParseResult(
                original_query=plan.get("original_query", ""),
                domain=plan.get("domain", ""),
                sub_domains=plan.get("sub_domains", []),
                complexity_score=plan.get("complexity_score", 0.5),
                suggested_sources=plan.get("suggested_sources", []),
            )

            generated = await opt.generate(parse_result, answers)

            # 将 Plan 持久化到 AgentDB
            task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{generated.goal[:20].replace(' ','_')}"
            try:
                self._db.create_agent_task(
                    task_id=task_id,
                    user_query=plan.get("original_query", ""),
                    session_id=state.get("session_id", "main"),
                )
                self._db.update_agent_task(
                    task_id=task_id,
                    plan_json=json.dumps(generated.plan_json, ensure_ascii=False),
                    plan_markdown=generated.plan_markdown,
                    total_steps=len(generated.sub_tasks),
                )
            except Exception as e:
                logger.warning(f"Failed to persist task: {e}")

            # 构建 review(plan) 信封
            review_envelope = {
                "type": "review",
                "subType": "plan",
                "payload": {
                    "taskId": task_id,
                    "goal": generated.goal,
                    "summary": f"分 {len(generated.sub_tasks)} 个子任务执行",
                    "steps": [
                        {
                            "index": i + 1,
                            "action": st.get("action", "execute"),
                            "description": st.get("description", st.get("name", "")),
                            "max_papers": st.get("max_results_per_source", 20),
                            "keywords": st.get("keywords", []),
                        }
                        for i, st in enumerate(generated.sub_tasks)
                    ],
                    "markdown": generated.plan_markdown,
                    "expected_output": generated.expected_output,
                    "risks": generated.risks,
                },
            }

            new_plan = plan.copy()
            new_plan.update({
                "goal": generated.goal,
                "task_id": task_id,
                "sub_tasks": generated.sub_tasks,
                "search_strategy": generated.search_strategy,
                "expected_output": generated.expected_output,
                "refined_query": generated.refined_query,
            })

            return {
                "pending_review": review_envelope,
                "plan": new_plan,
                "plan_status": "awaiting_approval",
            }

        except Exception as e:
            logger.error(f"generate_plan failed: {e}", exc_info=True)
            # 降级方案
            return {
                "pending_review": {
                    "type": "review",
                    "subType": "plan",
                    "payload": {
                        "taskId": f"task-{datetime.now(timezone.utc).strftime('%Y%m%d')}-fallback",
                        "goal": plan.get("original_query", "Research task"),
                        "summary": "Auto-generated fallback plan",
                        "steps": [{"index": 1, "action": "search", "description": "搜索论文", "keywords": [plan.get("original_query", "")]}],
                        "markdown": f"## Research Plan\n\nSearch for: {plan.get('original_query', '')}\n",
                    },
                },
                "plan_status": "awaiting_approval",
            }

    async def _await_approval(self, state: MainAgentState) -> dict:
        """Interrupt 节点 — 等待用户确认/拒绝/修改方案。"""
        logger.info("await_approval: graph paused, waiting for user approval")
        return {}

    async def _execute_plan(self, state: MainAgentState) -> dict:
        """Stage 4: 执行方案 — 委托 IngestAgent 入库。"""
        plan = state.get("plan", {}) or {}
        task_id = plan.get("task_id", "unknown")
        user_query = plan.get("original_query", plan.get("refined_query", ""))
        goal = plan.get("goal", user_query)
        adapter = self._task_adapter

        logger.info(f"execute_plan: delegating to IngestAgent for {task_id}")

        # 更新 DB 任务状态为 running
        try:
            self._db.update_agent_task(task_id, status="running")
        except Exception:
            pass

        # 构建 on_progress 回调 → task(running) WS 推送
        async def _on_ingest_progress(stage: str, stage_index: int,
                                       total_stages: int, current: int, total: int):
            if adapter:
                await adapter.on_task_running(
                    task_id, stage, stage_index, total_stages, current, total,
                )

        try:
            from ..sub_agent import PipelineRunner
            from ..graphs.ingest_graph import IngestAgent
            from ..pdf_converter import PDFConverter
            from ..journal_ranker import JournalRanker
            from ..chroma_store import ChromaStoreV2

            converter = PDFConverter(max_concurrent=2)
            ranker = JournalRanker()
            chroma = ChromaStoreV2()

            runner = PipelineRunner(
                engine=None,  # 由 IngestAgent 的 _search_node 内部创建
                db=self._db, llm=self._llm,
                chroma=chroma, converter=converter, ranker=ranker,
            )
            ingest = IngestAgent(runner, on_progress=_on_ingest_progress)
            ingest_graph = ingest.compile()

            config = {"configurable": {"thread_id": f"ingest-{task_id}"}}
            ingest_result = await ingest_graph.ainvoke(
                {
                    "project_id": task_id,
                    "user_query": user_query,
                    "sources": plan.get("suggested_sources", ["arxiv", "semantic_scholar"]),
                    "year_from": 2022,
                    "max_results": 20,
                    "is_single_tool": False,
                    "single_tool_name": "",
                },
                config=config,
            )

            result_summary = ingest_result.get("result", {})
            logger.info(f"IngestAgent complete: {json.dumps(result_summary, default=str)[:300]}")

            # task(done) + update DB
            if adapter:
                await adapter.on_task_done(task_id, result_summary)
            try:
                self._db.update_agent_task(task_id, status="done",
                                           total_steps=7, current_step=7)
            except Exception:
                pass

            return {
                "plan": {**plan, "execution_results": [result_summary]},
                "plan_status": "done",
            }

        except Exception as e:
            logger.error(f"IngestAgent execution failed: {e}", exc_info=True)

            # task(failed) + update DB
            if adapter:
                await adapter.on_task_failed(task_id, str(e))
            try:
                self._db.update_agent_task(task_id, status="failed")
            except Exception:
                pass

            return {
                "plan": {**plan, "execution_results": [{"error": str(e)}]},
                "plan_status": "done",
                "error": str(e),
            }

    async def _overall_evaluate(self, state: MainAgentState) -> dict:
        """Stage 5: LLM 评估执行结果。"""
        plan = state.get("plan", {}) or {}
        results = plan.get("execution_results", [])

        total_papers = sum(
            r.get("result", {}).get("total", 0) if isinstance(r.get("result"), dict) else 0
            for r in results
        )

        logger.info(f"overall_evaluate: {len(results)} steps completed, {total_papers} papers found")

        if total_papers < 5:
            # 结果太少，建议调整策略
            return {
                "evaluate_assessment": "adjust",
                "plan_status": "needs_adjustment",
                "error": f"Only found {total_papers} papers. Consider broadening keywords.",
            }

        return {
            "evaluate_assessment": "satisfied",
            "plan_status": "done",
            "error": None,
        }

    # ══════════════════════════════════════════════════════════
    # 条件路由
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _needs_clarify(state: MainAgentState) -> str:
        """检查是否需要澄清。"""
        plan_status = state.get("plan_status", "")
        pending_review = state.get("pending_review")
        if plan_status == "awaiting_clarify" and pending_review:
            return "yes"
        # Also check if plan has ambiguities
        plan = state.get("plan") or {}
        if plan.get("ambiguities") or plan.get("missing_info"):
            return "yes"
        return "no"

    @staticmethod
    def _user_approved(state: MainAgentState) -> str:
        """检查用户是否审批通过。"""
        approval = state.get("user_approval") or {}
        if isinstance(approval, dict) and approval.get("confirmed"):
            return "yes"
        return "no"

    @staticmethod
    def _decide_overall(state: MainAgentState) -> str:
        """检查总体评估结果。"""
        assessment = state.get("evaluate_assessment", "satisfied")
        return assessment if assessment in ("satisfied", "adjust") else "satisfied"

    # ══════════════════════════════════════════════════════════
    # 工具执行辅助
    # ══════════════════════════════════════════════════════════

    async def _execute_search(self, keywords: str, plan: dict) -> dict:
        """执行论文搜索（使用子 Agent 工具）。"""
        tool = self._tools.get("search_papers")
        if tool is None:
            return {"summary": "search_papers tool not available", "total": 0}

        try:
            result_str = await tool.coroutine(
                keywords=keywords,
                sources=",".join(plan.get("suggested_sources", ["arxiv", "semantic_scholar"])),
                max_results=20,
            )
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
            return {
                "summary": f"Found {result.get('total', 0)} papers",
                "total": result.get("total", 0),
                "project_id": result.get("project_id", ""),
            }
        except Exception as e:
            logger.error(f"Search execution failed: {e}")
            return {"summary": f"Search failed: {e}", "total": 0}
