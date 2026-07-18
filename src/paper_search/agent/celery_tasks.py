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
    from .pgdb import PostgresAgentDB
    return PostgresAgentDB()


def _get_reporter():
    from .reporter import Reporter
    agent_id = os.getenv("AGENT_ID", "agent-001")
    return Reporter(os.getenv("REDIS_URL", "redis://localhost:6379/0"), agent_id=agent_id)


def _get_logger(task_id: str, agent_type: str = "ingest"):
    from .task_logger import TaskLogger
    log_dir = Path.home() / ".paper_search" / "logs" / "sub_agents" / agent_type
    return TaskLogger(log_dir, task_id)


# ═══════════════════════════════════════════════════════════════
# Download Task
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def download_task(self, paper_id: str, project_id: str,
                  title: str = "", source: str = "arxiv",
                  agent_task_id: str = "", paper_index: int = 0,
                  paper_total: int = 0) -> dict:
    """下载单篇论文 PDF。

    失败自动重试 1 次（换来源），仍失败则记录 unavailable。

    Returns:
        {"paper_id": str, "success": bool, "local_path": str, "error": str}
    """
    task_id = self.request.id
    task_logger = _get_logger(agent_task_id or task_id)
    reporter = _get_reporter()

    # Pub/Sub 实时报告: 下载开始
    if agent_task_id:
        reporter.publish_report(agent_task_id, "ingest", "download",
                               paper_index=paper_index, paper_total=paper_total,
                               paper_id=paper_id, status="start")

    task_logger.paper_progress(agent_task_id or task_id, "download", paper_id, title, "download_start")

    try:
        from ..engine import PaperSearchEngine
        from ..config import Config
        from ..models import Paper, SourceType
        from .pgdb import PostgresAgentDB

        engine = PaperSearchEngine(Config())
        db = PostgresAgentDB()

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
        from .pgdb import PostgresAgentDB
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
            db = PostgresAgentDB()
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
        from .pgvector_store import PgVectorStore
        from .pgdb import PostgresAgentDB

        md_path = Path(markdown_path)
        if not md_path.exists():
            raise FileNotFoundError(f"Markdown not found: {markdown_path}")

        chroma = PgVectorStore()
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
        chunks = chunker.chunk(md_content, paper_id)
        chunk_count = chroma.add_fulltext_chunks(chunks) if chunks else 0

        db = PostgresAgentDB()
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
        from .llm_client_v2 import get_llm_client
        from .pgdb import PostgresAgentDB
        from ..config import get_outputs_dir

        db = PostgresAgentDB()
        papers_data = db.get_relevant_papers(project_id)

        if not papers_data:
            return {"project_id": project_id, "survey_path": "", "error": "No relevant papers found"}

        llm = get_llm_client()

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

        # NOTE: generate_report_sync 在 LLMClientV2 上未定义；旧代码的 dead-write 已删。
        # 这里直接用 asyncio event loop 跑 async generate_report，
        # 并传 db + project_id 触发 L2 CitationVerifier 反幻觉钩子
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            report = loop.run_until_complete(llm.generate_report(
                user_query, papers, [],
                db=db, project_id=project_id,
            ))
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


# ═══════════════════════════════════════════════════════════════
# Feature: Pipeline Stage Tasks (search / evaluate / rank)
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def search_task(
    self,
    user_query: str,
    sources: list[str] = None,
    year_from: int = 2022,
    max_results: int = 20,
    project_id: str = "",
    agent_task_id: str = "",
) -> dict:
    """Celery task: 多源搜索论文。

    由 sub_agent_task 编排器或 PipelineRunner.run_via_celery() 分发。
    """
    task_id = self.request.id
    task_logger = _get_logger(agent_task_id or task_id, "ingest")
    reporter = _get_reporter()
    db = _get_db()

    try:
        import asyncio
        from ..engine import PaperSearchEngine
        from ..config import Config
        from ..models import SearchQuery, SourceType

        source_list = sources or ["arxiv", "semantic_scholar"]
        stypes = [
            SourceType(s) for s in source_list
            if s in [x.value for x in SourceType]
        ]
        if not stypes:
            stypes = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

        query = SearchQuery(
            keywords=user_query,
            sources=stypes,
            year_from=year_from,
            max_results=max_results,
        )

        engine = PaperSearchEngine(Config())
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(engine.search(query))
        finally:
            loop.close()

        papers = []
        for p in result.papers:
            pid = db.upsert_paper(p)
            if project_id:
                db.link_paper_to_project(project_id, pid, relevance_score=0.5)
            papers.append({
                "paper_id": pid,
                "title": p.title,
                "year": p.year,
                "abstract": (p.abstract or "")[:500],
                "authors": p.authors[:10] if p.authors else [],
                "venue": p.venue or "",
                "source": p.source.value if hasattr(p.source, "value") else str(p.source),
                "doi": p.doi or "",
                "source_url": p.source_url or "",
            })

        task_logger.stage_done(task_id, "search", {"total": len(papers)})
        reporter.report_done(task_id, {"papers_found": len(papers)})
        return {"papers": papers, "total": len(papers), "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.error(f"search_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        task_logger.task_error(task_id, error_str, "")
        reporter.report_error(task_id, error_str)
        return {"papers": [], "total": 0, "error": error_str}


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def evaluate_task(
    self,
    user_query: str,
    papers: list[dict],
    project_id: str = "",
    agent_task_id: str = "",
) -> dict:
    """Celery task: LLM 相关性评估。

    对搜索结果进行批量 LLM 评估，返回每篇论文的相关性评分。
    """
    task_id = self.request.id
    task_logger = _get_logger(agent_task_id or task_id, "ingest")
    reporter = _get_reporter()
    db = _get_db()

    try:
        import asyncio
        from .llm_client_v2 import get_llm_client

        if not papers:
            return {"evaluations": [], "relevant_count": 0, "error": ""}

        llm = get_llm_client()

        # Convert paper dicts for LLM evaluation
        eval_papers = [
            {"title": p.get("title", ""), "abstract": p.get("abstract", "")}
            for p in papers
        ]

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                llm.evaluate_batch(eval_papers, user_query)
            )
        finally:
            loop.close()

        evaluations = []
        relevant_count = 0
        for i, (paper, eval_result) in enumerate(zip(papers, results)):
            score = float(getattr(eval_result, "score", eval_result.get("score", 0.5)) if hasattr(eval_result, "get") else getattr(eval_result, "score", 0.5))
            reason = str(getattr(eval_result, "reason", eval_result.get("reason", "")) if hasattr(eval_result, "get") else getattr(eval_result, "reason", ""))

            is_relevant = score >= 0.5
            if is_relevant:
                relevant_count += 1

            if project_id and paper.get("paper_id"):
                db.link_paper_to_project(
                    project_id, paper["paper_id"],
                    relevance_score=score,
                    relevance_reason=reason,
                )

            evaluations.append({
                "paper_id": paper.get("paper_id", ""),
                "score": score,
                "reason": reason,
                "is_relevant": is_relevant,
            })

        task_logger.stage_done(task_id, "evaluate",
                               {"total": len(papers), "relevant": relevant_count})
        reporter.report_done(task_id, {
            "evaluated": len(papers),
            "relevant": relevant_count,
        })
        return {
            "evaluations": evaluations,
            "relevant_count": relevant_count,
            "error": "",
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"evaluate_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        task_logger.task_error(task_id, error_str, "")
        reporter.report_error(task_id, error_str)
        return {"evaluations": [], "relevant_count": 0, "error": error_str}


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def rank_task(
    self,
    papers: list[dict],
    project_id: str = "",
    agent_task_id: str = "",
) -> dict:
    """Celery task: 期刊等级评定。

    同步执行（JournalRanker 无异步依赖），为有 venue 的论文评定等级。
    """
    task_id = self.request.id
    task_logger = _get_logger(agent_task_id or task_id, "ingest")
    reporter = _get_reporter()
    db = _get_db()

    try:
        from .journal_ranker import JournalRanker

        ranker = JournalRanker()
        ranks = []

        for paper in papers:
            venue = paper.get("venue", "")
            paper_id = paper.get("paper_id", "")
            if not venue or not paper_id:
                continue

            rank_result = ranker.rank(venue)
            level = getattr(rank_result, "unified_level", str(rank_result)) if rank_result else ""

            db.upsert_journal_rank(
                venue,
                ccf=getattr(rank_result, "ccf_level", None) if hasattr(rank_result, "ccf_level") else None,
                sci=getattr(rank_result, "sci_zone", None) if hasattr(rank_result, "sci_zone") else None,
                unified=level,
            )
            if level:
                db.update_paper_meta(paper_id, unified_level=level)

            ranks.append({"paper_id": paper_id, "venue": venue, "level": level})

        task_logger.stage_done(task_id, "rank", {"total": len(ranks)})
        reporter.report_done(task_id, {"ranked": len(ranks)})
        return {"ranks": ranks, "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.error(f"rank_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        task_logger.task_error(task_id, error_str, "")
        reporter.report_error(task_id, error_str)
        return {"ranks": [], "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Feature: Sub-Agent Orchestrator (Celery pipeline)
# ═══════════════════════════════════════════════════════════════


def _get_whisper_model():
    """惰性加载 faster-whisper 模型。

    失败返回 None —— VideoAgent._transcribe_node 会把 None 视为"转录不可用"
    并降级为基于元数据的摘要（不阻塞整个 video 流水线）。
    """
    try:
        from faster_whisper import WhisperModel
        model_size = os.getenv("WHISPER_MODEL_SIZE", "small")
        cache_dir = Path.home() / ".paper_search" / "models" / "whisper"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return WhisperModel(model_size, download_root=str(cache_dir))
    except Exception as e:
        logger.warning(
            f"Whisper model load failed (video transcription will be skipped): {e}"
        )
        return None


def _run_graph_agent(agent_type: str, arguments: dict, log_id: str,
                     user_query: str, project_id: str) -> dict:
    """在 Celery worker 内跑非 ingest 的 graph agent（sync 入口）。

    建独立 event loop → 跑 _run_graph_agent_async → 上报 lifecycle。
    lifecycle 用真实 agent_type（不再写死 "ingest"），订阅方据此判定完成。
    """
    import asyncio

    task_logger = _get_logger(log_id, agent_type)
    reporter = _get_reporter()

    reporter.publish_lifecycle(
        task_id=log_id, agent_type=agent_type,
        lifecycle="agent_started",
        summary=f"开始 {agent_type}: {user_query[:80]}",
    )

    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                _run_graph_agent_async(
                    agent_type, arguments, log_id,
                    reporter, task_logger, project_id,
                )
            )
        finally:
            loop.close()

        task_logger.task_done(log_id, {"result": result})
        reporter.report_done(log_id, result)
        reporter.publish_lifecycle(
            task_id=log_id, agent_type=agent_type,
            lifecycle="agent_done",
            summary=f"{agent_type} 完成",
            result=result,
        )
        return {"result": result, "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.error(
            f"sub_agent_task ({agent_type}) failed: {error_str}", exc_info=True
        )
        task_logger.task_error(log_id, error_str, "")
        reporter.report_error(log_id, error_str)
        reporter.publish_lifecycle(
            task_id=log_id, agent_type=agent_type,
            lifecycle="agent_failed",
            summary=f"{agent_type} 失败: {error_str[:120]}",
            error=error_str,
        )
        return {"error": error_str}


async def _run_graph_agent_async(agent_type: str, arguments: dict, log_id: str,
                                 reporter, task_logger, project_id: str) -> dict:
    """按 agent_type 构造依赖 + compile + ainvoke。

    依赖按需构造（避免无谓初始化，如 video 的 whisper）。
    进度通过 reporter.publish_report 上报（_handle_sub_agent 的订阅循环会
    转推 sub_progress 给 iOS）。

    参数映射（从 LLM 返回的 spec.arguments）:
      - clustering:     project_id, n_clusters
      - citation_chase: seed_title/seed_doi/seed_paper/query, max_depth/depth, direction
      - translation:    action, text/query, direction, project_id
      - video:          query/user_query/url (含链接), project_id
      - rad_query:      question/query, project_id
    """
    from .llm_client_v2 import get_llm_client

    db = _get_db()
    llm = get_llm_client()

    async def on_progress(stage, *args, **kwargs):
        """统一进度回调 — 兼容各 graph 的 _notify(stage, index, total, 0, 0) 签名。"""
        try:
            reporter.publish_report(
                log_id, agent_type, str(stage),
                status="progress",
                extra={"raw_args": [str(a)[:80] for a in args]},
            )
        except Exception:
            pass

    # ── clustering (S6/S8) ──
    if agent_type == "clustering":
        from .pgvector_store import PgVectorStore
        from .graphs.clustering_graph import ClusteringAgent
        chroma = PgVectorStore()
        agent = ClusteringAgent(db, chroma, llm, on_progress=on_progress)
        graph = agent.compile()
        state = {
            "project_id": arguments.get("project_id") or project_id,
            "n_clusters": arguments.get("n_clusters", 0),
        }
        result = await graph.ainvoke(state)
        return result.get("result", result)

    # ── citation_chase (S9) ──
    if agent_type == "citation_chase":
        from ..engine import PaperSearchEngine
        from ..config import Config
        from .graphs.citation_chase_graph import CitationChaseAgent
        engine = PaperSearchEngine(Config())
        agent = CitationChaseAgent(db, llm, engine, on_progress=on_progress)
        graph = agent.compile()
        state = {
            "project_id": arguments.get("project_id") or project_id,
            "seed_title": (arguments.get("seed_title")
                           or arguments.get("seed_paper")
                           or arguments.get("query") or ""),
            "seed_doi": arguments.get("seed_doi", ""),
            "max_depth": arguments.get("depth",
                                       arguments.get("max_depth", 2)),
            "direction": arguments.get("direction", "both"),
        }
        result = await graph.ainvoke(state)
        return result.get("result", result)

    # ── translation (S12) ──
    if agent_type == "translation":
        from .pgvector_store import PgVectorStore
        from .graphs.translation_graph import TranslationAgent
        chroma = PgVectorStore()
        agent = TranslationAgent(db, llm, chroma, on_progress=on_progress)
        graph = agent.compile()
        state = {
            "action": arguments.get("action", "translate_query"),
            "text": arguments.get("text") or arguments.get("query") or "",
            "direction": arguments.get("direction", "zh2en"),
            "project_id": arguments.get("project_id") or project_id,
        }
        result = await graph.ainvoke(state)
        return result.get("result", result)

    # ── video (S13) ──
    if agent_type == "video":
        from .video_downloader import VideoDownloader
        from .graphs.video_graph import VideoAgent
        from ..config import get_videos_dir
        whisper = _get_whisper_model()
        videos_dir = get_videos_dir()
        downloader = VideoDownloader(output_dir=videos_dir)
        agent = VideoAgent(downloader, whisper, llm, db,
                           videos_dir, on_progress=on_progress)
        graph = agent.compile()
        state = {
            "project_id": arguments.get("project_id") or project_id,
            "user_query": (arguments.get("query")
                           or arguments.get("user_query")
                           or arguments.get("url") or ""),
        }
        result = await graph.ainvoke(state)
        return result.get("result", result)

    # ── rad_query (S10) ──
    if agent_type == "rad_query":
        from .pgvector_store import PgVectorStore
        from .knowledge import KnowledgeBase
        from .graphs.knowledge_graph import KnowledgeAgent
        chroma = PgVectorStore()
        kb = KnowledgeBase(db, chroma, llm)
        agent = KnowledgeAgent(kb, on_progress=on_progress)
        graph = agent.compile()
        state = {
            "question": (arguments.get("question")
                         or arguments.get("query") or ""),
            "project_id": arguments.get("project_id") or project_id,
        }
        result = await graph.ainvoke(state)
        return result.get("result", result)

    # 兜底（_handle_sub_agent 已校验 agent_type，这里不应到达）
    raise ValueError(f"Unsupported agent_type: {agent_type}")


@app.task(bind=True, max_retries=0)
def sub_agent_task(
    self,
    user_query: str = "",
    sources: list[str] = None,
    year_from: int = 2022,
    max_results: int = 20,
    project_id: str = "",
    agent_task_id: str = "",
    agent_type: str = "ingest",
    arguments: dict = None,
) -> dict:
    """Celery 编排器 — 按 agent_type 分发到对应 graph runner。

    agent_type == "ingest"（默认，保留原逻辑）:
        7 阶段论文入库流水线
        search → evaluate → group(download) → group(convert)
                → group(index) → rank → survey
        通过 Celery group() 并行处理每篇论文的下载/转换/索引。

    其他 agent_type (clustering / citation_chase / translation / video / rad_query):
        在 Celery worker 内 asyncio 跑对应 LangGraph agent。
        通过 reporter 上报 lifecycle (agent_started/done/failed) + progress。
        依赖各 graph 文件的 Agent 类（ClusteringAgent / CitationChaseAgent / ...）。
    """
    # 非 ingest → 路由到对应 graph runner（ingest 路径完全不变）
    if agent_type != "ingest":
        return _run_graph_agent(
            agent_type=agent_type,
            arguments=arguments or {},
            log_id=agent_task_id or self.request.id,
            user_query=user_query,
            project_id=project_id,
        )

    from celery import group

    task_id = self.request.id
    log_id = agent_task_id or task_id
    task_logger = _get_logger(log_id, "ingest")
    reporter = _get_reporter()

    stages = {}

    # 发布 lifecycle: agent_started（订阅方用来确认订阅生效）
    reporter.publish_lifecycle(
        task_id=log_id,
        agent_type="ingest",
        lifecycle="agent_started",
        summary=f"开始入库: {user_query[:80]}",
    )

    try:
        # ── Stage 1: Search ──
        task_logger.stage_start(log_id, "search", 1, 7)
        search_result = search_task(
            user_query=user_query,
            sources=sources or ["arxiv", "semantic_scholar"],
            year_from=year_from,
            max_results=max_results,
            project_id=project_id,
            agent_task_id=log_id,
        )
        stages["search"] = {"total": search_result.get("total", 0)}
        papers = search_result.get("papers", [])

        if not papers:
            task_logger.task_done(log_id, {"stages": stages})
            reporter.report_done(log_id, {"stages": stages})
            # 新增: 发布 agent_done lifecycle
            reporter.publish_lifecycle(
                task_id=log_id,
                agent_type="ingest",
                lifecycle="agent_done",
                summary="未搜索到论文",
                result={"stages": stages, "total_papers": 0},
            )
            return {"stages": stages, "total_papers": 0, "error": ""}

        # ── Stage 2: Evaluate ──
        task_logger.stage_start(log_id, "evaluate", 2, 7)
        eval_result = evaluate_task(
            user_query=user_query,
            papers=papers,
            project_id=project_id,
            agent_task_id=log_id,
        )
        stages["evaluate"] = {"total": len(papers),
                              "relevant": eval_result.get("relevant_count", 0)}

        # Filter to relevant papers only
        evals = eval_result.get("evaluations", [])
        relevant_papers = [
            papers[i] for i, e in enumerate(evals)
            if e.get("is_relevant", True) and i < len(papers)
        ]
        if not relevant_papers:
            relevant_papers = papers[:10]  # fallback: top 10

        # ── Stage 3: Download (parallel group) ──
        task_logger.stage_start(log_id, "download", 3, 7)
        dl_group = group(
            download_task.s(
                paper_id=p["paper_id"],
                project_id=project_id,
                title=p.get("title", ""),
                source=p.get("source", "arxiv"),
                agent_task_id=log_id,
                paper_index=i + 1,
                paper_total=len(relevant_papers),
            )
            for i, p in enumerate(relevant_papers)
        )
        dl_results = dl_group.apply_async().get(timeout=600)
        dl_success = [r for r in dl_results if r and r.get("success")]
        stages["download"] = {
            "attempted": len(relevant_papers),
            "succeeded": len(dl_success),
        }

        # ── Stage 4: Convert (parallel group) ──
        task_logger.stage_start(log_id, "convert", 4, 7)
        cv_group = group(
            convert_task.s(
                paper_id=r["paper_id"],
                pdf_path=r.get("local_path", ""),
                title=next((p.get("title", "") for p in relevant_papers
                           if p["paper_id"] == r["paper_id"]), ""),
                project_id=project_id,
            )
            for r in dl_success
        )
        cv_results = cv_group.apply_async().get(timeout=600)
        cv_success = [r for r in cv_results if r and r.get("success")]
        stages["convert"] = {
            "attempted": len(dl_success),
            "succeeded": len(cv_success),
        }

        # ── Stage 5: Index (parallel group) ──
        task_logger.stage_start(log_id, "index", 5, 7)
        idx_group = group(
            index_task.s(
                paper_id=r["paper_id"],
                markdown_path=r.get("markdown_path", ""),
                title=next((p.get("title", "") for p in relevant_papers
                           if p["paper_id"] == r["paper_id"]), ""),
                abstract=next((p.get("abstract", "") for p in relevant_papers
                              if p["paper_id"] == r["paper_id"]), ""),
                year=next((p.get("year") for p in relevant_papers
                          if p["paper_id"] == r["paper_id"]), None),
                source=next((p.get("source", "") for p in relevant_papers
                            if p["paper_id"] == r["paper_id"]), ""),
                venue=next((p.get("venue", "") for p in relevant_papers
                           if p["paper_id"] == r["paper_id"]), ""),
            )
            for r in cv_success
        )
        idx_results = idx_group.apply_async().get(timeout=600)
        idx_success = [r for r in idx_results if r and r.get("success")]
        stages["index"] = {
            "attempted": len(cv_success),
            "succeeded": len(idx_success),
        }

        # ── Stage 6: Rank ──
        task_logger.stage_start(log_id, "rank", 6, 7)
        rank_result = rank_task(
            papers=relevant_papers,
            project_id=project_id,
            agent_task_id=log_id,
        )
        stages["rank"] = {"ranked": len(rank_result.get("ranks", []))}

        # ── Stage 7: Survey ──
        task_logger.stage_start(log_id, "survey", 7, 7)
        survey_result = survey_task(
            project_id=project_id,
            user_query=user_query,
        )
        stages["survey"] = {"path": survey_result.get("survey_path", "")}

        total_papers = len(papers)
        task_logger.task_done(log_id, {"stages": stages, "total_papers": total_papers})
        reporter.report_done(log_id, {
            "stages": stages,
            "total_papers": total_papers,
        })
        # 新增: 发布 agent_done lifecycle (订阅方依此判定子 Agent 完成)
        reporter.publish_lifecycle(
            task_id=log_id,
            agent_type="ingest",
            lifecycle="agent_done",
            summary=f"入库完成: 共处理 {total_papers} 篇论文",
            result={"stages": stages, "total_papers": total_papers},
        )
        return {"stages": stages, "total_papers": total_papers, "error": ""}

    except Exception as e:
        error_str = str(e)
        logger.error(f"sub_agent_task failed: {error_str}")
        task_logger.task_error(log_id, error_str, "")
        reporter.report_error(log_id, error_str)
        # 新增: 发布 agent_failed lifecycle
        reporter.publish_lifecycle(
            task_id=log_id,
            agent_type="ingest",
            lifecycle="agent_failed",
            summary=f"入库失败: {error_str[:120]}",
            error=error_str,
        )
        return {"stages": stages, "total_papers": 0, "error": error_str}


# ═══════════════════════════════════════════════════════════════
# Feature: Daily Frontier Tracking (subscription check)
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def subscription_check_task(self) -> dict:
    """Celery Beat 定时任务: 检查所有启用订阅，发现新论文。

    由 Celery Beat 定时触发（默认每 60 分钟）。
    对每个订阅: 搜索 → 对比上次论文 ID → 新论文存入 subscription_results → Pub/Sub 推送。
    """
    import asyncio

    task_id = self.request.id
    task_logger = _get_logger(task_id, "subscription")
    reporter = _get_reporter()
    db = _get_db()

    try:
        subscriptions = db.list_subscriptions(enabled_only=True)
        if not subscriptions:
            return {"checked": 0, "new_papers": 0}

        total_new = 0
        from ..engine import PaperSearchEngine
        from ..config import Config
        from ..models import SearchQuery, SourceType

        engine = PaperSearchEngine(Config())
        loop = asyncio.new_event_loop()

        try:
            for sub in subscriptions:
                sub_id = sub["id"]
                sub_name = sub.get("name", sub_id)

                try:
                    keywords = sub.get("keywords", "")
                    sources = sub.get("sources", ["arxiv", "semantic_scholar"])
                    if isinstance(sources, str):
                        import json as _json
                        sources = _json.loads(sources)
                    last_paper_ids = set(sub.get("last_paper_ids", []))

                    stypes = [
                        SourceType(s) for s in sources
                        if s in [x.value for x in SourceType]
                    ]
                    if not stypes:
                        stypes = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

                    query = SearchQuery(
                        keywords=keywords,
                        sources=stypes,
                        max_results=20,
                    )
                    result = loop.run_until_complete(engine.search(query))

                    # Detect new papers vs last_paper_ids
                    current_paper_ids = []
                    new_papers = []
                    for p in result.papers:
                        pid = db.upsert_paper(p)
                        current_paper_ids.append(pid)
                        if pid not in last_paper_ids:
                            paper_dict = {
                                "paper_id": pid,
                                "title": p.title,
                                "authors": p.authors[:5] if p.authors else [],
                                "year": p.year,
                                "abstract": (p.abstract or "")[:300],
                                "venue": p.venue or "",
                                "source": p.source.value if hasattr(p.source, "value") else str(p.source),
                                "doi": p.doi or "",
                            }
                            new_papers.append(paper_dict)

                    # Store results
                    for paper in new_papers:
                        db.save_subscription_result(sub_id, paper)

                    # Update subscription state
                    db.update_subscription(
                        sub_id,
                        last_checked_at=db._now(),
                        last_paper_ids=current_paper_ids,
                    )

                    if new_papers:
                        total_new += len(new_papers)
                        # Publish notification via Redis Pub/Sub → API process
                        reporter.publish_notification({
                            "subscription_id": sub_id,
                            "subscription_name": sub_name,
                            "new_papers": new_papers,
                        })
                        logger.info(
                            f"Subscription '{sub_name}': {len(new_papers)} new papers"
                        )

                except Exception as sub_err:
                    # Per-subscription isolation — one failure doesn't block others
                    logger.error(
                        f"Subscription '{sub_name}' check failed: {sub_err}",
                        exc_info=True,
                    )
                    continue

        finally:
            loop.close()

        reporter.report_done(task_id, {
            "subscriptions_checked": len(subscriptions),
            "new_papers_found": total_new,
        })
        return {
            "checked": len(subscriptions),
            "new_papers": total_new,
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"subscription_check_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"checked": 0, "new_papers": 0, "error": error_str}
@app.task(bind=True, max_retries=1, default_retry_delay=120)
def literature_push_task(self) -> dict:
    """Celery Beat 每日任务: 基于用户订阅 + 论文库推断话题，推送最新论文。

    由 Celery Beat 每天触发一次。
    流程: 读取所有用户订阅 → 对每个订阅做语义搜索 → 过滤已入库 → 推送通知。
    """
    import asyncio

    task_id = self.request.id
    reporter = _get_reporter()
    db = _get_db()

    try:
        subscriptions = db.list_subscriptions(enabled_only=True)
        if not subscriptions:
            return {"checked": 0, "pushed": 0}

        total_pushed = 0
        engine = PaperSearchEngine(Config())
        loop = asyncio.new_event_loop()

        try:
            for sub in subscriptions:
                sub_id = sub["id"]
                sub_name = sub.get("name", sub_id)
                keywords = sub.get("keywords", "")
                if not keywords:
                    continue

                try:
                    import json as _json
                    sources = sub.get("sources", ["arxiv", "semantic_scholar"])
                    if isinstance(sources, str):
                        sources = _json.loads(sources)

                    stypes = [
                        SourceType(s) for s in sources
                        if s in [x.value for x in SourceType]
                    ] or [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

                    query = SearchQuery(
                        keywords=keywords,
                        sources=stypes,
                        max_results=10,
                        year_from=datetime.now().year,
                    )
                    result = loop.run_until_complete(engine.search(query))

                    pushed = 0
                    for paper in result.papers:
                        pid = db.upsert_paper(paper)
                        paper_dict = {
                            "paper_id": pid,
                            "title": paper.title,
                            "authors": paper.authors[:5] if paper.authors else [],
                            "year": paper.year,
                            "abstract": (paper.abstract or "")[:200],
                            "source": str(paper.source) if hasattr(paper, "source") else "",
                        }
                        db.save_subscription_result(sub_id, paper_dict)
                        pushed += 1

                    if pushed:
                        reporter.publish_notification({
                            "type": "literature_push",
                            "subscription_id": sub_id,
                            "subscription_name": sub_name,
                            "new_papers_count": pushed,
                            "message": f"「{sub_name}」发现 {pushed} 篇新论文",
                        })
                        total_pushed += pushed

                except Exception as sub_err:
                    logger.error(f"literature_push '{sub_name}' failed: {sub_err}")
                    continue

        finally:
            loop.close()

        reporter.report_done(task_id, {
            "subscriptions_checked": len(subscriptions),
            "papers_pushed": total_pushed,
        })
        return {"checked": len(subscriptions), "pushed": total_pushed}

    except Exception as e:
        error_str = str(e)
        logger.error(f"literature_push_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"checked": 0, "pushed": 0, "error": error_str}



# ═══════════════════════════════════════════════════════════════
# Phase 5: System Timers migrated from v1 TimerEventSource
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def health_check_task(self) -> dict:
    """系统健康检查（原 v1 TimerEventSource health_check）。

    每 20 分钟运行，检查:
      - SQLite 可读写
      - Redis 连通性
      - 磁盘空间
    失败时打印 warning，不抛异常（避免 Celery 反复重试）。
    """
    import shutil
    result = {"sqlite": False, "redis": False, "disk_free_gb": 0.0}
    # SQLite
    try:
        db = _get_db()
        db.conn.execute("SELECT 1").fetchone()
        result["sqlite"] = True
    except Exception as e:
        logger.warning(f"[health_check] SQLite failed: {e}")

    # Redis
    try:
        import redis as _redis
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                            decode_responses=True)
        r.ping()
        result["redis"] = True
    except Exception as e:
        logger.warning(f"[health_check] Redis failed: {e}")

    # Disk
    try:
        from ..config import get_data_dir
        usage = shutil.disk_usage(get_data_dir())
        result["disk_free_gb"] = round(usage.free / (1024 ** 3), 2)
        if result["disk_free_gb"] < 1.0:
            logger.warning(f"[health_check] Low disk: {result['disk_free_gb']} GB free")
    except Exception as e:
        logger.warning(f"[health_check] Disk check failed: {e}")

    if not (result["sqlite"] and result["redis"]):
        logger.warning(f"[health_check] FAILED: {result}")
    else:
        logger.info(f"[health_check] OK: {result}")
    return result


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def cleanup_logs_task(self) -> dict:
    """日志清理（原 v1 TimerEventSource cleanup_logs）。

    每天 00:30 运行。清理 ~/.paper_search/logs/ 下:
      - 30 天前的 agent.log.* 滚动归档
      - 30 天前的 sub_agents/*/*.jsonl
    """
    import time
    from pathlib import Path
    log_dir = Path.home() / ".paper_search" / "logs"
    if not log_dir.exists():
        return {"removed": 0, "skipped": "log dir not found"}

    cutoff = time.time() - (30 * 86400)  # 30 days
    removed = 0
    for f in log_dir.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                # 不清理当前活跃的 agent.log
                if f.name == "agent.log":
                    continue
                f.unlink()
                removed += 1
        except Exception as e:
            logger.debug(f"[cleanup_logs] skip {f}: {e}")
    logger.info(f"[cleanup_logs] removed {removed} old log files")
    return {"removed": removed}


# ── Phase 4: Session Close —————————————————————————————————


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def session_close_check_task(self) -> dict:
    """Celery Beat 定时任务: 扫描并关闭过期会话。

    增量扫描策略:
      1. 读取 session_scan_markers 中的扫描水位线
      2. 扫描 updated_at > 水位线 且 updated_at < 1 小时前的活跃会话
      3. 将符合条件的会话 status 切换为 'closed'
      4. 更新水位线

    Returns:
        {"scanned": int, "closed": int, "error": str}
    """
    import json as _json

    task_id = self.request.id or "unknown"
    try:
        from .pgdb import PostgresAgentDB
        db = PostgresAgentDB()
    except Exception as e:
        logger.error(f"session_close_check_task: DB init failed: {e}")
        return {"scanned": 0, "closed": 0, "error": str(e)}

    reporter = _get_reporter()

    try:
        # 1) 读水位线
        marker_row = db.conn.execute(
            "SELECT last_scan_value FROM session_scan_markers "
            "WHERE marker_type = %s",
            ("session_close_last_scan",),
        ).fetchone()
        last_scan = (
            marker_row["last_scan_value"]
            if marker_row
            else "2020-01-01T00:00:00Z"
        )

        now_iso = _now_iso()

        # 2) 扫描 1 小时前有过活动但现在无连接的会话
        active_sessions = db.conn.execute(
            """SELECT id, user_id, title, updated_at
               FROM sessions
               WHERE status = 'active'
                 AND updated_at > %s::timestamptz
                 AND updated_at < NOW() - INTERVAL '1 hour'
               ORDER BY updated_at ASC""",
            (last_scan,),
        ).fetchall()

        closed_count = 0
        skipped_count = 0

        for sess in active_sessions:
            sess_id = sess["id"]
            try:
                db.conn.execute(
                    "UPDATE sessions SET status = %s, updated_at = %s WHERE id = %s",
                    ("closed", now_iso, sess_id),
                )
                db.conn.commit()
                closed_count += 1
                logger.info(
                    "Session closed: id=%s title=%s",
                    sess_id, sess.get("title", ""),
                )
            except Exception as e:
                skipped_count += 1
                logger.warning(
                    "Failed to close session %s: %s", sess_id, e,
                )
                try:
                    db.conn.rollback()
                except Exception:
                    pass

        # 3) 更新水位线
        db.conn.execute(
            """UPDATE session_scan_markers
               SET last_scan_value = %s, updated_at = %s
               WHERE marker_type = %s""",
            (now_iso, now_iso, "session_close_last_scan"),
        )
        db.conn.commit()

        reporter.report_done(task_id, {
            "scanned": len(active_sessions),
            "closed": closed_count,
            "skipped": skipped_count,
        })
        return {
            "scanned": len(active_sessions),
            "closed": closed_count,
            "skipped": skipped_count,
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"session_close_check_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"scanned": 0, "closed": 0, "error": error_str}


def _now_iso() -> str:
    """返回当前 UTC ISO 时间字符串."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
