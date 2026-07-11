"""RADQueryAgent — 知识库 RAG 问答子 Agent (DEPRECATED v3 Phase 2).

已合并到 KnowledgeAgent (graphs/knowledge_graph.py) 的 query 子图。
新代码请使用 KnowledgeAgent.ask() 方法。

5 节点动态 Execute Graph:
  parse → route → search → evaluate(refine loop) → format

功能:
  - 用户在已入库论文中提问
  - ChromaDB 向量检索 + LLM Reranker + 生成答案
  - 带引用标注的学术问答

用法:
    from .rad_query_graph import RADQueryAgent
    agent = RADQueryAgent(knowledge_base)
    graph = agent.compile()
    result = await graph.ainvoke({"question": "...", "project_id": "..."})
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class RADQueryState(TypedDict, total=False):
    question: str
    project_id: str
    use_fulltext: bool
    top_k: int

    # 检索结果
    query_intent: dict              # parse 输出
    target_collections: list[str]   # 目标 ChromaDB collections
    raw_results: list[dict]         # 初次检索
    reranked: list[dict]            # 重排序后

    # 迭代控制
    retrieval_rounds: int
    max_rounds: int
    is_complete: bool

    # 输出
    answer: str
    sources: list[dict]
    confidence: float
    follow_up_questions: list[str]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# RADQueryAgent
# ═══════════════════════════════════════════════════════════════


class RADQueryAgent:
    """知识库 RAG 问答 Agent — 5 节点动态图。

    图结构:
      parse → route → search → evaluate → format
                ↑                    │
                └── refine loop ─────┘
    """

    DEFAULT_COLLECTIONS = ["papers_abstract", "papers_fulltext"]

    def __init__(self, knowledge_base, on_progress=None):
        """
        Args:
            knowledge_base: KnowledgeBase 实例
            on_progress: 进度回调
        """
        self.kb = knowledge_base
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(RADQueryState)

        builder.add_node("parse", self._parse_node)
        builder.add_node("route", self._route_node)
        builder.add_node("search", self._search_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_node("format", self._format_node)

        builder.add_edge(START, "parse")
        builder.add_edge("parse", "route")
        builder.add_conditional_edges(
            "route", self._where_to_search,
            {"search": "search", "knowledge": "search", "format": "format"},
        )
        builder.add_edge("search", "evaluate")
        builder.add_conditional_edges(
            "evaluate", self._need_refine,
            {"yes": "search", "no": "format"},
        )
        builder.add_edge("format", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("RADQueryAgent not compiled")
        return self._graph

    # ── 节点 ─────────────────────────────────────────

    async def _parse_node(self, state: RADQueryState) -> dict:
        """解析问题意图 — 提取实体和查询类型。"""
        question = state.get("question", "")
        logger.info(f"RADQuery parse: {question[:100]}")

        # 简单规则解析
        query_intent = {
            "question": question,
            "query_type": "general",
            "entities": [],
            "time_filter": None,
        }

        # 检测查询类型
        if any(w in question.lower() for w in ["compare", "difference", "vs", "versus"]):
            query_intent["query_type"] = "comparison"
        elif any(w in question.lower() for w in ["how", "method", "approach", "technique"]):
            query_intent["query_type"] = "method"
        elif any(w in question.lower() for w in ["result", "score", "accuracy", "sota"]):
            query_intent["query_type"] = "result"

        return {"query_intent": query_intent, "retrieval_rounds": 0, "max_rounds": 3,
                "raw_results": [], "reranked": []}

    async def _route_node(self, state: RADQueryState) -> dict:
        """路由 — 确定搜索目标。"""
        project_id = state.get("project_id", "")
        use_fulltext = state.get("use_fulltext", True)
        intent = state.get("query_intent", {})

        collections = list(self.DEFAULT_COLLECTIONS)
        if not use_fulltext:
            collections = ["papers_abstract"]

        logger.info(f"RADQuery route: type={intent.get('query_type')} collections={collections}")
        return {"target_collections": collections}

    async def _search_node(self, state: RADQueryState) -> dict:
        """向量检索 — ChromaDB 搜索。"""
        question = state["question"]
        top_k = state.get("top_k", 5)
        use_fulltext = state.get("use_fulltext", True)
        project_id = state.get("project_id")

        await self._notify("检索中", 3, 5, "向量检索相关知识")

        try:
            result = await self.kb.ask(
                question=question,
                top_k=top_k,
                use_fulltext=use_fulltext,
                project_id=project_id,
            )
            return {
                "answer": result.answer,
                "sources": result.sources,
                "confidence": result.confidence,
                "follow_up_questions": result.follow_up_questions,
                "retrieval_rounds": state.get("retrieval_rounds", 0) + 1,
            }
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return {"error": str(e), "answer": f"检索失败: {e}"}

    async def _evaluate_node(self, state: RADQueryState) -> dict:
        """评估检索结果质量。"""
        confidence = state.get("confidence", 0)
        sources = state.get("sources", [])
        rounds = state.get("retrieval_rounds", 0)
        max_rounds = state.get("max_rounds", 3)

        is_complete = (
            confidence >= 0.6 or
            len(sources) >= 3 or
            rounds >= max_rounds
        )

        logger.info(f"RADQuery evaluate: confidence={confidence:.2f} sources={len(sources)} "
                     f"rounds={rounds} complete={is_complete}")
        return {"is_complete": is_complete}

    async def _format_node(self, state: RADQueryState) -> dict:
        """格式化最终答案。"""
        await self._notify("生成答案", 5, 5, "生成最终答案")
        logger.info("RADQuery format: done")
        return {}

    # ── 条件路由 ─────────────────────────────────────

    @staticmethod
    def _where_to_search(state: RADQueryState) -> str:
        if state.get("error"):
            return "format"
        return "search"

    @staticmethod
    def _need_refine(state: RADQueryState) -> str:
        if state.get("is_complete"):
            return "no"
        return "yes"

    # ── 辅助 ─────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  RADQuery [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception:
                pass
