"""ExecuteGraph — PlanGraph → 子 Agent 的统一调度层。

职责:
  1. 根据 plan.sub_tasks 确定需要哪些子 Agent
  2. 按依赖顺序编排子 Agent
  3. 通过 Celery 异步执行子 Agent
  4. 订阅 Redis Pub/Sub 子 Agent 实时报告
  5. 收集结果 → 回传 PlanGraph

子 Agent 映射:
  ingest          → IngestAgent     (论文搜索入库)
  rad_query       → RADQueryAgent   (知识库问答)
  clustering      → ClusteringAgent (研究方向聚类)
  citation_chase  → CitationChaseAgent (引用追溯)
  history         → HistoryAgent    (历史消息处理)
  translation     → TranslationAgent (术语翻译)

使用方式:
    executor = ExecuteGraph(db, llm, tools, task_adapter, celery_app, redis_url)
    result = await executor.dispatch(task_id, plan)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# 子 Agent 注册表
# ═══════════════════════════════════════════════════════════════

SUB_AGENT_REGISTRY: dict[str, type] = {}  # 运行时填充


def register_sub_agent(name: str):
    """装饰器 — 注册子 Agent 到调度器。"""
    def decorator(cls):
        SUB_AGENT_REGISTRY[name] = cls
        return cls
    return decorator


# ═══════════════════════════════════════════════════════════════
# ExecuteGraph
# ═══════════════════════════════════════════════════════════════


class ExecuteGraph:
    """统一子 Agent 调度器。

    PlanGraph._execute_plan 调用本调度器的 dispatch() 方法。
    调度器根据 sub_tasks 的类型和依赖关系，依次启动对应的子 Agent。
    """

    def __init__(self, db, llm, tools, task_adapter=None,
                 celery_app=None, redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001",
                 report_listener=None):
        self._db = db
        self._llm = llm
        self._tools = tools
        self._task_adapter = task_adapter
        self._celery_app = celery_app
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._report_listener = report_listener  # SubAgentReportListener 实例

    async def dispatch(self, task_id: str, plan: dict) -> dict:
        """根据 plan 分发到对应子 Agent(s) 并等待完成。

        Args:
            task_id: 主任务 ID
            plan: PlanGraph 生成的 plan dict

        Returns:
            {"steps": [...], "total_papers": int, "errors": [...]}
        """
        sub_tasks = plan.get("sub_tasks", [])
        if not sub_tasks:
            logger.warning("dispatch: no sub_tasks in plan")
            return {"steps": [], "error": "No sub_tasks in plan"}

        logger.info(f"ExecuteGraph: dispatching {len(sub_tasks)} sub_tasks for {task_id}")

        # 按依赖顺序分组执行
        steps = []
        all_errors = []

        for i, st in enumerate(sub_tasks):
            step_result = await self._execute_sub_task(
                task_id=task_id,
                sub_task=st,
                step_index=i + 1,
                total_steps=len(sub_tasks),
                plan=plan,
            )
            steps.append(step_result)
            if step_result.get("error"):
                all_errors.append(step_result["error"])

        # 汇总
        total_papers = sum(s.get("papers_found", 0) for s in steps)
        return {
            "task_id": task_id,
            "steps": steps,
            "total_papers": total_papers,
            "errors": all_errors,
        }

    async def _execute_sub_task(self, task_id: str, sub_task: dict,
                                step_index: int, total_steps: int,
                                plan: dict) -> dict:
        """执行单个子任务 → 订阅实时报告 + 通过 Celery 或直接调用。

        根据 sub_task 的 action 字段路由到对应的子 Agent。
        执行前订阅 agent:reports:{task_id}，执行后取消订阅。
        """
        action = sub_task.get("action", "search")
        agent_type = sub_task.get("agent", self._resolve_agent(action))

        logger.info(f"  Step {step_index}/{total_steps}: action={action} agent={agent_type}")

        # ── 订阅子 Agent 实时报告 ──
        if self._report_listener:
            try:
                await self._report_listener.subscribe(task_id)
                logger.debug(f"Subscribed to agent:reports:{task_id}")
            except Exception as e:
                logger.warning(f"Failed to subscribe to reports: {e}")

        # 通知 task(running)
        if self._task_adapter:
            try:
                await self._task_adapter.on_task_running(
                    task_id, f"{action}:{agent_type}", step_index, total_steps, 0, 0,
                )
            except Exception:
                pass

        result = None
        if agent_type == "ingest":
            result = await self._run_ingest(task_id, sub_task, plan)
        elif agent_type == "citation_chase":
            result = await self._run_citation_chase(task_id, sub_task, plan)
        elif agent_type == "rad_query":
            result = await self._run_rad_query(task_id, sub_task, plan)
        elif agent_type == "clustering":
            result = await self._run_clustering(task_id, sub_task, plan)
        elif agent_type == "translation":
            result = await self._run_translation(task_id, sub_task, plan)
        elif agent_type == "history":
            result = await self._run_history(task_id, sub_task, plan)
        else:
            result = {"action": action, "agent": agent_type, "error": f"Unknown agent: {agent_type}"}

        # ── 取消订阅 ──
        if self._report_listener:
            try:
                await self._report_listener.unsubscribe(task_id)
            except Exception as e:
                logger.warning(f"Failed to unsubscribe from reports: {e}")

        return result

    def _resolve_agent(self, action: str) -> str:
        """根据 action 类型推断子 Agent。"""
        mapping = {
            "search": "ingest",
            "download": "ingest",
            "evaluate": "ingest",
            "index": "ingest",
            "survey": "ingest",
            "citation_chase": "citation_chase",
            "query": "rad_query",
            "cluster": "clustering",
            "translate": "translation",
            "history": "history",
        }
        return mapping.get(action, "ingest")

    # ── 子 Agent 执行器 ─────────────────────────────────

    async def _run_ingest(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 IngestAgent — 通过 Celery 或直接调用。"""
        from ..sub_agent import PipelineRunner
        from .ingest_graph import IngestAgent
        from ..pdf_converter import PDFConverter
        from ..journal_ranker import JournalRanker
        from ..chroma_store import ChromaStoreV2

        converter = PDFConverter(max_concurrent=2)
        ranker = JournalRanker()
        chroma = ChromaStoreV2()

        runner = PipelineRunner(
            engine=None, db=self._db, llm=self._llm,
            chroma=chroma, converter=converter, ranker=ranker,
        )

        # 构建 on_progress 回调
        async def _on_progress(stage, stage_index, total_stages, current, total):
            if self._task_adapter:
                await self._task_adapter.on_task_running(
                    task_id, stage, stage_index, total_stages, current, total,
                )

        ingest = IngestAgent(runner, on_progress=_on_progress)
        ingest_graph = ingest.compile()

        config = {"configurable": {"thread_id": f"ingest-{task_id}"}}
        user_query = sub_task.get("description", plan.get("original_query", ""))
        keywords = sub_task.get("keywords", [])
        if keywords:
            user_query = f"{user_query} {' '.join(keywords)}"

        try:
            result = await ingest_graph.ainvoke({
                "project_id": task_id,
                "user_query": user_query,
                "sources": sub_task.get("sources", plan.get("suggested_sources", ["arxiv", "semantic_scholar"])),
                "year_from": sub_task.get("year_from", 2022),
                "max_results": sub_task.get("max_papers", 20),
                "is_single_tool": sub_task.get("is_single_tool", False),
                "single_tool_name": sub_task.get("single_tool_name", ""),
            }, config=config)

            return {
                "action": "ingest",
                "agent": "ingest",
                "papers_found": result.get("result", {}).get("total_papers", 0),
                "result": result.get("result", {}),
            }
        except Exception as e:
            logger.error(f"IngestAgent failed: {e}", exc_info=True)
            return {"action": "ingest", "agent": "ingest", "error": str(e)}

    async def _run_citation_chase(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 CitationChaseAgent。"""
        from .citation_chase_graph import CitationChaseAgent

        agent = CitationChaseAgent(db=self._db, llm=self._llm, engine=None)
        graph = agent.compile()

        config = {"configurable": {"thread_id": f"chase-{task_id}"}}
        try:
            result = await graph.ainvoke({
                "seed_title": sub_task.get("seed_title", plan.get("original_query", "")),
                "seed_doi": sub_task.get("doi", ""),
                "project_id": task_id,
                "max_depth": sub_task.get("depth", 2),
                "direction": sub_task.get("direction", "both"),
            }, config=config)
            return {
                "action": "citation_chase", "agent": "citation_chase",
                "papers_found": result.get("result", {}).get("total_found", 0),
                "result": result.get("result", {}),
            }
        except Exception as e:
            logger.error(f"CitationChaseAgent failed: {e}", exc_info=True)
            return {"action": "citation_chase", "agent": "citation_chase", "error": str(e)}

    async def _run_rad_query(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 RADQueryAgent。"""
        from ..knowledge import KnowledgeBase
        from .rad_query_graph import RADQueryAgent

        kb = KnowledgeBase(self._db, None, self._llm)
        agent = RADQueryAgent(knowledge_base=kb)
        graph = agent.compile()

        config = {"configurable": {"thread_id": f"radq-{task_id}"}}
        try:
            result = await graph.ainvoke({
                "question": sub_task.get("question", sub_task.get("description", "")),
                "project_id": sub_task.get("project_id", task_id),
                "top_k": sub_task.get("top_k", 5),
                "use_fulltext": sub_task.get("use_fulltext", True),
            }, config=config)
            return {
                "action": "rad_query", "agent": "rad_query",
                "answer": result.get("answer", ""),
                "sources": result.get("sources", []),
                "confidence": result.get("confidence", 0),
                "result": {"answer": result.get("answer", "")},
            }
        except Exception as e:
            logger.error(f"RADQueryAgent failed: {e}", exc_info=True)
            return {"action": "rad_query", "agent": "rad_query", "error": str(e)}

    async def _run_clustering(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 ClusteringAgent。"""
        from .clustering_graph import ClusteringAgent

        agent = ClusteringAgent(db=self._db, chroma_store=None, llm=self._llm)
        graph = agent.compile()

        config = {"configurable": {"thread_id": f"cluster-{task_id}"}}
        try:
            result = await graph.ainvoke({
                "project_id": task_id,
                "n_clusters": sub_task.get("n_clusters", 0),
            }, config=config)
            return {
                "action": "clustering", "agent": "clustering",
                "n_clusters": result.get("n_clusters", 0),
                "result": result.get("result", {}),
            }
        except Exception as e:
            logger.error(f"ClusteringAgent failed: {e}", exc_info=True)
            return {"action": "clustering", "agent": "clustering", "error": str(e)}

    async def _run_translation(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 TranslationAgent。"""
        from .translation_graph import TranslationAgent

        agent = TranslationAgent(db=self._db, llm=self._llm)
        result = await agent.translate_query(
            text=sub_task.get("text", sub_task.get("description", "")),
            direction=sub_task.get("direction", "zh2en"),
            project_id=task_id,
        )
        return {"action": "translation", "agent": "translation",
                "result": result}

    async def _run_history(self, task_id: str, sub_task: dict, plan: dict) -> dict:
        """执行 HistoryAgent。"""
        from .history_graph import HistoryAgent

        agent = HistoryAgent(db=self._db, memory=None, llm=self._llm)
        graph = agent.compile()

        messages = sub_task.get("messages", [])
        config = {"configurable": {"thread_id": f"hist-{task_id}"}}
        try:
            result = await graph.ainvoke({
                "messages": messages,
                "agent_id": self._agent_id,
                "session_id": sub_task.get("session_id", "main"),
            }, config=config)
            return {
                "action": "history", "agent": "history",
                "result": result.get("result", {}),
            }
        except Exception as e:
            logger.error(f"HistoryAgent failed: {e}", exc_info=True)
            return {"action": "history", "agent": "history", "error": str(e)}
