"""子 Agent 编排器 — PipelineRunner。

编排 IngestAgent 的 7 阶段论文入库流水线 (默认进程内执行):
  1. search_stage    → Engine.search()              [进程内 async]
  2. evaluate_stage  → LLM.evaluate_batch()         [进程内 async]
  3. download_stage  → PipelineRunner._download     [进程内 async]
  4. convert_stage   → PipelineRunner._convert      [进程内 async]
  5. index_stage     → PipelineRunner._index        [进程内 async]
  6. rank_stage      → JournalRanker.rank_batch()   [进程内 async]
  7. survey_stage    → PipelineRunner._survey       [进程内 async]

Celery 异步调度 (通过 sub_agent_task 编排器):
  run_pipeline_via_celery() → Celery sub_agent_task.delay()
    → search_task → evaluate_task → group(download) → group(convert)
    → group(index) → rank_task → survey_task

执行模式:
  - ExecuteGraph (默认): 完整流水线，进程内执行
  - Single Tool: 单独执行一个阶段
  - Celery 分发: run_pipeline_via_celery() 异步执行

进度上报:
  - TaskLogger 写 JSON 日志
  - Reporter 推 Redis 事件
  - on_progress 回调 → ws_manager 推送
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

STAGES = ["search", "evaluate", "download", "convert", "index", "rank", "survey"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PipelineRunner:
    """子 Agent 编排器。"""

    def __init__(self, engine, db, llm, chroma, converter, ranker,
                 task_logger_cls=None, reporter=None,
                 on_progress: Optional[Callable] = None,
                 agent_type: str = "ingest"):
        """
        Args:
            engine: PaperSearchEngine 实例
            db: AgentDB 实例
            llm: LLMClientV2 实例
            chroma: ChromaStoreV2 实例
            converter: PDFConverter 实例
            ranker: JournalRanker 实例
            task_logger_cls: TaskLogger 类（用于创建实例）
            reporter: Reporter 实例
            on_progress: 进度回调 async def(stage, data)
            agent_type: 子 Agent 类型 (用于日志分目录)
        """
        self.engine = engine
        self.db = db
        self.llm = llm
        self.chroma = chroma
        self.converter = converter
        self.ranker = ranker
        self._task_logger_cls = task_logger_cls
        self._reporter = reporter
        self._on_progress = on_progress
        self.agent_type = agent_type

    # ══════════════════════════════════════════════════════════
    # ExecuteGraph — 完整 7 阶段流水线
    # ══════════════════════════════════════════════════════════

    async def run_pipeline(self, project_id: str, user_query: str,
                           sources: list[str] = None,
                           year_from: int = 2022,
                           max_results: int = 20) -> dict:
        """完整 7 阶段论文入库流水线。

        Returns:
            {"project_id": str, "stages": dict, "total_papers": int,
             "downloaded": int, "indexed": int, "failed": int, "errors": list}
        """
        sources = sources or ["arxiv", "semantic_scholar"]
        task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4]}"

        from .task_logger import TaskLogger
        log_dir = Path.home() / ".paper_search" / "logs" / "sub_agents" / self.agent_type
        tlog = TaskLogger(log_dir, task_id)

        tlog.task_start(task_id, project_id, {
            "user_query": user_query, "sources": sources,
            "year_from": year_from, "max_results": max_results,
        })

        errors = []
        stage_results = {}
        papers = []

        try:
            # 1. Search
            papers = await self._search_stage(task_id, project_id, user_query, sources, year_from, max_results, tlog)
            stage_results["search"] = {"total": len(papers)}

            if not papers:
                tlog.task_done(task_id, {"total": 0, "message": "No papers found"})
                return {"project_id": project_id, "stages": stage_results, "total_papers": 0,
                        "downloaded": 0, "indexed": 0, "failed": 0, "errors": ["No papers found"]}

            # 2. Evaluate
            evaluations = await self._evaluate_stage(task_id, project_id, user_query, papers, tlog)
            stage_results["evaluate"] = {"evaluated": len(evaluations)}

            # 筛选高相关论文
            relevant_papers = [p for p, ev in zip(papers, evaluations) if ev.get("score", 0) >= 0.5]
            if not relevant_papers:
                relevant_papers = papers[:10]  # fallback: top 10

            tlog.stage_progress(task_id, "evaluate", len(relevant_papers), len(papers))

            # 3-5: Download → Convert → Index（每篇论文独立处理）
            stats = await self._process_papers(task_id, project_id, relevant_papers, tlog)
            stage_results.update(stats)

            # 6. Rank
            rank_results = await self._rank_stage(task_id, project_id, relevant_papers, tlog)
            stage_results["rank"] = {"ranked": len(rank_results)}

            # 7. Survey
            survey_result = await self._survey_stage(task_id, project_id, user_query, tlog)
            stage_results["survey"] = survey_result

            downloaded = stats.get("download", {}).get("success", 0)
            indexed = stats.get("index", {}).get("success", 0)
            failed = errors.count("download") + errors.count("convert") + errors.count("index")

            tlog.task_done(task_id, {
                "total_papers": len(papers),
                "downloaded": downloaded,
                "indexed": indexed,
                "failed": failed,
                "errors": errors[:20],
            })

            if self._reporter:
                self._reporter.report_done(task_id, {
                    "total": len(papers), "downloaded": downloaded, "indexed": indexed,
                })

            return {
                "project_id": project_id,
                "task_id": task_id,
                "stages": stage_results,
                "total_papers": len(papers),
                "downloaded": downloaded,
                "indexed": indexed,
                "failed": failed,
                "errors": errors[:20],
            }

        except Exception as e:
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            tlog.task_error(task_id, str(e), "")
            if self._reporter:
                self._reporter.report_error(task_id, str(e))
            raise

    # ══════════════════════════════════════════════════════════
    # Single Tool
    # ══════════════════════════════════════════════════════════

    async def run_single(self, tool_name: str, **kwargs) -> dict:
        """单独执行一个工具。

        Args:
            tool_name: search | download | convert | index | evaluate | rank | survey
            **kwargs: 工具特定参数

        Returns:
            工具执行结果
        """
        task_id = f"single-{tool_name}-{uuid.uuid4().hex[:6]}"

        from .task_logger import TaskLogger
        log_dir = Path.home() / ".paper_search" / "logs" / "sub_agents" / self.agent_type
        tlog = TaskLogger(log_dir, task_id)

        tlog.task_start(task_id, "", {"tool": tool_name, "kwargs": kwargs})

        try:
            if tool_name == "search":
                papers = await self._search_stage(task_id, kwargs.get("project_id", ""),
                                                   kwargs.get("user_query", ""),
                                                   kwargs.get("sources", ["arxiv"]),
                                                   kwargs.get("year_from", 2022),
                                                   kwargs.get("max_results", 20),
                                                   tlog)
                tlog.task_done(task_id, {"total": len(papers)})
                return {"papers": papers, "total": len(papers)}

            elif tool_name == "download":
                paper_data = kwargs.get("paper", {})
                result = await self._download_single(task_id, kwargs.get("project_id", ""), paper_data, tlog)
                tlog.task_done(task_id, result)
                return result

            elif tool_name == "convert":
                result = await self._convert_single(task_id, kwargs.get("paper_id", ""),
                                                     kwargs.get("pdf_path", ""),
                                                     kwargs.get("title", ""), tlog)
                tlog.task_done(task_id, result)
                return result

            elif tool_name == "index":
                result = await self._index_single(task_id, kwargs.get("paper_id", ""),
                                                   kwargs.get("markdown_path", ""),
                                                   kwargs.get("title", ""), tlog)
                tlog.task_done(task_id, result)
                return result

            elif tool_name == "evaluate":
                papers = kwargs.get("papers", [])
                results = await self._evaluate_stage(task_id, kwargs.get("project_id", ""),
                                                      kwargs.get("user_query", ""), papers, tlog)
                tlog.task_done(task_id, {"evaluated": len(results)})
                return {"evaluations": results}

            elif tool_name == "rank":
                papers = kwargs.get("papers", [])
                results = await self._rank_stage(task_id, kwargs.get("project_id", ""), papers, tlog)
                tlog.task_done(task_id, {"ranked": len(results)})
                return {"ranks": results}

            elif tool_name == "survey":
                result = await self._survey_stage(task_id, kwargs.get("project_id", ""),
                                                   kwargs.get("user_query", ""), tlog)
                tlog.task_done(task_id, result)
                return result

            else:
                return {"error": f"Unknown tool: {tool_name}"}

        except Exception as e:
            logger.error(f"Single tool {tool_name} failed: {e}", exc_info=True)
            tlog.task_error(task_id, str(e), "")
            return {"error": str(e)}

    # ══════════════════════════════════════════════════════════
    # 阶段实现
    # ══════════════════════════════════════════════════════════

    async def _search_stage(self, task_id, project_id, user_query, sources, year_from, max_results, tlog) -> list:
        """搜索论文 → 持久化到 DB → 返回论文列表。"""
        from ..models import SearchQuery, SourceType

        tlog.stage_start(task_id, "search", 1, 7)

        source_list = [SourceType(s) for s in sources if s in [x.value for x in SourceType]]
        if not source_list:
            source_list = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

        query = SearchQuery(keywords=user_query, sources=source_list,
                            year_from=year_from, max_results=max_results)
        try:
            result = await self.engine.search(query)
        except Exception as e:
            logger.error(f"Search failed: {e}")
            tlog.stage_done(task_id, "search", {"total": 0, "error": str(e)})
            return []

        papers = []
        for p in result.papers:
            paper_id = self.db.upsert_paper(p)
            self.db.link_paper_to_project(project_id, paper_id, round_num=1)
            paper_dict = {
                "paper_id": paper_id, "title": p.title, "year": p.year,
                "abstract": (p.abstract or "")[:300],
                "authors": p.authors[:5] if p.authors else [],
                "venue": p.venue or "", "source": p.source.value,
                "doi": p.doi or "",
            }
            papers.append(paper_dict)
            tlog.paper_progress(task_id, "search", paper_id, p.title, "search_found")

        tlog.stage_done(task_id, "search", {"total": len(papers)})
        if self._on_progress:
            await self._on_progress("search", {"total": len(papers)})

        return papers

    async def _evaluate_stage(self, task_id, project_id, user_query, papers, tlog) -> list:
        """LLM 评估论文相关性。"""
        tlog.stage_start(task_id, "evaluate", 2, 7)

        if not papers:
            return []

        try:
            results = await self.llm.evaluate_batch(papers, user_query, max_concurrent=5)
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return [{"score": 0.5, "reason": "eval_failed", "is_relevant": True} for _ in papers]

        evaluations = []
        for i, (paper, judgment) in enumerate(zip(papers, results)):
            try:
                self.db.link_paper_to_project(
                    project_id, paper["paper_id"],
                    relevance_score=judgment.score,
                    relevance_reason=judgment.reason,
                )
            except Exception as e:
                logger.debug(f"link_paper_to_project failed for {paper.get('paper_id', '?')}: {e}")
            evaluations.append({"score": judgment.score, "reason": judgment.reason,
                                "is_relevant": judgment.is_relevant})
            tlog.paper_progress(task_id, "evaluate", paper["paper_id"], paper["title"], "eval_complete")

        tlog.stage_done(task_id, "evaluate", {"evaluated": len(evaluations)})
        if self._on_progress:
            await self._on_progress("evaluate", {"evaluated": len(evaluations)})

        return evaluations

    async def _process_papers(self, task_id, project_id, papers, tlog) -> dict:
        """批量处理论文: download → convert → index。"""
        stats = {
            "download": {"success": 0, "failed": 0},
            "convert": {"success": 0, "failed": 0},
            "index": {"success": 0, "failed": 0},
        }

        for i, paper in enumerate(papers):
            paper_id = paper["paper_id"]
            title = paper["title"]
            tlog.stage_progress(task_id, "download", i + 1, len(papers))

            # Download
            dl_result = await self._download_single(task_id, project_id, paper, tlog)
            if dl_result.get("success"):
                stats["download"]["success"] += 1
                pdf_path = dl_result.get("local_path", "")
            else:
                stats["download"]["failed"] += 1
                continue  # 跳过下载失败的论文

            # Convert
            cv_result = await self._convert_single(task_id, paper_id, pdf_path, title, tlog)
            if cv_result.get("success"):
                stats["convert"]["success"] += 1
                md_path = cv_result.get("markdown_path", "")
            else:
                stats["convert"]["failed"] += 1
                continue

            # Index
            ix_result = await self._index_single(task_id, paper_id, md_path, title,
                                                   paper.get("abstract", ""),
                                                   paper.get("year"),
                                                   paper.get("source", ""),
                                                   paper.get("venue", ""),
                                                   tlog)
            if ix_result.get("success"):
                stats["index"]["success"] += 1
            else:
                stats["index"]["failed"] += 1

        return stats

    async def _download_single(self, task_id, project_id, paper, tlog) -> dict:
        """下载单篇论文 — 重试 1 次。"""
        paper_id = paper["paper_id"]
        title = paper["title"]

        for attempt in range(2):
            if attempt > 0:
                logger.info(f"Download retry {attempt} for {paper_id}")
                tlog.paper_progress(task_id, "download", paper_id, title, "download_start")

            try:
                from ..models import Paper, SourceType
                src = SourceType(paper.get("source", "arxiv")) if paper.get("source") in [s.value for s in SourceType] else SourceType.ARXIV
                p = Paper(title=title, authors=paper.get("authors", []), year=paper.get("year"),
                          abstract=paper.get("abstract", ""), source=src)
                result = await self.engine.download(p)
                if result.success and result.local_path:
                    self.db.mark_pdf_downloaded(project_id, paper_id, str(result.local_path))
                    tlog.paper_progress(task_id, "download", paper_id, title, "download_done")
                    return {"paper_id": paper_id, "success": True, "local_path": str(result.local_path)}
                if attempt == 0:
                    continue  # 重试
            except Exception as e:
                logger.warning(f"Download attempt {attempt+1} failed for {paper_id}: {e}")
                if attempt == 0:
                    continue

        tlog.paper_progress(task_id, "download", paper_id, title, "download_failed")
        return {"paper_id": paper_id, "success": False, "local_path": "", "error": "Download failed after 2 attempts"}

    async def _convert_single(self, task_id, paper_id, pdf_path, title, tlog) -> dict:
        """转换单篇论文 PDF → Markdown — 重试 1 次。"""
        for attempt in range(2):
            if attempt > 0:
                tlog.paper_progress(task_id, "convert", paper_id, title, "convert_start")

            try:
                pdf = Path(pdf_path)
                if not pdf.exists():
                    if attempt == 0: continue
                    break

                from ..config import get_markdown_dir
                output_dir = get_markdown_dir()
                output_dir.mkdir(parents=True, exist_ok=True)
                md_path = await self.converter.convert(pdf, output_dir)
                if md_path:
                    self.db.update_paper_meta(paper_id, markdown_path=str(md_path))
                    tlog.paper_progress(task_id, "convert", paper_id, title, "convert_done")
                    return {"paper_id": paper_id, "success": True, "markdown_path": str(md_path)}
                if attempt == 0:
                    continue
            except Exception as e:
                logger.warning(f"Convert attempt {attempt+1} failed for {paper_id}: {e}")
                if attempt == 0:
                    continue

        tlog.paper_progress(task_id, "convert", paper_id, title, "convert_failed")
        return {"paper_id": paper_id, "success": False, "markdown_path": "", "error": "Convert failed after 2 attempts"}

    async def _index_single(self, task_id, paper_id, markdown_path, title,
                             abstract="", year=None, source="", venue="", tlog=None) -> dict:
        """索引单篇论文 → ChromaDB — 重试 1 次。"""
        for attempt in range(2):
            if attempt > 0 and tlog:
                tlog.paper_progress(task_id, "index", paper_id, title, "index_start")

            try:
                md = Path(markdown_path)
                if not md.exists():
                    if attempt == 0: continue
                    break

                content = md.read_text(encoding="utf-8")
                self.chroma.add_abstracts_batch([{
                    "paper_id": paper_id, "title": title,
                    "abstract": abstract or content[:500],
                    "year": year, "source": source, "venue": venue,
                }])

                from .chunker import SectionChunker
                chunker = SectionChunker()
                chunks = chunker.chunk(content, paper_id)
                chunk_count = self.chroma.add_fulltext_chunks(chunks) if chunks else 0

                self.db.update_paper_meta(paper_id, embedding_id=f"idx:{paper_id}")
                if tlog:
                    tlog.paper_progress(task_id, "index", paper_id, title, "index_done")
                return {"paper_id": paper_id, "success": True, "chunks": chunk_count}
            except Exception as e:
                logger.warning(f"Index attempt {attempt+1} failed for {paper_id}: {e}")
                if attempt == 0:
                    continue

        if tlog:
            tlog.paper_progress(task_id, "index", paper_id, title, "index_failed")
        return {"paper_id": paper_id, "success": False, "chunks": 0, "error": "Index failed after 2 attempts"}

    async def _rank_stage(self, task_id, project_id, papers, tlog) -> list:
        """期刊等级评定。"""
        tlog.stage_start(task_id, "rank", 6, 7)

        results = []
        for paper in papers:
            venue = paper.get("venue", "")
            if venue:
                level = self.ranker.rank(venue)
                if level:
                    try:
                        self.db.upsert_journal_rank(venue, unified=level)
                        self.db.update_paper_meta(paper["paper_id"], unified_level=level)
                    except Exception as e:
                        logger.warning(f"upsert_journal_rank failed for {paper.get('paper_id', '?')}: {e}")
                    results.append({"paper_id": paper["paper_id"], "venue": venue, "level": level})
                    tlog.paper_progress(task_id, "rank", paper["paper_id"], paper["title"], "rank_done")

        tlog.stage_done(task_id, "rank", {"ranked": len(results)})
        return results

    async def _survey_stage(self, task_id, project_id, user_query, tlog) -> dict:
        """生成文献综述。"""
        tlog.stage_start(task_id, "survey", 7, 7)

        try:
            relevant = self.db.get_relevant_papers(project_id)
            if not relevant:
                return {"survey_path": "", "error": "No relevant papers"}

            papers_for_report = []
            for p in relevant[:30]:
                papers_for_report.append({
                    "title": p.get("title", ""),
                    "authors": json.loads(p.get("authors", "[]")) if isinstance(p.get("authors"), str) else (p.get("authors") or []),
                    "year": p.get("year"),
                    "abstract": p.get("abstract", ""),
                    "venue": p.get("venue", ""),
                })

            # L2 反幻觉：传入 db + project_id 让 generate_report 调 CitationVerifier
            report = await self.llm.generate_report(
                user_query, papers_for_report, [],
                db=self.db, project_id=project_id,
            )

            from ..config import get_outputs_dir
            output_dir = get_outputs_dir() / project_id
            output_dir.mkdir(parents=True, exist_ok=True)
            survey_path = output_dir / "survey.md"
            survey_path.write_text(report, encoding="utf-8")

            self.db.update_project(project_id, report_path=str(survey_path))

            tlog.stage_done(task_id, "survey", {"survey_path": str(survey_path)})
            tlog.paper_progress(task_id, "survey", "", "", "survey_done")

            if self._on_progress:
                await self._on_progress("survey", {"path": str(survey_path)})

            return {"survey_path": str(survey_path)}
        except Exception as e:
            logger.error(f"Survey generation failed: {e}")
            return {"survey_path": "", "error": str(e)}

    # ══════════════════════════════════════════════════════════
    # Celery 异步分发 — 完整流水线通过 Celery 编排器执行
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def run_pipeline_via_celery(
        user_query: str,
        sources: list[str] = None,
        year_from: int = 2022,
        max_results: int = 20,
        project_id: str = "",
        agent_task_id: str = "",
    ) -> dict:
        """通过 Celery sub_agent_task 异步执行完整流水线。

        Args:
            user_query: 搜索查询
            sources: 搜索来源列表
            year_from: 起始年份
            max_results: 每个来源最大结果数
            project_id: 项目 ID
            agent_task_id: 主 Agent 任务 ID (用于日志/报告关联)

        Returns:
            {"celery_task_id": str, "status": "dispatched"}
        """
        from .celery_tasks import sub_agent_task

        result = sub_agent_task.delay(
            user_query=user_query,
            sources=sources or ["arxiv", "semantic_scholar"],
            year_from=year_from,
            max_results=max_results,
            project_id=project_id,
            agent_task_id=agent_task_id,
        )
        logger.info(
            f"Celery pipeline dispatched: task_id={result.id} "
            f"query={user_query[:60]}"
        )
        return {"celery_task_id": result.id, "status": "dispatched"}
