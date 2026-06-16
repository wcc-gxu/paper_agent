"""Celery 异步任务 — 重量操作（download/convert/index/survey）。

每个 task 承担：
  1. 执行实际操作
  2. 更新 AgentDB 中的状态
  3. 通过 Reporter 向主 Agent 报告进度
  4. 失败时记录错误日志（重试 1 次）
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .celery_app import app

logger = logging.getLogger(__name__)


def _get_db():
    from .db import AgentDB
    return AgentDB()


def _get_reporter():
    from .reporter import Reporter
    return Reporter(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _get_logger(task_id: str):
    from .task_logger import TaskLogger
    log_dir = Path.home() / ".paper_search" / "logs" / "tasks"
    return TaskLogger(log_dir, task_id)


# ═══════════════════════════════════════════════════════════════
# Download Task
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def download_task(self, paper_id: str, project_id: str,
                  title: str = "", source: str = "arxiv") -> dict:
    """下载单篇论文 PDF。

    失败自动重试 1 次（换来源），仍失败则记录 unavailable。

    Returns:
        {"paper_id": str, "success": bool, "local_path": str, "error": str}
    """
    task_id = self.request.id
    task_logger = _get_logger(task_id)
    reporter = _get_reporter()

    task_logger.paper_progress(task_id, "download", paper_id, title, "download_start")

    try:
        from ..engine import PaperSearchEngine
        from ..config import Config
        from ..models import Paper, SourceType
        from .db import AgentDB

        engine = PaperSearchEngine(Config())
        db = AgentDB()

        # 构建 Paper 对象
        source_type = SourceType(source) if source in [s.value for s in SourceType] else SourceType.ARXIV
        paper = Paper(
            title=title or paper_id,
            authors=[],
            year=None,
            abstract="",
            source=source_type,
        )

        # 执行下载
        result = engine.download_sync(paper, None)
        # download_sync is sync wrapper. We import and call asyncio version instead:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            dl_result = loop.run_until_complete(engine.download(paper))
        finally:
            loop.close()

        if dl_result.success and dl_result.local_path:
            db.mark_pdf_downloaded(project_id, paper_id, str(dl_result.local_path))
            task_logger.paper_progress(task_id, "download", paper_id, title, "download_done")
            reporter.report_progress(task_id, "normal", {
                "stage": "download", "paper_id": paper_id, "status": "done",
            })
            return {"paper_id": paper_id, "success": True, "local_path": str(dl_result.local_path), "error": ""}

        # 下载失败
        error_msg = dl_result.error or "Unknown download error"
        raise Exception(error_msg)

    except Exception as e:
        error_str = str(e)
        logger.warning(f"Download failed for {paper_id}: {error_str}")

        if self.request.retries < self.max_retries:
            task_logger.paper_progress(task_id, "download", paper_id, title, "download_start")
            reporter.report_progress(task_id, "normal", {
                "stage": "download", "paper_id": paper_id, "status": "retrying",
                "retry": self.request.retries + 1,
            })
            raise self.retry(exc=e)

        # 最终失败
        task_logger.paper_progress(task_id, "download", paper_id, title, "download_failed")
        reporter.report_progress(task_id, "normal", {
            "stage": "download", "paper_id": paper_id, "status": "failed",
            "error": error_str,
        })
        return {"paper_id": paper_id, "success": False, "local_path": "", "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Convert Task
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=10)
def convert_task(self, paper_id: str, pdf_path: str,
                 title: str = "", project_id: str = "") -> dict:
    """PDF → Markdown。

    使用 pymupdf4llm 转换，输出到 ~/papers/markdown/{project_id}/。

    Returns:
        {"paper_id": str, "success": bool, "markdown_path": str, "error": str}
    """
    task_id = self.request.id
    task_logger = _get_logger(task_id)
    reporter = _get_reporter()

    task_logger.paper_progress(task_id, "convert", paper_id, title, "convert_start")

    try:
        from .pdf_converter import PDFConverter
        from .db import AgentDB
        from ..config import get_markdown_dir

        pdf = Path(pdf_path)
        if not pdf.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        output_dir = get_markdown_dir() / (project_id or "default")
        output_dir.mkdir(parents=True, exist_ok=True)

        converter = PDFConverter(max_concurrent=1)
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            md_path = loop.run_until_complete(converter.convert(pdf, output_dir))
        finally:
            loop.close()

        if md_path:
            db = AgentDB()
            db.update_paper_meta(paper_id, markdown_path=str(md_path))
            task_logger.paper_progress(task_id, "convert", paper_id, title, "convert_done")
            reporter.report_progress(task_id, "normal", {
                "stage": "convert", "paper_id": paper_id, "status": "done",
            })
            return {"paper_id": paper_id, "success": True, "markdown_path": str(md_path), "error": ""}

        raise Exception("PDF conversion returned None")

    except Exception as e:
        error_str = str(e)
        logger.warning(f"Convert failed for {paper_id}: {error_str}")

        if self.request.retries < self.max_retries:
            task_logger.paper_progress(task_id, "convert", paper_id, title, "convert_start")
            raise self.retry(exc=e)

        task_logger.paper_progress(task_id, "convert", paper_id, title, "convert_failed")
        reporter.report_progress(task_id, "normal", {
            "stage": "convert", "paper_id": paper_id, "status": "failed", "error": error_str,
        })
        return {"paper_id": paper_id, "success": False, "markdown_path": "", "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Index Task
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=10)
def index_task(self, paper_id: str, markdown_path: str,
               title: str = "", abstract: str = "",
               year: int = None, source: str = "", venue: str = "") -> dict:
    """Markdown → ChromaDB（摘要 + 全文分块）。

    Returns:
        {"paper_id": str, "success": bool, "chunks": int, "error": str}
    """
    task_id = self.request.id
    task_logger = _get_logger(task_id)
    reporter = _get_reporter()

    task_logger.paper_progress(task_id, "index", paper_id, title, "index_start")

    try:
        from .chroma_store import ChromaStoreV2
        from .db import AgentDB

        md_path = Path(markdown_path)
        if not md_path.exists():
            raise FileNotFoundError(f"Markdown not found: {markdown_path}")

        chroma = ChromaStoreV2()
        md_content = md_path.read_text(encoding="utf-8")

        # 索引摘要
        chroma.add_abstracts_batch([{
            "paper_id": paper_id,
            "title": title,
            "abstract": abstract or md_content[:500],
            "year": year,
            "source": source,
            "venue": venue,
        }])

        # 索引全文分块
        from .chunker import SectionChunker
        chunker = SectionChunker()
        chunks = chunker.chunk(paper_id, md_content)
        chunk_count = chroma.add_fulltext_chunks(chunks) if chunks else 0

        db = AgentDB()
        db.update_paper_meta(paper_id, embedding_id=f"idx:{paper_id}")

        task_logger.paper_progress(task_id, "index", paper_id, title, "index_done")
        reporter.report_progress(task_id, "normal", {
            "stage": "index", "paper_id": paper_id, "status": "done", "chunks": chunk_count,
        })
        return {"paper_id": paper_id, "success": True, "chunks": chunk_count, "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.warning(f"Index failed for {paper_id}: {error_str}")

        if self.request.retries < self.max_retries:
            task_logger.paper_progress(task_id, "index", paper_id, title, "index_start")
            raise self.retry(exc=e)

        task_logger.paper_progress(task_id, "index", paper_id, title, "index_failed")
        reporter.report_progress(task_id, "normal", {
            "stage": "index", "paper_id": paper_id, "status": "failed", "error": error_str,
        })
        return {"paper_id": paper_id, "success": False, "chunks": 0, "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Survey Task
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def survey_task(self, project_id: str, user_query: str) -> dict:
    """LLM 生成文献综述报告。

    Returns:
        {"project_id": str, "survey_path": str, "error": str}
    """
    task_id = self.request.id
    task_logger = _get_logger(task_id)
    reporter = _get_reporter()

    try:
        from .llm_client_v2 import LLMClientV2
        from .db import AgentDB
        from ..config import get_outputs_dir

        db = AgentDB()
        papers_data = db.get_relevant_papers(project_id)

        if not papers_data:
            return {"project_id": project_id, "survey_path": "", "error": "No relevant papers found"}

        llm = LLMClientV2()

        # 构造论文列表
        papers = []
        for p in papers_data:
            papers.append({
                "title": p.get("title", ""),
                "authors": p.get("authors", "[]"),
                "year": p.get("year"),
                "abstract": p.get("abstract", ""),
                "venue": p.get("venue", ""),
            })

        report = llm.generate_report_sync(user_query, papers)
        # generate_report is async, use sync wrapper:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            report = loop.run_until_complete(llm.generate_report(user_query, papers, []))
        finally:
            loop.close()

        output_dir = get_outputs_dir() / project_id
        output_dir.mkdir(parents=True, exist_ok=True)
        survey_path = output_dir / "survey.md"
        survey_path.write_text(report, encoding="utf-8")

        db.update_project(project_id, report_path=str(survey_path), status="completed")

        task_logger.stage_done(task_id, "survey", {"survey_path": str(survey_path)})
        reporter.report_done(task_id, {"survey_path": str(survey_path)})
        return {"project_id": project_id, "survey_path": str(survey_path), "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.error(f"Survey generation failed for {project_id}: {error_str}")
        task_logger.task_error(task_id, error_str, "")
        reporter.report_error(task_id, error_str)
        return {"project_id": project_id, "survey_path": "", "error": error_str}
