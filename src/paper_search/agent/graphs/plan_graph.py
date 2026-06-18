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

        图结构 (v2 — Phase 2):
          START → parse_intent → (clarify? → await_clarify →) generate_plan
          → await_approval → await_permissions → execute_plan
          → overall_evaluate → (satisfied? → END | adjust → generate_plan)
        """
        builder = StateGraph(MainAgentState)

        # 添加节点
        builder.add_node("parse_intent", self._parse_intent)
        builder.add_node("clarify", self._clarify)
        builder.add_node("await_clarify", self._await_clarify)
        builder.add_node("generate_plan", self._generate_plan)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("await_permissions", self._await_permissions)  # [NEW] Phase 2
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

        # await_approval → 检查权限需求 → await_permissions 或 execute_plan
        builder.add_conditional_edges(
            "await_approval", self._needs_permissions,
            {"yes": "await_permissions", "no": "execute_plan", "rejected": END},
        )

        # await_permissions → 检查权限确认
        builder.add_conditional_edges(
            "await_permissions", self._permissions_confirmed,
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

    async def _await_permissions(self, state: MainAgentState) -> dict:
        """[NEW] Interrupt 节点 — 等待用户确认权限清单。

        在用户批准 plan 之后、执行之前，一次性列出所有子 Agent
        需要的权限（搜索/下载/通知等），用户一次性确认。
        """
        plan = state.get("plan", {}) or {}
        task_id = plan.get("task_id", "unknown")
        sub_tasks = plan.get("sub_tasks", [])

        # 收集权限清单
        permissions = self._collect_permissions(sub_tasks, plan)

        if not permissions:
            return {"pending_review": None}

        review_envelope = {
            "type": "review",
            "subType": "permissions",
            "payload": {
                "taskId": task_id,
                "message": "以下操作需要你的授权，请一次性确认：",
                "permissions": permissions,
            },
        }

        logger.info(f"await_permissions: {len(permissions)} permissions for {task_id}")
        return {
            "pending_review": review_envelope,
            "plan_status": "awaiting_permissions",
        }

    def _collect_permissions(self, sub_tasks: list, plan: dict) -> list[dict]:
        """从子任务列表收集所需权限。

        权限类型:
          - search: 搜索论文 (source, max_results, year_range)
          - download: 下载 PDF (count, estimate_size)
          - notification: 通知推送 (scope)
          - ios_calendar: 日历访问
          - ios_file: 文件读写
        """
        permissions = []
        total_papers_estimate = 0

        for st in sub_tasks:
            action = st.get("action", "search")
            agent = st.get("agent", "ingest")

            if action == "search":
                max_papers = st.get("max_papers", st.get("max_results_per_source", 20))
                sources = st.get("sources", plan.get("suggested_sources", ["arxiv", "semantic_scholar"]))
                keywords = st.get("keywords", [])
                total_papers_estimate += max_papers * len(sources)

                permissions.append({
                    "id": f"search-{len(permissions)}",
                    "tool": "search_papers",
                    "scope": ", ".join(sources) if isinstance(sources, list) else str(sources),
                    "description": f"在 {', '.join(sources) if isinstance(sources, list) else sources} 搜索: {', '.join(keywords[:5]) if keywords else '相关论文'}",
                    "maxResults": max_papers,
                    "agent": agent,
                })

            elif action == "download":
                count = st.get("download_count", 10)
                permissions.append({
                    "id": f"download-{len(permissions)}",
                    "tool": "download_paper",
                    "scope": f"{count} papers",
                    "estimateSize": f"{count * 5}MB",
                    "description": f"下载约 {count} 篇论文 PDF",
                    "agent": agent,
                })

            elif action == "notify":
                permissions.append({
                    "id": f"notify-{len(permissions)}",
                    "tool": "ios_notification_local",
                    "scope": st.get("notification_scope", "task_complete"),
                    "description": st.get("description", "任务完成时推送通知"),
                    "agent": agent,
                })

            elif action == "citation_chase":
                permissions.append({
                    "id": f"citation-{len(permissions)}",
                    "tool": "citation_chase",
                    "scope": f"depth={st.get('depth', 2)}",
                    "description": f"引用追溯 (深度 {st.get('depth', 2)} 层)",
                    "agent": agent,
                })

        # 总览
        if total_papers_estimate > 0:
            permissions.append({
                "id": "summary",
                "tool": "_summary",
                "scope": "",
                "description": f"预计共搜索约 {total_papers_estimate} 篇论文，下载并入库相关论文",
                "estimate": f"搜索 {total_papers_estimate} 篇，下载 ~{total_papers_estimate // 4} 篇",
                "agent": "ingest",
            })

        return permissions

    async def _execute_plan(self, state: MainAgentState) -> dict:
        """Stage 4: 执行方案 — 委托 ExecuteGraph 调度子 Agent。"""
        plan = state.get("plan", {}) or {}
        task_id = plan.get("task_id", "unknown")
        adapter = self._task_adapter

        logger.info(f"execute_plan: delegating to ExecuteGraph for {task_id}")

        # 更新 DB 任务状态为 running
        try:
            self._db.update_agent_task(task_id, status="running")
        except Exception:
            pass

        # 通知 task(started)
        if adapter:
            try:
                await adapter.on_task_started(
                    task_id, plan.get("goal", plan.get("original_query", "Research")),
                    mode="foreground",
                    total_stages=len(plan.get("sub_tasks", [])),
                )
            except Exception:
                pass

        try:
            # ── Phase 2: 由 ExecuteGraph 统一调度 ──
            from .execute_graph import ExecuteGraph

            executor = ExecuteGraph(
                db=self._db, llm=self._llm, tools=self._tools,
                task_adapter=adapter,
            )
            dispatch_result = await executor.dispatch(task_id, plan)

            logger.info(f"ExecuteGraph complete: {json.dumps(dispatch_result, default=str)[:300]}")

            # task(done) + update DB
            if adapter:
                await adapter.on_task_done(task_id, dispatch_result)
            try:
                self._db.update_agent_task(task_id, status="done",
                                           total_steps=len(plan.get("sub_tasks", [])),
                                           current_step=len(plan.get("sub_tasks", [])))
            except Exception:
                pass

            return {
                "plan": {**plan, "execution_results": [dispatch_result]},
                "plan_status": "done",
            }

        except Exception as e:
            logger.error(f"Execution failed: {e}", exc_info=True)

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
        """[DEPRECATED] 被 _needs_permissions 替代。保留向后兼容。"""
        approval = state.get("user_approval") or {}
        if isinstance(approval, dict) and approval.get("confirmed"):
            return "yes"
        return "no"

    @staticmethod
    def _needs_permissions(state: MainAgentState) -> str:
        """检查是否需要权限确认。

        Returns:
            "yes" → 进入 await_permissions
            "no" → 直接进入 execute_plan
            "rejected" → 用户拒绝了 plan，结束
        """
        # 先检查用户是否审批
        approval = state.get("user_approval") or {}
        if isinstance(approval, dict) and not approval.get("confirmed"):
            return "rejected"

        # 检查是否有子任务需要权限
        plan = state.get("plan", {}) or {}
        sub_tasks = plan.get("sub_tasks", [])
        for st in sub_tasks:
            action = st.get("action", "")
            if action in ("search", "download", "citation_chase", "notify"):
                return "yes"

        return "no"

    @staticmethod
    def _permissions_confirmed(state: MainAgentState) -> str:
        """检查用户是否确认权限。"""
        perm_approval = state.get("user_approval") or {}
        if isinstance(perm_approval, dict) and perm_approval.get("confirmed"):
            return "yes"
        # 检查旧字段名
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
