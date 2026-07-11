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
                 runner=None, on_progress=None):
        """
        Args:
            db: AgentDB / PostgresAgentDB 实例
            vector_store: ChromaStoreV2 / PgVectorStore 实例
            llm_client: LLMClientV2 实例
            runner: PipelineRunner 实例（有 runner 时走 runner 方法）
            on_progress: 进度回调
        """
        self.db = db
        self.vector_store = vector_store
        self.llm = llm_client
        self.runner = runner
        self._on_progress = on_progress
        self._ingest_graph = None
        self._query_graph = None

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
        top_k = state.get("top_k", 5)
        use_fulltext = state.get("use_fulltext", True)
        project_id = state.get("project_id")

        await self._notify("检索中", 3, 5, "向量检索相关知识")

        try:
            if self.vector_store:
                # 使用向量存储直接检索
                results = self.vector_store.search_similar(
                    question, n_results=top_k * 2,
                )
                # 如果有 LLM，做 rerank
                if self.llm and results:
                    results = await self._llm_rerank(question, results, top_k)

                sources = [{
                    "paper_id": r.get("paper_id", ""),
                    "title": r.get("title", ""),
                    "relevance": r.get("score", 0.5),
                    "snippet": (r.get("chunk_text") or r.get("abstract") or "")[:500],
                } for r in results[:top_k]]

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
            logger.error(f"KnowledgeAgent search failed: {e}")
            return {"error": str(e)}

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
                response = await self.llm.chat(prompt)
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
        """使用向量存储去重检查。"""
        if not self.vector_store:
            return {"is_duplicate": False}
        try:
            title = paper.get("title", "")
            results = self.vector_store.search_similar(title, n_results=3)
            for r in results:
                if r.get("paper_id") != paper_id:
                    score = r.get("score", 0)
                    if score > 0.95:
                        return {"is_duplicate": True, "duplicate_of": r["paper_id"]}
            return {"is_duplicate": False}
        except Exception:
            return {"is_duplicate": False}

    async def _llm_rerank(self, question: str, results: list[dict], top_k: int) -> list[dict]:
        """LLM Reranker — 对检索结果重排序。"""
        if not self.llm or len(results) <= top_k:
            return results
        try:
            items_text = "\n".join([
                f"[{i}] {r.get('title','')}: {(r.get('chunk_text') or r.get('abstract',''))[:200]}"
                for i, r in enumerate(results[:top_k * 2])
            ])
            prompt = (
                f"Rank these papers by relevance to: {question}\n\n"
                f"{items_text}\n\n"
                f"Return the indices of the top {top_k} most relevant papers, "
                f"one per line, like: [0]\n[3]\n[1]"
            )
            response = await self.llm.chat(prompt)
            # 简单解析
            import re
            indices = [int(m.group(1)) for m in re.finditer(r'\[(\d+)\]', response)]
            ranked = [results[i] for i in indices if i < len(results)]
            return ranked[:top_k] if ranked else results[:top_k]
        except Exception:
            return results[:top_k]

    async def _notify(self, stage: str, index: int, total: int, msg: str,
                       current: int = 0, paper_total: int = 0):
        logger.info(f"  KnowledgeAgent [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, current, paper_total)
            except Exception:
                pass
