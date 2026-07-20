"""Knowledge Agent — 知识入库与 RAG 问答 (v3 Phase 2).

从 IngestAgent 拆分出的后半段 + rad_query_graph 合并:
  - 入库路径: chunk → embed → dedup → rank (从 ingest 拆分)
  - 问答路径: parse → route → search → evaluate → format (从 rad_query 迁移)
  - 知识发现: discover_gaps → find_related (从 knowledge.py 迁移)

LangGraph 双图结构:
  IngestGraph:    chunk → embed → dedup → rank
  QueryGraph:     parse → route → search → evaluate(refine loop) → format

用法:
    from .knowledge_graph import KnowledgeAgent
    agent = KnowledgeAgent(db, vector_store, llm_client)

    # 入库
    result = await agent.run_ingest(papers, project_id)

    # RAG 问答
    result = await agent.ask("transformer attention 机制如何工作?", project_id="prj-xxx")
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State types
# ═══════════════════════════════════════════════════════════════


class KnowledgeIngestState(TypedDict, total=False):
    """入库子图 State — Literature Agent 的输出作为输入。"""
    project_id: str
    user_query: str
    papers: list[dict]         # Literature Agent 产出的论文列表
    current_stage: str
    stage_index: int
    total_stages: int
    errors: list[dict]
    # 输出
    indexed_count: int
    ranked_count: int
    result: Optional[dict]
    error: Optional[str]


class KnowledgeIngestLocalState(TypedDict, total=False):
    """本地 PDF 入库 State — 从 PDF 目录出发，完整入库流程。"""
    # 输入
    pdf_dir: str
    project_id: str
    user_id: str

    # Phase 1: 扫描
    discovered_pdfs: list[str]
    total_files: int

    # Phase 2: 快速标题去重
    fast_deduped: list[dict]
    fast_dup_count: int

    # Phase 3: PDF→MD 转换
    converted: list[dict]
    convert_failed: list[dict]

    # Phase 4: 元数据提取
    papers_meta: list[dict]
    meta_quality: dict

    # Phase 5: 图表提取
    figures: list[dict]
    tables: list[dict]

    # Phase 6: 摘要向量去重
    vector_deduped: list[dict]
    vector_dup_count: int

    # Phase 7: 分块索引
    indexed: list[dict]
    index_errors: list[dict]

    # Phase 8: PDF 清理
    cleaned_pdfs: list[str]

    # 全局
    current_phase: str
    errors: list[dict]
    result: Optional[dict]
    error: Optional[str]


class KnowledgeQueryState(TypedDict, total=False):
    """RAG 问答子图 State — 合并自 RADQueryState。"""
    question: str
    project_id: str
    top_k: int
    use_fulltext: bool

    # 检索结果
    query_intent: dict
    target_collections: list[str]
    raw_results: list[dict]
    reranked: list[dict]

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
    errors: list[dict]     # 累积的 AgentError（包含 agent/node/type/message/traceback）


# ═══════════════════════════════════════════════════════════════
# KnowledgeAgent
# ═══════════════════════════════════════════════════════════════


class KnowledgeAgent:
    """知识入库与 RAG 问答 Agent。

    两个子图:
      - ingest_graph: chunk → embed → dedup → rank（论文入库）
      - query_graph:  parse → route → search → evaluate ⇄ refine → format（RAG 问答）

    用法:
        from .knowledge_graph import KnowledgeAgent
        agent = KnowledgeAgent(db, vector_store, llm_client)

        # 入库
        result = await agent.run_ingest(papers, "prj-xxx", "machine learning")

        # 问答
        answer = await agent.ask("什么是 attention?")
    """

    DEFAULT_COLLECTIONS = ["papers_abstract", "papers_fulltext"]

    def __init__(self, db=None, vector_store=None, llm_client=None,
                 runner=None, on_progress=None, oss_dir: str = ""):
        """
        Args:
            db: AgentDB / PostgresAgentDB 实例
            vector_store: ChromaStoreV2 / PgVectorStore 实例
            llm_client: LLMClientV2 实例
            runner: PipelineRunner 实例（有 runner 时走 runner 方法）
            on_progress: 进度回调
            oss_dir: OSS 归档根目录 (local PDF ingest 用)
        """
        self.db = db
        self.vector_store = vector_store
        self.llm = llm_client
        self.runner = runner
        self._on_progress = on_progress
        self._ingest_graph = None
        self._query_graph = None
        self._ingest_local_graph = None
        from ...config import get_base_dir
        self.oss_dir = oss_dir or str(get_base_dir())

    # ═══════════════════════════════════════════════════════════
    # Ingest Graph: chunk → embed → dedup → rank
    # ═══════════════════════════════════════════════════════════

    def compile_ingest(self, checkpointer=None):
        """编译入库子图。"""
        builder = StateGraph(KnowledgeIngestState)

        builder.add_node("chunk", self._chunk_node)
        builder.add_node("embed", self._embed_node)
        builder.add_node("dedup", self._dedup_node)
        builder.add_node("rank", self._rank_node)

        builder.add_edge(START, "chunk")
        builder.add_edge("chunk", "embed")
        builder.add_edge("embed", "dedup")
        builder.add_edge("dedup", "rank")
        builder.add_edge("rank", END)

        self._ingest_graph = builder.compile(checkpointer=checkpointer)
        return self._ingest_graph

    async def run_ingest(self, papers: list[dict], project_id: str,
                         user_query: str = "") -> dict:
        """直接调用入库流程（非 graph 模式，适合被 MainAgent 调用）。"""
        if self._ingest_graph is None:
            self.compile_ingest()

        state = {
            "project_id": project_id,
            "user_query": user_query,
            "papers": papers,
            "errors": [],
        }
        result = await self._ingest_graph.ainvoke(state)
        return result

    # ── 入库节点 ──────────────────────────────────────

    async def _chunk_node(self, state: KnowledgeIngestState) -> dict:
        """Section-aware 分块 — 双模式（语义 + 滑动窗口）。"""
        papers = state.get("papers", [])
        to_chunk = [p for p in papers if p.get("convert_ok") and p.get("markdown_path")]

        await self._notify("切片", 1, 4, f"论文切片 ({0}/{len(to_chunk)})", 0, len(to_chunk))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(to_chunk):
            await self._notify("切片", 1, 4, f"切片 ({i+1}/{len(to_chunk)})", i + 1, len(to_chunk))
            try:
                if self.runner:
                    result = await self.runner._chunk_single(
                        paper["paper_id"], paper["markdown_path"], paper.get("title", ""),
                    )
                else:
                    result = await self._chunk_with_vector_store(paper)
                paper["chunks"] = result.get("chunks", [])
                paper["chunk_ok"] = result.get("success", False)
            except Exception as e:
                logger.warning(f"Chunk failed for {paper.get('title', '')}: {e}")
                paper["chunk_ok"] = False
                paper["chunks"] = []
                errors.append({"stage": "chunk", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": str(e)})

        return {"papers": papers, "errors": errors, "current_stage": "chunk", "stage_index": 1,
                "total_stages": 4}

    async def _embed_node(self, state: KnowledgeIngestState) -> dict:
        """向量化 — 使用 pgvector/ChromaDB 生成 embedding 并存储。"""
        papers = state.get("papers", [])
        to_embed = [p for p in papers if p.get("chunk_ok") and p.get("chunks")]

        await self._notify("向量化", 2, 4, f"向量化 ({0}/{len(to_embed)})", 0, len(to_embed))

        errors = list(state.get("errors", []))
        total_chunks = 0
        for i, paper in enumerate(to_embed):
            chunks = paper.get("chunks", [])
            await self._notify("向量化", 2, 4, f"向量化 ({i+1}/{len(to_embed)})", i + 1, len(to_embed))
            try:
                if self.runner:
                    result = await self.runner._embed_single(
                        paper["paper_id"], chunks, paper.get("title", ""),
                        paper.get("abstract", ""),
                    )
                else:
                    result = await self._embed_with_vector_store(paper, chunks)
                paper["embed_ok"] = result.get("success", False)
                paper["embedded_chunks"] = result.get("count", 0)
                total_chunks += result.get("count", 0)
            except Exception as e:
                logger.warning(f"Embed failed for {paper.get('title', '')}: {e}")
                paper["embed_ok"] = False
                errors.append({"stage": "embed", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": str(e)})

        return {"papers": papers, "errors": errors, "current_stage": "embed", "stage_index": 2}

    async def _dedup_node(self, state: KnowledgeIngestState) -> dict:
        """去重 — 基于向量相似度和 DOI 匹配。"""
        papers = state.get("papers", [])
        indexed = [p for p in papers if p.get("embed_ok")]

        await self._notify("去重", 3, 4, "去重检查", len(indexed), len(indexed))

        if indexed and self.vector_store:
            try:
                for paper in indexed:
                    paper_id = paper.get("paper_id", "")
                    if self.runner:
                        dup_result = await self.runner._dedup_single(paper_id, paper)
                    else:
                        dup_result = await self._dedup_with_vector_store(paper_id, paper)
                    paper["is_duplicate"] = dup_result.get("is_duplicate", False)
                    paper["duplicate_of"] = dup_result.get("duplicate_of")
            except Exception as e:
                logger.warning(f"Dedup failed: {e}")

        return {"papers": papers, "current_stage": "dedup", "stage_index": 3}

    async def _rank_node(self, state: KnowledgeIngestState) -> dict:
        """期刊评级 — CCF+SCI 分级。"""
        papers = state.get("papers", [])
        unique = [p for p in papers if p.get("embed_ok") and not p.get("is_duplicate")]

        await self._notify("评级", 4, 4, f"评定期刊等级", len(unique), len(unique))

        if unique and self.runner:
            await self.runner._rank_stage("knowledge", state["project_id"], unique, None)

        errors = state.get("errors", [])
        indexed_count = sum(1 for p in papers if p.get("embed_ok") and not p.get("is_duplicate"))

        return {
            "current_stage": "rank", "stage_index": 4,
            "indexed_count": indexed_count, "ranked_count": len(unique),
            "result": {
                "project_id": state.get("project_id", ""),
                "total_papers": len(papers),
                "indexed": indexed_count,
                "failed": len(errors),
                "errors": errors[:20],
            },
        }

    # ═══════════════════════════════════════════════════════════
    # Query Graph: parse → route → search → evaluate → format
    # ═══════════════════════════════════════════════════════════

    def compile_query(self, checkpointer=None):
        """编译 RAG 问答子图。"""
        builder = StateGraph(KnowledgeQueryState)

        builder.add_node("parse", self._parse_node)
        builder.add_node("route", self._route_node)
        builder.add_node("search", self._search_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_node("format", self._format_node)

        builder.add_edge(START, "parse")
        builder.add_edge("parse", "route")
        builder.add_conditional_edges(
            "route", self._where_to_search,
            {"search": "search", "format": "format"},
        )
        builder.add_edge("search", "evaluate")
        builder.add_conditional_edges(
            "evaluate", self._need_refine,
            {"yes": "search", "no": "format"},
        )
        builder.add_edge("format", END)

        self._query_graph = builder.compile(checkpointer=checkpointer)
        return self._query_graph

    async def ask(self, question: str, project_id: str = None,
                  top_k: int = 5, use_fulltext: bool = True) -> dict:
        """RAG 问答入口。"""
        if self._query_graph is None:
            self.compile_query()

        result = await self._query_graph.ainvoke({
            "question": question,
            "project_id": project_id or "",
            "top_k": top_k,
            "use_fulltext": use_fulltext,
            "max_rounds": 3,
        })
        return {
            "question": question,
            "answer": result.get("answer", ""),
            "sources": result.get("sources", []),
            "confidence": result.get("confidence", 0.0),
            "follow_up_questions": result.get("follow_up_questions", []),
        }

    # ── Query 节点 ──────────────────────────────────

    async def _parse_node(self, state: KnowledgeQueryState) -> dict:
        question = state.get("question", "")
        logger.info(f"KnowledgeAgent parse: {question[:100]}")

        query_intent = {
            "question": question,
            "query_type": "general",
            "entities": [],
            "time_filter": None,
        }

        lower = question.lower()
        if any(w in lower for w in ["compare", "difference", "vs", "versus"]):
            query_intent["query_type"] = "comparison"
        elif any(w in lower for w in ["how", "method", "approach", "technique"]):
            query_intent["query_type"] = "method"
        elif any(w in lower for w in ["result", "score", "accuracy", "sota"]):
            query_intent["query_type"] = "result"

        return {"query_intent": query_intent, "retrieval_rounds": 0, "max_rounds": 3}

    async def _route_node(self, state: KnowledgeQueryState) -> dict:
        project_id = state.get("project_id", "")
        use_fulltext = state.get("use_fulltext", True)

        collections = list(self.DEFAULT_COLLECTIONS)
        if not use_fulltext:
            collections = ["papers_abstract"]

        logger.info(f"KnowledgeAgent route: collections={collections}")
        return {"target_collections": collections}

    async def _search_node(self, state: KnowledgeQueryState) -> dict:
        question = state["question"]
        top_k = state.get("top_k") or 5
        use_fulltext = state.get("use_fulltext") or True
        project_id = state.get("project_id")

        await self._notify("检索中", 3, 5, "向量检索相关知识")

        t_start = time.monotonic()
        retrieval_ms = 0
        rerank_ms = 0

        try:
            if self.vector_store:
                # Stage 1: 向量检索
                t1 = time.monotonic()
                results = self.vector_store.search_similar(
                    question, n_results=top_k * 2,
                )
                retrieval_ms = int((time.monotonic() - t1) * 1000)

                # Short-circuit: RAG 无结果 → 直接返回，不调 rerank/LLM
                if not results:
                    logger.info("KnowledgeAgent search: no vector results, returning empty")
                    self._record_rag_trace(
                        session_id=state.get("session_id") or "",
                        query_text=question,
                        retrieved_count=0,
                        reranked_count=0,
                        retrieval_ms=retrieval_ms,
                        rerank_ms=0,
                        total_ms=int((time.monotonic() - t_start) * 1000),
                        confidence=0.0,
                    )
                    return {
                        "sources": [],
                        "answer": "",
                        "retrieval_rounds": state.get("retrieval_rounds", 0) + 1,
                        "error": "",
                    }

                # Stage 2: Cross-Encoder Rerank
                t2 = time.monotonic()
                results = await self._llm_rerank(question, results, top_k)
                rerank_ms = int((time.monotonic() - t2) * 1000)

                sources = [{
                    "paper_id": r.get("paper_id", ""),
                    "title": r.get("title", ""),
                    "relevance": r.get("score", 0.5),
                    "snippet": (r.get("chunk_text") or r.get("abstract") or "")[:500],
                } for r in results[:top_k]]

                # 记录 RAG trace (fire-and-forget)
                self._record_rag_trace(
                    session_id=state.get("session_id") or "",
                    query_text=question,
                    retrieved_count=len(results),
                    reranked_count=len(sources),
                    retrieval_ms=retrieval_ms,
                    rerank_ms=rerank_ms,
                    total_ms=int((time.monotonic() - t_start) * 1000),
                    confidence=0.7 if sources else 0.1,
                )

                return {
                    "sources": sources,
                    "confidence": 0.7 if sources else 0.1,
                    "retrieval_rounds": state.get("retrieval_rounds", 0) + 1,
                }
            else:
                return {
                    "answer": "向量存储未初始化，无法执行检索。",
                    "confidence": 0.0,
                    "retrieval_rounds": state.get("retrieval_rounds", 0) + 1,
                }
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(
                f"KnowledgeAgent search failed (round {state.get('retrieval_rounds', 0) + 1}/{state.get('max_rounds', 3)}): "
                f"{type(e).__name__}: {e}\n{tb}"
            )
            # 记录错误 trace
            self._record_rag_trace(
                session_id=state.get("session_id", ""),
                query_text=question,
                retrieved_count=0,
                reranked_count=0,
                retrieval_ms=retrieval_ms,
                rerank_ms=rerank_ms,
                total_ms=int((time.monotonic() - t_start) * 1000),
                confidence=0.0,
                error_text=str(e),
            )
            # CRITICAL: increment retrieval_rounds even on error to prevent infinite loop
            current_round = state.get("retrieval_rounds", 0) + 1
            accumulated_errors = list(state.get("errors", []))
            accumulated_errors.append({
                "agent": "KnowledgeAgent",
                "node": "_search_node",
                "error_type": type(e).__name__,
                "message": str(e),
                "traceback": tb,
                "context": {
                    "question": question[:200],
                    "top_k": top_k,
                    "use_fulltext": use_fulltext,
                    "project_id": project_id,
                },
                "retry_count": current_round,
                "max_retries": state.get("max_rounds", 3),
            })
            return {
                "error": str(e),
                "errors": accumulated_errors,
                "retrieval_rounds": current_round,
                "confidence": 0.0,
                "sources": [],
            }

    async def _evaluate_node(self, state: KnowledgeQueryState) -> dict:
        confidence = state.get("confidence", 0)
        sources = state.get("sources", [])
        rounds = state.get("retrieval_rounds", 0)
        max_rounds = state.get("max_rounds", 3)

        is_complete = (
            confidence >= 0.6 or
            len(sources) >= 3 or
            rounds >= max_rounds
        )

        logger.info(f"KnowledgeAgent evaluate: conf={confidence:.2f} sources={len(sources)} "
                     f"rounds={rounds} complete={is_complete}")
        return {"is_complete": is_complete}

    async def _format_node(self, state: KnowledgeQueryState) -> dict:
        await self._notify("生成答案", 5, 5, "生成最终答案")

        if state.get("answer"):
            return {}

        sources = state.get("sources", [])
        question = state.get("question", "")

        # 使用 LLM 生成最终答案（如果可用）
        if self.llm and sources:
            try:
                context = "\n\n".join([
                    f"[{i+1}] {s.get('title','')}: {s.get('snippet','')}"
                    for i, s in enumerate(sources[:5])
                ])
                prompt = (
                    f"Based on the following papers, answer the question.\n\n"
                    f"Context:\n{context}\n\n"
                    f"Question: {question}\n\n"
                    f"Provide a concise answer with citations like [1], [2], etc."
                )
                response = await self.llm.chat([{"role": "user", "content": prompt}])
                return {
                    "answer": response,
                    "confidence": state.get("confidence", 0.7),
                }
            except Exception as e:
                logger.warning(f"LLM format failed: {e}")

        return {
            "answer": f"Found {len(sources)} relevant papers. "
                      f"Top match: {sources[0].get('title', 'N/A') if sources else 'N/A'}",
        }

    # ── 条件路由 ─────────────────────────────────────

    @staticmethod
    def _where_to_search(state: KnowledgeQueryState) -> str:
        if state.get("error"):
            return "format"
        return "search"

    @staticmethod
    def _need_refine(state: KnowledgeQueryState) -> str:
        if state.get("is_complete"):
            return "no"
        return "yes"

    # ── 知识发现 ─────────────────────────────────────

    async def discover_gaps(self, domain: str = "", project_id: str = None) -> dict:
        """研究空白发现 — 分析已入库论文，找出研究缺口。"""
        logger.info(f"KnowledgeAgent discover_gaps: domain={domain}")

        if not self.vector_store:
            return {"domain": domain, "gaps": [], "trends": [], "message": "向量存储未初始化"}

        try:
            # 搜索领域论文
            results = self.vector_store.search_similar(
                domain or "research gaps", n_results=50,
            )

            # 简单聚类分析
            topics = {}
            for r in results:
                venue = r.get("venue", "unknown")
                year = r.get("year", 0)
                topics.setdefault(venue, []).append(year)

            # 识别趋势
            gaps = []
            for venue, years in topics.items():
                if len(years) < 3:
                    gaps.append({
                        "topic": venue,
                        "papers_count": len(years),
                        "suggestion": "under-explored area",
                    })

            return {
                "domain": domain,
                "gaps": gaps,
                "trends": [{"venue": v, "count": len(ys), "latest": max(ys) if ys else 0}
                            for v, ys in topics.items()],
            }
        except Exception as e:
            logger.error(f"discover_gaps failed: {e}")
            return {"domain": domain, "gaps": [], "trends": [], "error": str(e)}

    async def find_related(self, paper_id: str, top_k: int = 10) -> list[dict]:
        """查找相关论文。"""
        if not self.vector_store:
            return []
        try:
            results = self.vector_store.search_similar(
                f"paper_id:{paper_id}", n_results=top_k,
            )
            return [{"paper_id": r.get("paper_id", ""), "title": r.get("title", ""),
                     "score": r.get("score", 0)} for r in results]
        except Exception as e:
            logger.error(f"find_related failed: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # Local PDF Ingest — 本地 PDF 入库 (8 Phase)
    # ═══════════════════════════════════════════════════════════════

    def compile_ingest_local(self, checkpointer=None):
        """编译本地 PDF 入库子图（单节点，8 phase 内部串联）。"""
        builder = StateGraph(KnowledgeIngestLocalState)
        builder.add_node("ingest_local_pdfs", self._ingest_local_pdfs)
        builder.add_edge(START, "ingest_local_pdfs")
        builder.add_edge("ingest_local_pdfs", END)
        self._ingest_local_graph = builder.compile(checkpointer=checkpointer)
        return self._ingest_local_graph

    async def run_ingest_local(self, pdf_dir: str, project_id: str,
                                user_id: str = "user-default") -> dict:
        """入口：本地 PDF 目录 → 全量入库。"""
        if self._ingest_local_graph is None:
            self.compile_ingest_local()
        state = {
            "pdf_dir": pdf_dir,
            "project_id": project_id,
            "user_id": user_id,
            "errors": [],
        }
        result = await self._ingest_local_graph.ainvoke(state)
        return result.get("result", {})

    # ── 主节点: _ingest_local_pdfs ─────────────────────

    async def _ingest_local_pdfs(self, state: KnowledgeIngestLocalState) -> dict:
        """本地 PDF 入库主节点 — 8 Phase 串联。

        Phase 1: 扫描发现 PDF 文件
        Phase 2: 快速标题去重 (DB 精确匹配)
        Phase 3: PDF→MD 批量转换
        Phase 4: MD 元数据提取 + 质量评估
        Phase 5: 图表提取 (figure→OSS, table→MD)
        Phase 6: 摘要向量去重
        Phase 7: 分块 + 向量索引 (复用已有 ingest 流程)
        Phase 8: PDF 清理 (移动到 OSS 归档目录)
        """
        pdf_dir = state["pdf_dir"]
        project_id = state.get("project_id", "proj-local-ingest")
        user_id = state.get("user_id", "user-default")
        errors: list[dict] = list(state.get("errors", []))

        dp = Path(pdf_dir)
        if not dp.exists():
            return {"error": f"PDF 目录不存在: {pdf_dir}", "result": {"success": False}}

        # ── P1: 扫描 ──
        await self._notify("scan", 0, 1, "正在扫描 PDF 文件...", 0, 0)
        pdfs = list(dp.rglob("*.pdf"))
        total = len(pdfs)
        await self._notify("scan", 0, 1, f"发现 {total} 个 PDF 文件", 0, total)
        if not pdfs:
            return {"result": {"success": True, "total": 0, "message": "没有找到 PDF 文件"}}

        # ── P2: 快速标题去重 ──
        fast_deduped, fast_dup_count = await self._phase2_fast_title_dedup(
            pdfs, user_id, total,
        )

        # ── P3: PDF→MD 转换 ──
        converted, convert_failed = await self._phase3_convert_pdfs(
            fast_deduped, project_id, total,
        )

        # ── P4: 元数据提取 ──
        papers_meta, meta_quality = await self._phase4_extract_metadata(
            converted, total,
        )

        # ── P5: 图表提取 ──
        figures, tables = await self._phase5_extract_figures(
            converted, papers_meta, user_id,
        )

        # ── P6: 摘要向量去重 ──
        vector_deduped, vector_dup_count = await self._phase6_vector_dedup(
            papers_meta, user_id,
        )

        # ── P7: 入库 + 索引 ──
        indexed, index_errors = await self._phase7_index_papers(
            vector_deduped, project_id, user_id, figures,
        )

        # ── P8: PDF 清理 ──
        cleaned_pdfs = await self._phase8_cleanup_pdfs(
            vector_deduped, user_id,
        )

        result = {
            "success": True,
            "total_files": total,
            "fast_dup_count": fast_dup_count,
            "converted_count": len(converted),
            "convert_failed": len(convert_failed),
            "meta_quality": meta_quality,
            "figures_count": len(figures),
            "tables_count": len(tables),
            "vector_dup_count": vector_dup_count,
            "indexed_count": len(indexed),
            "index_errors": len(index_errors),
            "cleaned_pdfs": len(cleaned_pdfs),
            "errors": errors,
        }
        return {
            "result": result,
            "discovered_pdfs": [str(p) for p in pdfs],
            "total_files": total,
            "fast_deduped": fast_deduped,
            "fast_dup_count": fast_dup_count,
            "converted": converted,
            "convert_failed": convert_failed,
            "papers_meta": papers_meta,
            "meta_quality": meta_quality,
            "figures": figures,
            "tables": tables,
            "vector_deduped": vector_deduped,
            "vector_dup_count": vector_dup_count,
            "indexed": indexed,
            "index_errors": index_errors,
            "cleaned_pdfs": cleaned_pdfs,
            "current_phase": "done",
            "errors": errors,
        }

    # ── Phase 2: 快速标题去重 ──────────────────────────

    async def _phase2_fast_title_dedup(
        self, pdfs: list[Path], user_id: str, total: int,
    ) -> tuple[list[dict], int]:
        """P2: 基于文件名的候选标题进行 DB 精确匹配去重。"""
        candidates = []
        for p in pdfs:
            # 从文件名提取候选标题 (Author_Year_Title.pdf)
            stem = p.stem
            parts = stem.split("_", 2)
            title_candidate = parts[2] if len(parts) >= 3 else stem
            title_candidate = title_candidate.replace("_", " ").strip()
            candidates.append({"pdf_path": str(p), "title_candidate": title_candidate})

        fast_deduped = []
        dup_count = 0
        for i, c in enumerate(candidates):
            title = c["title_candidate"]
            existing = None
            if self.db and title:
                try:
                    existing = self.db.find_paper_by_title_exact(title, user_id)
                except Exception:
                    pass

            if existing:
                c["is_duplicate"] = True
                c["existing_paper_id"] = existing.get("id", "")
                dup_count += 1
                await self._notify(
                    "fast_dedup", i + 1, total,
                    f"⏭ 重复: {c['title_candidate'][:60]}", i + 1, total,
                )
            else:
                c["is_duplicate"] = False
                fast_deduped.append(c)
                if (i + 1) % 10 == 0:
                    await self._notify(
                        "fast_dedup", i + 1, total,
                        f"快速去重: {i + 1}/{total} ({dup_count} dup)",
                        i + 1, total,
                    )

        return fast_deduped, dup_count

    # ── Phase 3: PDF→MD 转换 ───────────────────────────

    async def _phase3_convert_pdfs(
        self, pdfs: list[dict], project_id: str, total: int,
    ) -> tuple[list[dict], list[dict]]:
        """P3: 批量 PDF→MD 转换。"""
        from ..pdf_converter import PDFConverter

        output_dir = Path(self.oss_dir) / "papers"
        output_dir.mkdir(parents=True, exist_ok=True)
        converter = PDFConverter(max_concurrent=4)

        converted, failed = [], []
        for i, item in enumerate(pdfs):
            pdf_path = Path(item["pdf_path"])
            md_path = await converter.convert(pdf_path, output_dir)
            if md_path:
                item["md_path"] = str(md_path)
                converted.append(item)
                if (i + 1) % 10 == 0:
                    await self._notify(
                        "convert", i + 1, len(pdfs),
                        f"PDF→MD: {i+1}/{len(pdfs)} (ok={len(converted)} fail={len(failed)})",
                        i + 1, len(pdfs),
                    )
            else:
                item["convert_error"] = "转换失败"
                failed.append(item)

        return converted, failed

    # ── Phase 4: 元数据提取 ────────────────────────────

    async def _phase4_extract_metadata(
        self, papers: list[dict], total: int,
    ) -> tuple[list[dict], dict]:
        """P4: 从 MD 前 500 行提取标题/期刊/DOI/作者。"""
        papers_meta = []
        extracted, failed = 0, 0
        for i, p in enumerate(papers):
            md_path = p.get("md_path", "")
            meta = {"pdf_path": p["pdf_path"], "md_path": md_path}

            if md_path:
                try:
                    md_text = Path(md_path).read_text(encoding="utf-8")[:3000]
                    meta.update(self._extract_metadata_from_md(md_text))
                    if meta.get("title", "").strip():
                        extracted += 1
                    else:
                        # Fallback: 用文件名作为 title
                        meta["title"] = p.get("title_candidate", "")
                        failed += 1
                except Exception:
                    meta["title"] = p.get("title_candidate", "")
                    failed += 1
            else:
                meta["title"] = p.get("title_candidate", "")
                failed += 1

            papers_meta.append(meta)

        accuracy = round(extracted / max(len(papers), 1), 3)

        # 混合评估: LLM 抽样 5 篇
        sample_results = []
        if self.llm and len(papers_meta) >= 5:
            sample_results = await self._evaluate_metadata_quality(papers_meta[:5])

        meta_quality = {
            "extracted": extracted, "failed": failed,
            "accuracy": accuracy, "sample_results": sample_results,
        }
        await self._notify(
            "metadata", len(papers), total,
            f"元数据提取: {extracted}/{len(papers)} (准确率 {accuracy:.0%})",
            len(papers), total,
        )
        return papers_meta, meta_quality

    @staticmethod
    def _extract_metadata_from_md(md_text: str) -> dict:
        """从 MD 文本前段提取元数据（纯规则，无外部 API）。"""
        result = {"title": "", "journal": "", "doi": "", "authors": "", "year": 0}

        lines = md_text.split("\n")
        # Rule 1: 第一行非空行 → 候选标题 (优先 heading, 其次普通文本)
        for line in lines[:10]:
            stripped = line.strip()
            if not stripped:
                continue
            # 优先取 # 开头的 heading（docling 通常第一行就是标题）
            if stripped.startswith("#"):
                result["title"] = re.sub(r'^#+\s*', '', stripped)[:500]
                break
        if not result["title"]:
            # Fallback: 取第一个非空普通行
            for line in lines[:10]:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    result["title"] = stripped[:500]
                    break

        # Rule 2: 匹配 DOI
        doi_pat = re.compile(
            r'(?:DOI|doi)\s*[:=]?\s*(10\.\d{4,}/[^\s]+)|'
            r'https?://doi\.org/(10\.\d{4,}/[^\s]+)',
        )
        for line in lines[:100]:
            m = doi_pat.search(line)
            if m:
                result["doi"] = (m.group(1) or m.group(2)).strip()
                break

        # Rule 3: 匹配期刊/会议
        venue_pats = [
            r'(?:Published\sin|Conference|Journal|Proceedings of)\s*[:=]?\s*(.+?)(?:\n|$)',
            r'(?:In\s+Proceedings\s+of)\s+(.+?)(?:\n|$)',
        ]
        for pat in venue_pats:
            for line in lines[:50]:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    result["journal"] = m.group(1).strip()[:200]
                    break
            if result["journal"]:
                break

        # Rule 4: 匹配作者行
        for line in lines[:20]:
            stripped = line.strip()
            if re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+', stripped) and len(stripped) > 10:
                result["authors"] = stripped[:300]
                break

        # Rule 5: 提取年份
        year_pat = re.search(r'\b(19|20)\d{2}\b', md_text[:500])
        if year_pat:
            result["year"] = int(year_pat.group())

        return result

    async def _evaluate_metadata_quality(self, samples: list[dict]) -> list[dict]:
        """LLM 抽样评估元数据提取质量。"""
        if not self.llm:
            return []
        try:
            sample_texts = []
            for s in samples:
                md_path = s.get("md_path", "")
                if md_path:
                    t = Path(md_path).read_text(encoding="utf-8")[:1000]
                else:
                    t = s.get("title", "")
                sample_texts.append(
                    f"提取结果: title={s.get('title','')}, journal={s.get('journal','')}\n"
                    f"原文前段: {t[:300]}",
                )

            prompt = (
                "评估以下论文元数据提取的准确性。对每条记录判断 title 和 journal "
                "是否正确 (correct/incorrect/partial)。返回 JSON。\n\n"
                + "\n\n---\n\n".join(sample_texts)
            )
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
                schema=None,  # free-form
                temperature=0.1,
                node="eval_metadata",
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"元数据质量评估 LLM 调用失败: {e}")
            return []

    # ── Phase 5: 图表提取 ──────────────────────────────

    async def _phase5_extract_figures(
        self, papers: list[dict], papers_meta: list[dict], user_id: str,
    ) -> tuple[list[dict], list[dict]]:
        """P5: 从 PDF 提取图表。Figures→OSS, Tables→MD。

        注意: 为提高效率，跳过已有 figures 目录的论文（幂等）。
        """
        from ..pdf_converter import PDFConverter

        all_figures, all_tables = [], []
        converter = PDFConverter(max_concurrent=2)

        for i, p in enumerate(papers):
            pdf_path = Path(p["pdf_path"])
            paper_title = p.get("title_candidate", pdf_path.stem)
            meta = papers_meta[i] if i < len(papers_meta) else {}
            paper_id = self._paper_id_from_title(meta.get("title", paper_title))

            figures_dir = Path(self.oss_dir) / "figures" / paper_id
            md_dir = Path(self.oss_dir) / "papers"

            if figures_dir.exists() and any(figures_dir.iterdir()):
                continue  # 已提取过，skip

            md_path, figures = await converter.convert_with_figures(
                pdf_path, md_dir, figures_dir,
            )
            if figures:
                # 写入 papers (figures JSONB) 表
                for fig in figures:
                    if self.db:
                        try:
                            self.db.save_paper_figure({
                                "id": fig["id"],
                                "paper_id": paper_id,
                                "caption": fig.get("caption", ""),
                                "figure_type": fig.get("figure_type", "figure"),
                                "local_path": fig.get("local_path", ""),
                                "oss_path": str(figures_dir / f"{fig['id']}.png"),
                                "page_number": fig.get("page_number", 0),
                                "image_hash": fig.get("image_hash", ""),
                            })
                        except Exception:
                            pass
                all_figures.extend(figures)

            # 更新 md_path（convert_with_figures 可能生成新的 MD）
            if md_path:
                p["md_path"] = str(md_path)
                meta["md_path"] = str(md_path)

            # 提取表格（从 MD 文本）
            if md_path and md_path.exists():
                md_text = md_path.read_text(encoding="utf-8")[:10000]
                tables = self._extract_tables_from_md(md_text, paper_id)
                all_tables.extend(tables)

            if (i + 1) % 20 == 0:
                await self._notify(
                    "figures", i + 1, len(papers),
                    f"图表提取: {len(all_figures)} figures, {len(all_tables)} tables",
                    i + 1, len(papers),
                )

        return all_figures, all_tables

    @staticmethod
    def _extract_tables_from_md(md_text: str, paper_id: str) -> list[dict]:
        """从 MD 文本中提取表格。"""
        tables = []
        # 匹配 Markdown 表格 (|...|...|)
        table_pattern = re.compile(r'(\|.+\|[\r\n]+\|[-:\s|]+\|[\r\n]+(?:\|.+\|[\r\n]*)+)')
        for match in table_pattern.finditer(md_text):
            table_text = match.group(1).strip()
            tables.append({
                "paper_id": paper_id,
                "table_text_md": table_text,
                "position": match.start(),
            })
        return tables

    # ── Phase 6: 摘要向量去重 ──────────────────────────

    async def _phase6_vector_dedup(
        self, papers_meta: list[dict], user_id: str,
    ) -> tuple[list[dict], int]:
        """P6: 摘要 embedding → pgvector 余弦相似度去重。

        阈值: cosine ≥ 0.90 视为重复。
        """
        if not self.vector_store:
            return papers_meta, 0

        vector_deduped, dup_count = [], 0
        for i, meta in enumerate(papers_meta):
            title = meta.get("title", "")
            if not title:
                vector_deduped.append(meta)
                continue

            try:
                results = self.vector_store.search_abstract(title, n_results=3)
                if results:
                    best = results[0]
                    similarity = 1 - best.get("distance", 0)
                    if similarity >= 0.90:
                        meta["is_duplicate"] = True
                        meta["vector_dup_of"] = best.get("paper_id", "")
                        dup_count += 1
                        continue
            except Exception:
                pass

            vector_deduped.append(meta)

        return vector_deduped, dup_count

    # ── Phase 7: 入库 + 向量索引 ───────────────────────

    async def _phase7_index_papers(
        self, papers_meta: list[dict], project_id: str, user_id: str,
        figures: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """P7: 论文入库 (DB) + 分块 + 向量索引 (复用已有 ingest 流程)。"""
        from ..chunker import SectionChunker

        indexed, index_errors = [], []
        chunker = SectionChunker()

        for i, meta in enumerate(papers_meta):
            paper_id = self._paper_id_from_title(meta.get("title", ""))
            try:
                # A. 入库 paper 元数据
                if self.db:
                    paper = {
                        "id": paper_id,
                        "title": meta.get("title", ""),
                        "authors": [meta.get("authors", "")] if meta.get("authors") else [],
                        "year": meta.get("year", 0),
                        "abstract": "",
                        "venue": meta.get("journal", ""),
                        "doi": meta.get("doi", ""),
                        "source": "local_pdf",
                        "file_path": meta.get("pdf_path", ""),
                        "md_path": meta.get("md_path", ""),
                        "status": "indexed",
                    }
                    # 读 MD 提取 abstract（前 2000 字符）
                    md_path = meta.get("md_path", "")
                    if md_path and Path(md_path).exists():
                        md_text = Path(md_path).read_text(encoding="utf-8")
                        paper["abstract"] = md_text[:2000]

                    self.db.upsert_paper(paper, user_id=user_id)
                    self.db.link_paper_to_project(project_id, paper_id)

                # B. 分块 + 向量索引
                md_path = meta.get("md_path", "")
                if md_path and Path(md_path).exists() and self.vector_store:
                    md_text = Path(md_path).read_text(encoding="utf-8")
                    chunks = chunker.chunk(md_text, paper_id)
                    if chunks:
                        self.vector_store.add_fulltext_chunks(chunks)

                    # 摘要向量索引
                    title = meta.get("title", "")
                    abstract = paper.get("abstract", md_text[:2000]) if "paper" in dir() else md_text[:2000]
                    self.vector_store.add_paper_abstract(
                        paper_id=paper_id, title=title, abstract=abstract,
                    )

                indexed.append({**meta, "paper_id": paper_id, "status": "indexed"})

                if (i + 1) % 10 == 0:
                    await self._notify(
                        "index", i + 1, len(papers_meta),
                        f"索引入库: {i+1}/{len(papers_meta)}", i + 1, len(papers_meta),
                    )

            except Exception as e:
                logger.error(f"索引失败 [{paper_id}]: {e}")
                index_errors.append({
                    "paper_id": paper_id, "title": meta.get("title", ""),
                    "error": str(e),
                })

        return indexed, index_errors

    # ── Phase 8: PDF 清理 ──────────────────────────────

    async def _phase8_cleanup_pdfs(
        self, papers_meta: list[dict], user_id: str,
    ) -> list[str]:
        """P8: 成功入库的 PDF → OSS archive 目录。"""
        import shutil

        archive_dir = Path(self.oss_dir) / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        cleaned = []
        for meta in papers_meta:
            pdf_path = Path(meta.get("pdf_path", ""))
            if not pdf_path.exists():
                continue

            paper_id = self._paper_id_from_title(meta.get("title", ""))
            oss_pdf_path = archive_dir / f"{paper_id}.pdf"

            try:
                file_size = pdf_path.stat().st_size
                md5_hash = hashlib.md5(pdf_path.read_bytes()).hexdigest()

                shutil.move(str(pdf_path), str(oss_pdf_path))

                if self.db:
                    self.db.archive_pdf(
                        paper_id, str(pdf_path), str(oss_pdf_path),
                        file_size=file_size, md5_hash=md5_hash,
                    )
                cleaned.append(str(oss_pdf_path))
            except Exception as e:
                logger.warning(f"PDF 归档失败 [{paper_id}]: {e}")

        return cleaned

    # ── 工具方法 ───────────────────────────────────────

    @staticmethod
    def _paper_id_from_title(title: str) -> str:
        """从标题生成稳定 paper_id。"""
        import hashlib
        h = hashlib.md5(title.encode() if title else b"unknown").hexdigest()[:8]
        return f"local:{h}"

    # ── 私有辅助方法 ─────────────────────────────────

    async def _chunk_with_vector_store(self, paper: dict) -> dict:
        """使用向量存储做分块（无 runner 时的回退路径）。"""
        from ..chunker import chunk_markdown
        md_path = paper.get("markdown_path", "")
        if md_path:
            from pathlib import Path
            text = Path(md_path).read_text(encoding="utf-8")
            chunks = chunk_markdown(text, paper.get("title", ""))
            return {"success": True, "chunks": chunks}
        return {"success": False, "chunks": []}

    async def _embed_with_vector_store(self, paper: dict, chunks: list) -> dict:
        """使用向量存储生成 embedding（无 runner 回退）。"""
        if not self.vector_store:
            return {"success": False, "count": 0}
        try:
            count = 0
            for chunk in chunks:
                self.vector_store.add_paper_chunk(
                    paper["paper_id"], chunk.get("text", ""),
                    chunk.get("section", ""), chunk.get("index", 0),
                )
                count += 1
            return {"success": True, "count": count}
        except Exception as e:
            logger.warning(f"_embed_with_vector_store failed: {e}")
            return {"success": False, "count": 0}

    async def _dedup_with_vector_store(self, paper_id: str, paper: dict) -> dict:
        """使用向量存储去重检查。

        Returns:
            dict with keys: is_duplicate, duplicate_of, similarity, potential_duplicate
            - score > 0.95 → is_duplicate=True (直接拒)
            - 0.8 < score ≤ 0.95 → potential_duplicate=True (标记待确认)
        """
        if not self.vector_store:
            return {"is_duplicate": False, "similarity": 0.0}
        try:
            title = paper.get("title", "")
            results = self.vector_store.search_similar(title, n_results=3)
            for r in results:
                if r.get("paper_id") != paper_id:
                    score = r.get("score", 0)
                    if score > 0.95:
                        return {
                            "is_duplicate": True,
                            "duplicate_of": r["paper_id"],
                            "similarity": score,
                        }
                    elif score > 0.8:
                        return {
                            "is_duplicate": False,
                            "potential_duplicate": True,
                            "duplicate_of": r["paper_id"],
                            "similarity": score,
                        }
            return {"is_duplicate": False, "similarity": 0.0}
        except Exception as e:
            logger.warning(f"_dedup_with_vector_store failed for {paper_id}: {e}")
            return {"is_duplicate": False, "similarity": 0.0}

    async def _llm_rerank(self, question: str, results: list[dict], top_k: int) -> list[dict]:
        """Cross-Encoder 重排序 — BGE-reranker-v2-m3 (SiliconFlow).

        替代原有 LLM 重排序。失败时返回原始向量排序结果。
        """
        import asyncio

        from ..reranker import RerankError, get_reranker

        if not results or len(results) <= top_k:
            return results

        # 截断到 ~2000 chars (约 512 tokens)
        RERANK_CHAR_LIMIT = 2000
        documents = []
        for r in results:
            title = r.get("title", "")
            text = (r.get("chunk_text") or r.get("abstract") or "")[:RERANK_CHAR_LIMIT]
            documents.append(f"{title} {text}" if title else text)

        try:
            reranker = get_reranker()
            rerank_results = await asyncio.to_thread(
                reranker.rerank, question, documents, top_k=top_k,
            )
        except RerankError as e:
            logger.error(f"KnowledgeAgent rerank 失败 (hard error): {e}")
            return results[:top_k]

        ranked = []
        for rr in rerank_results:
            idx = rr.index
            if 0 <= idx < len(results):
                item = dict(results[idx])
                item["score"] = round(rr.score, 4)
                ranked.append(item)

        return ranked[:top_k]

    def _record_rag_trace(
        self,
        session_id: str,
        query_text: str,
        retrieved_count: int,
        reranked_count: int,
        retrieval_ms: int = 0,
        rerank_ms: int = 0,
        total_ms: int = 0,
        confidence: float = 0.0,
        error_text: str = "",
    ) -> None:
        """Fire-and-forget 写 rag_traces 表."""
        try:
            from ..pgdb import _uuid

            if not self.db:
                return
            trace_id = _uuid("rag")
            self.db.conn.execute(
                """-- INSERT INTO event_logs (merged)
                   (id, session_id, user_id, query_text, retrieved_count,
                    reranked_count, retrieval_ms, rerank_ms, total_ms,
                    confidence, error_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    trace_id,
                    (session_id or "unknown")[:64],
                    "user-default",
                    (query_text or "")[:500],
                    retrieved_count,
                    reranked_count,
                    retrieval_ms,
                    rerank_ms,
                    total_ms,
                    confidence,
                    (error_text or "")[:500],
                ),
            )
            self.db.conn.commit()
        except Exception as e:
            logger.debug(f"Failed to record rag_trace: {e}")

    async def _notify(self, stage: str, index: int, total: int, msg: str,
                       current: int = 0, paper_total: int = 0):
        logger.info(f"  KnowledgeAgent [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, current, paper_total)
            except Exception as e:
                logger.debug(f"KnowledgeAgent on_progress error: {e}")
