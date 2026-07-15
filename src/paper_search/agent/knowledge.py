"""知识库系统 — RAG 问答 + 知识提取 + 知识发现.

三大核心功能:
  1. RAG QA: ChromaDB 检索 → LLM Reranker → LLM 生成 (带引用)
  2. 知识提取: LLM 结构化提取论文贡献/方法/数据集/指标/局限
  3. 知识发现: 研究空白分析、矛盾检测、趋势分析

使用方式:
    from paper_search.agent.knowledge import KnowledgeBase

    kb = KnowledgeBase(db, chroma_store, llm_client)
    answer = await kb.ask("transformer 在医疗图像分割中的最新进展是什么？")
    discoveries = await kb.discover_gaps(domain="medical image segmentation")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import get_outputs_dir

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class RAGResult:
    """RAG 问答结果."""
    question: str
    answer: str
    sources: list[dict] = field(default_factory=list)
    # [{"paper_id": "...", "title": "...", "relevance": 0.9, "snippet": "..."}]
    confidence: float = 0.0
    follow_up_questions: list[str] = field(default_factory=list)


@dataclass
class ExtractedKnowledge:
    """从论文中提取的结构化知识."""
    paper_id: str
    paper_title: str
    contribution: str  # 核心贡献
    method: str  # 方法/技术栈
    datasets: list[str] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)  # {"SOTA": "95.2%", "Benchmark": "ImageNet"}
    limitations: list[str] = field(default_factory=list)
    future_work: list[str] = field(default_factory=list)
    code_url: str = ""
    reading_level: str = "deep"  # skim / normal / deep


@dataclass
class DiscoveryResult:
    """知识发现结果."""
    domain: str
    gaps: list[dict] = field(default_factory=list)
    # [{"topic": "...", "papers_count": 3, "suggestion": "under-explored area"}]
    contradictions: list[dict] = field(default_factory=list)
    # [{"claim_a": "...", "claim_b": "...", "papers": [...]}]
    trends: list[dict] = field(default_factory=list)
    # [{"direction": "increasing", "topic": "...", "growth_rate": "+45%"}]
    emerging_topics: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Knowledge Base
# ═══════════════════════════════════════════════════════════════


class KnowledgeBase:
    """知识库系统 — RAG 问答 + 知识提取 + 知识发现."""

    def __init__(self, db, chroma_store, llm_client):
        self._db = db
        self._chroma = chroma_store
        self._llm = llm_client

    # ── RAG QA ─────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        top_k: int = 5,
        use_fulltext: bool = True,
        project_id: str = None,
    ) -> RAGResult:
        """基于知识库的 RAG 问答.

        Args:
            question: 用户问题
            top_k: 检索论文数
            use_fulltext: 是否使用全文检索 (否则仅摘要)
            project_id: 限制搜索范围到特定项目

        Returns:
            RAGResult with answer and sources
        """
        top_k = top_k or 5  # 防御 None 传入
        # Stage 1: 检索
        if use_fulltext and self._chroma:
            retrieved = self._chroma.search_fulltext(question, n_results=top_k * 2)
        elif self._chroma:
            retrieved = self._chroma.search_similar(question, n_results=top_k * 2)
        else:
            retrieved = []

        if not retrieved:
            return RAGResult(
                question=question,
                answer="知识库中没有找到相关信息。请先导入论文。",
                confidence=0.0,
            )

        # Stage 2: LLM Reranker — 重排序 top-2k → top-k
        candidates = []
        for r in retrieved[:min(top_k * 2, 20)]:
            paper_id = r.get("paper_id", "")
            # 从 DB 获取完整信息
            row = self._db.conn.execute(
                "SELECT * FROM papers WHERE id = ?", (paper_id,)
            ).fetchone()
            if row:
                rd = dict(row)
                candidates.append({
                    "paper_id": paper_id,
                    "title": rd.get("title", ""),
                    "abstract": rd.get("abstract", "") or "",
                    "year": rd.get("year"),
                    "venue": rd.get("venue", ""),
                    "snippet": r.get("text", r.get("document", ""))[:300],
                })

        if not candidates:
            return RAGResult(
                question=question,
                answer="检索到论文但无法获取详细信息。",
                confidence=0.0,
            )

        # Stage 2: Cross-Encoder Rerank
        t_rerank_start = time.monotonic()
        reranked = await self._rerank(question, candidates, top_k)
        t_rerank_end = time.monotonic()

        # Stage 3: LLM 生成答案
        answer, confidence, follow_ups = await self._generate_answer(
            question, reranked
        )
        t_total = time.monotonic()

        # 记录 RAG trace (fire-and-forget)
        self._record_rag_trace(
            session_id="knowledge",
            query_text=question,
            retrieved_count=len(candidates),
            reranked_count=len(reranked),
            retrieval_ms=0,
            rerank_ms=int((t_rerank_end - t_rerank_start) * 1000),
            total_ms=int((t_total - t_rerank_start) * 1000),
            confidence=confidence,
        )

        return RAGResult(
            question=question,
            answer=answer,
            sources=reranked,
            confidence=confidence,
            follow_up_questions=follow_ups,
        )

    async def _rerank(
        self, question: str, candidates: list[dict], top_k: int
    ) -> list[dict]:
        """Cross-Encoder 重排序 — BGE-reranker-v2-m3 (SiliconFlow).

        替代原有 LLM 重排序。RerankError 时返回空列表（降级到无重排序结果）。
        """
        import asyncio

        from .reranker import RerankError, get_reranker

        if not candidates:
            return []

        # 截断到 ~2000 chars (约 512 tokens) 以适配 Cross-Encoder 输入限制
        RERANK_CHAR_LIMIT = 2000
        documents = [
            f"{c['title']} {c.get('abstract', '')[:RERANK_CHAR_LIMIT]}"
            for c in candidates
        ]

        try:
            reranker = get_reranker()
            results = await asyncio.to_thread(
                reranker.rerank, question, documents, top_k=top_k,
            )
        except RerankError as e:
            logger.error(f"Rerank 失败 (hard error): {e}")
            return []

        reranked = []
        for rr in results:
            idx = rr.index
            if 0 <= idx < len(candidates):
                c = dict(candidates[idx])
                c["relevance"] = round(rr.score, 4)
                reranked.append(c)

        return reranked

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
            from .pgdb import _uuid

            trace_id = _uuid("rag")
            self._db.conn.execute(
                """INSERT INTO rag_traces
                   (id, session_id, user_id, query_text, retrieved_count,
                    reranked_count, retrieval_ms, rerank_ms, total_ms,
                    confidence, error_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    trace_id,
                    session_id[:64] if session_id else "unknown",
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
            self._db.conn.commit()
        except Exception as e:
            logger.debug(f"Failed to record rag_trace: {e}")

    async def _generate_answer(
        self, question: str, sources: list[dict]
    ) -> tuple[str, float, list[str]]:
        """LLM 基于检索到的论文生成答案."""
        sources_text = "\n\n".join(
            f"### [{i+1}] {s['title']} ({s.get('year', '?')}) — {s.get('venue', '')}\n"
            f"{s.get('abstract', '')[:500]}\n"
            f"相关片段: {s.get('snippet', '')[:300]}"
            for i, s in enumerate(sources)
        )

        response = await self._llm.chat_json(
            messages=[{"role": "user", "content": (
                f"问题: {question}\n\n"
                f"参考论文:\n{sources_text}\n\n"
                f"请基于以上论文回答问题，并标注引用来源（用 [N] 标注）。"
            )}],
            system="""你是一个学术知识库问答系统。基于提供的论文回答用户问题。

要求:
1. 只基于提供的论文回答问题，不要编造
2. 每条关键声明后标注引用编号 [N]
3. 如果论文之间有不一致，指出来
4. 如果不确定，诚实说明
5. 最后给出2-3个可进一步探索的后续问题

输出纯 JSON:
{
  "answer": "基于论文的回答（Markdown格式，含[N]引用标注）",
  "confidence": 0.85,
  "follow_up_questions": ["后续问题1", "后续问题2"]
}""",
            node="rad_query_answer",
        )

        return (
            response.get("answer", "无法生成答案。"),
            response.get("confidence", 0.5),
            response.get("follow_up_questions", []),
        )

    # ── Knowledge Extraction ────────────────────────────────

    async def extract_knowledge(
        self,
        paper_id: str,
        deep: bool = False,
    ) -> ExtractedKnowledge:
        """从论文中提取结构化知识.

        Args:
            paper_id: 论文 DB ID
            deep: 是否深度提取（读取全文, 否则仅摘要）

        Returns:
            ExtractedKnowledge
        """
        row = self._db.conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Paper not found: {paper_id}")

        rd = dict(row)

        # 构建提取内容
        content_parts = [
            f"标题: {rd.get('title', '')}",
            f"作者: {rd.get('authors', '')}",
            f"年份: {rd.get('year', '?')}",
            f"期刊/会议: {rd.get('venue', '未知')}",
            f"摘要: {rd.get('abstract', '无')[:1000]}",
        ]

        if deep and rd.get("markdown_path"):
            md_path = Path(rd["markdown_path"])
            if md_path.exists():
                md_text = md_path.read_text(encoding="utf-8")
                content_parts.append(f"全文(前4000字): {md_text[:4000]}")

        user_msg = "\n\n".join(content_parts)

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": user_msg}],
                system="""你是论文学术价值提取器。从论文中提取结构化知识。

输出纯 JSON:
{
  "contribution": "这篇论文的核心贡献（2-3句话）",
  "method": "使用的方法/技术栈",
  "datasets": ["数据集1", "数据集2"],
  "metrics": {"关键指标1": "值/描述", "关键指标2": "值/描述"},
  "limitations": ["局限性1", "局限性2"],
  "future_work": ["建议的未来方向1"],
  "code_url": "开源代码链接（如有提及）",
  "reading_level": "deep"
}

阅读等级:
- "skim": 粗略读即可（方法论简单或结果可预期）
- "normal": 正常阅读（有值得关注的贡献）
- "deep": 值得细读（重要贡献或复杂方法）""",
                node="ingest_survey",
            )
        except Exception as e:
            logger.error(f"Knowledge extraction failed: {e}")
            return ExtractedKnowledge(
                paper_id=paper_id,
                paper_title=rd.get("title", ""),
                contribution="",
                method="",
            )

        return ExtractedKnowledge(
            paper_id=paper_id,
            paper_title=rd.get("title", ""),
            contribution=result.get("contribution", ""),
            method=result.get("method", ""),
            datasets=result.get("datasets", []),
            metrics=result.get("metrics", {}),
            limitations=result.get("limitations", []),
            future_work=result.get("future_work", []),
            code_url=result.get("code_url", ""),
            reading_level=result.get("reading_level", "normal"),
        )

    async def extract_batch(
        self, paper_ids: list[str], max_concurrent: int = 5
    ) -> list[ExtractedKnowledge]:
        """批量提取知识."""
        import asyncio
        sem = asyncio.Semaphore(max_concurrent)

        async def extract_one(pid):
            async with sem:
                return await self.extract_knowledge(pid)

        return await asyncio.gather(*[extract_one(pid) for pid in paper_ids])

    # ── Knowledge Discovery ─────────────────────────────────

    async def discover_gaps(
        self,
        domain: str = "",
        project_id: str = None,
    ) -> DiscoveryResult:
        """发现研究空白 — 分析知识库中哪些方向覆盖不足.

        Args:
            domain: 研究领域（用于聚焦分析）
            project_id: 限定分析范围

        Returns:
            DiscoveryResult with gaps, contradictions, trends
        """
        # 获取相关知识
        papers = []
        if project_id:
            papers = self._db.get_project_papers(project_id)
        else:
            # 从全文搜索
            rows = self._db.conn.execute(
                "SELECT * FROM papers ORDER BY year DESC LIMIT 200"
            ).fetchall()
            papers = [dict(r) for r in rows]

        if len(papers) < 10:
            return DiscoveryResult(domain=domain)

        # 构建分析上下文
        papers_summary = "\n".join(
            f"- [{i}] {p.get('title', '')} ({p.get('year', '?')}) | {p.get('venue', '')} | {p.get('unified_level', '未评级')}"
            for i, p in enumerate(papers[:100])
        )

        domain_context = f"领域: {domain}" if domain else ""

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": (
                    f"{domain_context}\n"
                    f"论文列表 (共 {len(papers)} 篇):\n{papers_summary}\n\n"
                    f"请分析这个研究领域的知识空白、矛盾和趋势。"
                )}],
                system="""你是一个研究趋势分析器。分析论文集合，发现:

1. 研究空白 (gaps): 哪些子方向论文数量少但有研究价值？
2. 矛盾发现 (contradictions): 不同论文的结论是否冲突？
3. 趋势识别 (trends): 哪些方向论文在增多？哪些在减少？
4. 新兴话题 (emerging_topics): 最近2年出现的新方向

输出纯 JSON:
{
  "gaps": [
    {"topic": "子方向描述", "papers_count": 3, "total_relevant": 50, "suggestion": "该方向论文仅3篇，但相关度高，值得深入"}
  ],
  "contradictions": [
    {"claim_a": "论文A的发现", "claim_b": "论文B的相反发现", "paper_ids": ["id1", "id2"]}
  ],
  "trends": [
    {"direction": "increasing", "topic": "方向名", "description": "2024-2026年增长45%"}
  ],
  "emerging_topics": ["新兴话题1", "新兴话题2"]
}""",
                node="gap_discovery",
            )
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return DiscoveryResult(domain=domain)

        return DiscoveryResult(
            domain=domain,
            gaps=result.get("gaps", []),
            contradictions=result.get("contradictions", []),
            trends=result.get("trends", []),
            emerging_topics=result.get("emerging_topics", []),
        )

    async def find_related(
        self,
        paper_id: str,
        top_k: int = 10,
        use_citations: bool = True,
    ) -> list[dict]:
        """发现相关论文 — 结合语义相似度 + 引用关系.

        Args:
            paper_id: 种子论文 ID
            top_k: 返回数量
            use_citations: 是否使用引用关系

        Returns:
            相关论文列表
        """
        related = []

        # 语义相似度
        row = self._db.conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if row and self._chroma:
            rd = dict(row)
            query_text = f"{rd.get('title', '')} {rd.get('abstract', '')[:500]}"
            results = self._chroma.search_similar(query_text, n_results=top_k + 1)
            for r in results:
                pid = r.get("paper_id", "")
                if pid and pid != paper_id:
                    related.append({
                        "paper_id": pid,
                        "method": "semantic",
                        "distance": r.get("distance", 1.0),
                    })

        # 引用关系
        if use_citations:
            citing = self._db.get_citations(paper_id, direction="incoming")
            for c in citing[:top_k]:
                if c.get("target_paper_id") not in [r["paper_id"] for r in related]:
                    related.append({
                        "paper_id": c.get("target_paper_id", ""),
                        "method": "citation",
                        "target_title": c.get("target_title", ""),
                    })

        return related[:top_k]

    # ── Auto-Update Survey ──────────────────────────────────

    async def update_survey(
        self, project_id: str, new_papers_since: str = None
    ) -> str:
        """当知识库有新论文时自动更新综述报告.

        Args:
            project_id: 项目 ID
            new_papers_since: ISO 日期，只考虑此日期后的新论文

        Returns:
            更新后的综述 Markdown
        """
        project = self._db.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")

        papers = self._db.get_project_papers(project_id, relevant_only=True)
        if not papers:
            papers = self._db.get_project_papers(project_id)

        if new_papers_since:
            papers = [p for p in papers
                     if p.get("first_seen_at", "") >= new_papers_since]

        if not papers:
            return "没有找到需要更新的论文。"

        # 生成更新后的综述
        user_query = project.get("user_query", "")

        paper_items = []
        for p in papers[:50]:
            judgment = p.get("relevance_score", 0.5)
            paper_items.append(
                f"- [{judgment:.2f}] {p.get('title', '')} ({p.get('year', '?')}) "
                f"| {p.get('source', '')} | {p.get('venue', '')}"
            )

        # L2 反幻觉：传 db + project_id 让 generate_report 走 CitationVerifier
        report = await self._llm.generate_report(
            user_query, papers, judgments=[],
            db=self._db, project_id=project_id,
        )

        # 保存
        out_dir = get_outputs_dir(project_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "survey.md"
        report_path.write_text(report, encoding="utf-8")

        self._db.update_project(project_id, report_path=str(report_path))

        return report
