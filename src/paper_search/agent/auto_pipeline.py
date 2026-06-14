"""一键自动搜集入库 — 端到端自动化研究流水线.

用户一个提示词 → 自动完成: 搜索 → 评估 → 下载 → 转换 → 索引 → 知识提取 → 入库

流水线阶段:
  Stage 1: 搜索 (多源并发搜索 + 去重)
  Stage 2: 评估 (LLM 批量相关性打分)
  Stage 3: 下载 (高相关论文 PDF 下载)
  Stage 4: 转换 (PDF → Markdown)
  Stage 5: 索引 (ChromaDB 双 Collection)
  Stage 6: 知识提取 (LLM 结构化提取 → 知识库)
  Stage 7: 汇总 (统计 + 清理 + 报告)

使用方式:
    from paper_search.agent.auto_pipeline import AutoPipeline

    pipeline = AutoPipeline(db, engine, llm_client)
    result = await pipeline.run(
        keywords="transformer attention mechanism",
        sources=["arxiv", "semantic_scholar"],
        year_from=2022,
        year_to=2026,
        max_papers=30,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import get_papers_dir, get_markdown_dir

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════


class PipelineStage(str, Enum):
    SEARCH = "search"
    EVALUATE = "evaluate"
    DOWNLOAD = "download"
    CONVERT = "convert"
    INDEX = "index"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"


@dataclass
class PipelineProgress:
    """流水线进度."""
    stage: PipelineStage
    stage_index: int
    total_stages: int
    message: str
    detail: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class PipelineResult:
    """流水线结果."""
    project_id: str
    keywords: str
    total_found: int
    total_relevant: int
    total_downloaded: int
    total_converted: int
    total_indexed: int
    total_knowledge_extracted: int
    elapsed_seconds: float
    errors: list[str] = field(default_factory=list)
    paper_ids: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Auto Pipeline
# ═══════════════════════════════════════════════════════════════


class AutoPipeline:
    """一键自动搜集入库流水线.

    参数:
        db: AgentDB 实例
        engine: PaperSearchEngine 实例
        llm: LLMClient 或 LLMClientV2 实例
        chroma_store: ChromaStoreV2 实例 (可选, 自动创建)
        max_concurrent_downloads: 最大并发下载数
    """

    STAGES = [
        PipelineStage.SEARCH,
        PipelineStage.EVALUATE,
        PipelineStage.DOWNLOAD,
        PipelineStage.CONVERT,
        PipelineStage.INDEX,
        PipelineStage.EXTRACT,
        PipelineStage.SUMMARIZE,
    ]

    def __init__(
        self,
        db,
        engine,
        llm_client,
        chroma_store=None,
        max_concurrent_downloads: int = 4,
    ):
        self._db = db
        self._engine = engine
        self._llm = llm_client
        self._chroma = chroma_store
        self._max_concurrent_dl = max_concurrent_downloads

    async def run(
        self,
        keywords: str,
        sources: list[str] = None,
        title: str = None,
        author: str = None,
        year_from: int = None,
        year_to: int = None,
        max_papers: int = 30,
        relevance_threshold: float = 0.5,
        auto_download: bool = True,
        auto_extract_knowledge: bool = True,
        project_id: str = None,
        on_progress: Callable = None,
    ) -> PipelineResult:
        """执行完整的自动化流水线.

        Args:
            keywords: 搜索关键词 (支持 AND/OR)
            sources: 搜索来源列表
            title: 按标题搜索
            author: 按作者筛选
            year_from: 起始年份
            year_to: 截止年份
            max_papers: 最大处理论文数
            relevance_threshold: 相关性阈值 (≥此值视为相关)
            auto_download: 是否自动下载 PDF
            auto_extract_knowledge: 是否自动提取知识
            project_id: 项目 ID (不提供则自动创建)
            on_progress: 进度回调

        Returns:
            PipelineResult
        """
        t0 = time.time()
        errors = []

        from ..models import SearchQuery, SourceType

        source_list = [SourceType(s) for s in (sources or ["arxiv", "semantic_scholar"])]

        # 创建项目
        pid = project_id or self._db.create_project(user_query=keywords)
        logger.info(f"AutoPipeline started: project={pid}, keywords='{keywords}'")

        # ── Stage 1: Search ────────────────────────────
        await self._report(on_progress, PipelineStage.SEARCH, 0, "开始搜索...")

        query = SearchQuery(
            keywords=keywords, title=title, author=author,
            year_from=year_from, year_to=year_to,
            max_results=max(20, max_papers // len(source_list)),
            sources=source_list,
        )

        search_result = await self._engine.search(query)
        papers = search_result.papers[:max_papers]

        # 写入 DB
        paper_ids = []
        for p in papers:
            paper_id = self._db.upsert_paper(p)
            self._db.link_paper_to_project(pid, paper_id)
            paper_ids.append(paper_id)

        await self._report(on_progress, PipelineStage.SEARCH, 1,
                           f"搜索完成: {len(papers)} 篇论文",
                           {"total": len(papers), "errors": search_result.errors})

        if not papers:
            return PipelineResult(pid, keywords, 0, 0, 0, 0, 0, 0, time.time() - t0, search_result.errors)

        # ── Stage 2: Evaluate ──────────────────────────
        await self._report(on_progress, PipelineStage.EVALUATE, 2, "LLM 评估相关性...")

        try:
            judgments = await self._llm.evaluate_batch(papers, keywords, max_concurrent=5)
            for p, j in zip(papers, judgments):
                self._db.link_paper_to_project(pid, self._db._paper_id(p),
                                               relevance_score=j.score,
                                               relevance_reason=j.reason)
        except Exception as e:
            errors.append(f"评估失败: {e}")
            judgments = []

        relevant_papers = [
            (p, j) for p, j in zip(papers, judgments)
            if j.score >= relevance_threshold
        ] if judgments else [(p, None) for p in papers]

        await self._report(on_progress, PipelineStage.EVALUATE, 2,
                           f"评估完成: {len(relevant_papers)}/{len(papers)} 相关",
                           {"relevant": len(relevant_papers), "total": len(papers)})

        # ── Stage 3: Download ──────────────────────────
        downloaded = 0
        if auto_download and relevant_papers:
            await self._report(on_progress, PipelineStage.DOWNLOAD, 3,
                               f"下载 PDF ({len(relevant_papers)} 篇)...")

            for p, j in relevant_papers:
                try:
                    dl = await self._engine.download(p, target_dir=get_papers_dir())
                    paper_id = self._db._paper_id(p)
                    if dl.success:
                        self._db.mark_pdf_downloaded(pid, paper_id, str(dl.local_path))
                        self._db.update_paper_meta(paper_id, pdf_path=str(dl.local_path))
                        downloaded += 1
                except Exception as e:
                    errors.append(f"下载失败 [{p.title[:50]}]: {e}")

        await self._report(on_progress, PipelineStage.DOWNLOAD, 3,
                           f"下载完成: {downloaded} 篇",
                           {"downloaded": downloaded})

        # ── Stage 4: Convert ───────────────────────────
        converted = 0
        if downloaded > 0:
            await self._report(on_progress, PipelineStage.CONVERT, 4, "PDF → Markdown 转换...")

            from .pdf_converter import PDFConverter
            converter = PDFConverter(max_concurrent=2)
            output_dir = get_markdown_dir()
            output_dir.mkdir(parents=True, exist_ok=True)

            for p, _ in relevant_papers:
                try:
                    paper_id = self._db._paper_id(p)
                    # 查找 PDF 路径
                    rows = self._db.conn.execute(
                        "SELECT pdf_path FROM project_papers WHERE project_id=? AND paper_id=? AND pdf_downloaded=1",
                        (pid, paper_id),
                    ).fetchall()
                    for row in rows:
                        pdf_path_str = row["pdf_path"] if "pdf_path" in row.keys() else row[0]
                        if pdf_path_str:
                            pdf_path = Path(pdf_path_str)
                            if pdf_path.exists():
                                md_path = await converter.convert(pdf_path, output_dir)
                                if md_path:
                                    self._db.update_paper_meta(paper_id, markdown_path=str(md_path))
                                    converted += 1
                except Exception as e:
                    errors.append(f"转换失败 [{p.title[:50]}]: {e}")

        await self._report(on_progress, PipelineStage.CONVERT, 4,
                           f"转换完成: {converted} 篇",
                           {"converted": converted})

        # ── Stage 5: Index ─────────────────────────────
        indexed = 0
        if converted > 0:
            await self._report(on_progress, PipelineStage.INDEX, 5, "索引到 ChromaDB...")

            from .chroma_store import ChromaStoreV2
            from .chunker import SectionChunker
            store = self._chroma or ChromaStoreV2()

            for p, _ in relevant_papers:
                try:
                    paper_id = self._db._paper_id(p)
                    row = self._db.conn.execute(
                        "SELECT * FROM papers WHERE id=?", (paper_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    rd = dict(row)

                    # 摘要索引
                    if rd.get("title") and (rd.get("abstract") or ""):
                        store.add_paper_abstract(
                            paper_id=paper_id,
                            title=rd["title"],
                            abstract=rd.get("abstract", ""),
                            metadata={"year": rd.get("year"), "source": rd.get("source"),
                                      "venue": rd.get("venue")},
                        )

                    # 全文索引
                    md_path = rd.get("markdown_path")
                    if md_path and Path(md_path).exists():
                        md_text = Path(md_path).read_text(encoding="utf-8")
                        chunks = SectionChunker().chunk(md_text, paper_id)
                        if chunks:
                            store.add_fulltext_chunks(chunks)

                    self._db.update_paper_meta(paper_id, embedding_id=f"chroma:{paper_id}")
                    indexed += 1
                except Exception as e:
                    errors.append(f"索引失败 [{p.title[:50]}]: {e}")

        await self._report(on_progress, PipelineStage.INDEX, 5,
                           f"索引完成: {indexed} 篇",
                           {"indexed": indexed})

        # ── Stage 6: Knowledge Extraction ──────────────
        knowledge_extracted = 0
        if auto_extract_knowledge and indexed > 0:
            await self._report(on_progress, PipelineStage.EXTRACT, 6, "知识提取...")

            from .memory import LongTermMemory, KnowledgeEntry
            kb = LongTermMemory(self._db, self._chroma)

            for p, _ in relevant_papers[:min(20, len(relevant_papers))]:
                try:
                    digest = await self._llm.extract_digest(p)
                    if digest and digest.get("one_liner"):
                        entry = KnowledgeEntry(
                            id="",
                            title=f"Contribution: {p.title[:60]}",
                            content=digest.get("one_liner", ""),
                            category="contribution",
                            source_paper_id=self._db._paper_id(p),
                            source_paper_title=p.title,
                        )
                        kb.add_knowledge(entry)

                        # 方法标签作为单独知识条目
                        for tag in digest.get("method_tags", [])[:3]:
                            kb.add_knowledge(KnowledgeEntry(
                                id="",
                                title=f"Method: {tag}",
                                content=f"Paper: {p.title}\nMethod: {tag}",
                                category="method",
                                source_paper_id=self._db._paper_id(p),
                                source_paper_title=p.title,
                            ))

                        knowledge_extracted += 1
                except Exception as e:
                    errors.append(f"知识提取失败 [{p.title[:50]}]: {e}")

        await self._report(on_progress, PipelineStage.EXTRACT, 6,
                           f"知识提取完成: {knowledge_extracted} 篇",
                           {"knowledge_extracted": knowledge_extracted})

        # ── Stage 7: Summarize ─────────────────────────
        elapsed = time.time() - t0
        await self._report(on_progress, PipelineStage.SUMMARIZE, 7,
                           f"流水线完成 ({elapsed:.1f}s)",
                           {"elapsed": elapsed})

        # 更新项目
        self._db.update_project(pid,
                                total_papers_found=len(papers),
                                total_relevant=len(relevant_papers),
                                total_downloaded=downloaded,
                                status="completed")

        return PipelineResult(
            project_id=pid,
            keywords=keywords,
            total_found=len(papers),
            total_relevant=len(relevant_papers),
            total_downloaded=downloaded,
            total_converted=converted,
            total_indexed=indexed,
            total_knowledge_extracted=knowledge_extracted,
            elapsed_seconds=elapsed,
            errors=errors,
            paper_ids=paper_ids,
        )

    async def _report(self, on_progress, stage: PipelineStage, idx: int,
                      msg: str, detail: dict = None):
        """报告进度."""
        logger.info(f"[{stage.value}] {msg}")
        if on_progress:
            await on_progress(PipelineProgress(
                stage=stage,
                stage_index=idx,
                total_stages=len(self.STAGES),
                message=msg,
                detail=detail or {},
            ))


# ═══════════════════════════════════════════════════════════════
# Subscription Pipeline
# ═══════════════════════════════════════════════════════════════


class SubscriptionPipeline:
    """研究方向订阅 — 定时轮询 + 新论文推送.

    使用方式:
        sub = SubscriptionPipeline(db, engine, llm_client)
        await sub.check_and_push(subscription_id)
    """

    def __init__(self, db, engine, llm_client):
        self._db = db
        self._engine = engine
        self._llm = llm_client
        self._auto = AutoPipeline(db, engine, llm_client)

    async def check_and_push(self, subscription_id: str) -> dict:
        """检查订阅方向的新论文并推送.

        Args:
            subscription_id: 订阅 ID

        Returns:
            {"new_papers": N, "pushed": bool, "details": [...]}
        """
        # 从 DB 加载订阅配置
        row = self._db.conn.execute(
            "SELECT * FROM user_profile WHERE key = ?",
            (f"subscription:{subscription_id}",),
        ).fetchone()

        if row is None:
            return {"error": f"订阅不存在: {subscription_id}"}

        config = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        keywords = config.get("keywords", "")
        sources = config.get("sources", ["arxiv", "semantic_scholar"])
        last_check = config.get("last_check", "")

        # 搜索最近一周的新论文
        from datetime import timedelta
        from ..models import SearchQuery, SourceType

        source_list = [SourceType(s) for s in sources]
        query = SearchQuery(
            keywords=keywords,
            sources=source_list,
            year_from=datetime.now().year,
            max_results=10,
        )
        result = await self._engine.search(query)

        # 过滤新论文（上次检查之后发表的）
        new_papers = []
        if last_check:
            last_date = datetime.fromisoformat(last_check)
            new_papers = [p for p in result.papers
                         if p.year and p.year >= last_date.year]

        if new_papers:
            # 快速入库
            await self._auto.run(
                keywords=keywords,
                sources=sources,
                max_papers=len(new_papers),
                auto_download=False,
                auto_extract_knowledge=False,
            )

        # 更新最后检查时间
        config["last_check"] = datetime.now(timezone.utc).isoformat()
        self._db.conn.execute(
            "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?,?,?)",
            (f"subscription:{subscription_id}",
             json.dumps(config, ensure_ascii=False),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._db.conn.commit()

        return {
            "subscription_id": subscription_id,
            "new_papers": len(new_papers),
            "pushed": len(new_papers) > 0,
            "details": [{"title": p.title, "year": p.year, "source": str(p.source)}
                        for p in new_papers],
        }
