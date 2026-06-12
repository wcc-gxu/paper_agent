"""ResearchAgent — 自然语言驱动的多步学术搜索 Agent.

管道流程:
  Stage 1: LLM 解析用户意图
  Stage 2: 制定搜索策略
  Stage 3: 迭代搜索 (并发多源 → 去重 → 入库 → LLM评估 → 判断是否继续)
  Stage 4: PDF 下载
  Stage 5: 引用追踪 (可选)
  Stage 6: 报告生成
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import Config
from ..engine import PaperSearchEngine
from ..models import SearchQuery, SearchResult, SourceType
from .db import AgentDB
from .chroma_store import ChromaStore
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

# 默认最大迭代轮数
MAX_ITERATIONS = 3
# 每次最多评估的相关论文数阈值（超过此数不再评估）
MAX_EVALUATIONS = 100


class ResearchAgent:
    """学术搜索 Agent — 用自然语言一句话驱动全流程。

    用法:
        agent = ResearchAgent()
        result = await agent.research("帮我搜近半年 adversarial attack 的最新进展")
        print(result.report)
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        # 确保所有 Provider 已注册
        self._load_providers()
        self.engine = PaperSearchEngine(self.config)
        self.llm = LLMClient()
        self.db = AgentDB()
        self.chroma = ChromaStore()

    @staticmethod
    def _load_providers():
        """导入所有 Provider 模块触发 @register。"""
        try:
            from ..providers import arxiv_provider  # noqa
            from ..providers import semanticscholar_provider  # noqa
            from ..providers import pubmed_provider  # noqa
        except ImportError:
            pass
        try:
            from ..providers import cnki_provider  # noqa
        except ImportError:
            pass
        try:
            from ..providers import ieee_provider  # noqa
        except ImportError:
            pass
        try:
            from ..providers import sciencedirect_provider  # noqa
        except ImportError:
            pass

    # ── 主入口 ────────────────────────────────────────────

    async def research(
        self,
        user_query: str,
        enable_citation_chase: bool = False,
        max_iterations: int = MAX_ITERATIONS,
    ) -> dict:
        """执行完整搜索管道。

        Returns:
            dict with keys: project_id, report, papers, stats, errors
        """
        start_time = time.time()
        project_id = self.db.create_project(user_query)
        stats = {"rounds": 0, "found": 0, "relevant": 0, "downloaded": 0, "errors": []}

        logger.info(f"[{project_id}] 开始搜索: {user_query}")

        try:
            # ── Stage 1: 意图解析 ────────────────────────
            logger.info(f"[{project_id}] Stage 1: 意图解析")
            intent = await self.llm.parse_intent(user_query)
            import json as _json
            self.db.update_project(project_id, parsed_intent=_json.dumps({
                "sub_queries": intent.sub_queries,
                "sources": intent.sources,
                "year_from": intent.year_from,
                "year_to": intent.year_to,
                "entities": intent.entities,
                "domain_hint": intent.domain_hint,
            }, ensure_ascii=False))

            # ── Stage 2: 策略规划 ────────────────────────
            logger.info(f"[{project_id}] Stage 2: 策略规划")
            sources = self._resolve_sources(intent.sources)
            sub_queries = intent.sub_queries if intent.sub_queries else [user_query]

            # ── Stage 3: 迭代搜索 ────────────────────────
            logger.info(f"[{project_id}] Stage 3: 迭代搜索")
            all_papers = []
            all_judgments = []

            for round_num in range(1, max_iterations + 1):
                stats["rounds"] = round_num
                logger.info(f"[{project_id}] 第 {round_num} 轮搜索")

                # 3a: 并发搜索
                round_papers = await self._search_round(
                    project_id, round_num, sub_queries, sources,
                    intent.year_from, intent.year_to, stats,
                )

                if not round_papers:
                    logger.info(f"[{project_id}] 第 {round_num} 轮无新结果")
                    break

                # 3b: 去重 + 入库
                new_papers = self._dedup_and_store(project_id, round_num, round_papers, all_papers)
                all_papers.extend(new_papers)
                stats["found"] = len(all_papers)

                # 3c: LLM 相关性评估
                judgments = await self.llm.evaluate_batch(new_papers, user_query)
                for p, j in zip(new_papers, judgments):
                    self.db.link_paper_to_project(
                        project_id, self.db._paper_id(p), round_num,
                        j.score, j.reason,
                    )
                all_judgments.extend(judgments)
                relevant = [j for j in judgments if j.is_relevant]
                stats["relevant"] = sum(1 for j in all_judgments if j.is_relevant)

                # 3d: 添加到 ChromaDB
                relevant_papers = [p for p, j in zip(new_papers, judgments) if j.is_relevant]
                if relevant_papers:
                    self.chroma.add_papers_batch([
                        {**p.model_dump(), "id": self.db._paper_id(p)}
                        for p in relevant_papers
                    ])

                # 3e: 判断是否继续
                if round_num >= max_iterations:
                    break

                titles = [p.title for p, j in zip(all_papers, all_judgments) if j.is_relevant][:15]
                decision = await self.llm.should_continue_search(
                    user_query, round_num, len(all_papers),
                    len(relevant), titles,
                )
                if not decision.should_continue:
                    logger.info(f"[{project_id}] Agent 判断搜索充分: {decision.reason}")
                    break
                # 更新下一轮搜索词
                sub_queries = decision.new_queries if decision.new_queries else sub_queries
                if decision.new_sources:
                    sources = self._resolve_sources(decision.new_sources)

            # ── Stage 3.5: 期刊分级 + 摘要提炼 ──────────
            logger.info(f"[{project_id}] Stage 3.5: 元数据增强")
            relevant_papers = [p for p, j in zip(all_papers, all_judgments) if j.is_relevant]
            await self._enrich_metadata(project_id, relevant_papers, user_query)

            # ── Stage 4: PDF 下载 ────────────────────────
            logger.info(f"[{project_id}] Stage 4: PDF 下载")
            stats["downloaded"] = await self._download_all(project_id, relevant_papers, stats)

            # ── Stage 4.5: PDF→Markdown 转换 ────────────
            logger.info(f"[{project_id}] Stage 4.5: PDF→Markdown")
            await self._convert_pdfs(project_id, relevant_papers)

            # ── Stage 5: 分块入库 ────────────────────────
            logger.info(f"[{project_id}] Stage 5: Markdown分块入库")
            await self._chunk_and_index(project_id, relevant_papers)

            # ── Stage 6: Wiki + JSON + Survey ────────────
            logger.info(f"[{project_id}] Stage 6: 结构化输出")
            report_path = await self._generate_outputs(project_id, relevant_papers, all_judgments, user_query, stats)

            # ── Stage 7: 引用追踪 (可选) ──────────────────
            if enable_citation_chase and relevant_papers:
                logger.info(f"[{project_id}] Stage 7: 引用追踪")
                await self._citation_chase(project_id, relevant_papers, stats)

            # ── Stage 8: 收尾 ───────────────────────────
            logger.info(f"[{project_id}] Stage 8: 管道完成")
            self.db.update_project(
                project_id,
                status="completed",
                total_papers_found=stats["found"],
                total_relevant=stats["relevant"],
                total_downloaded=stats["downloaded"],
                report_path=str(report_path),
            )

            elapsed = time.time() - start_time
            logger.info(f"[{project_id}] 完成: {stats['relevant']}/{stats['found']} 相关, "
                        f"{stats['downloaded']} PDF, {elapsed:.0f}s")

            # 构建返回的论文摘要
            paper_summaries = []
            for p, j in zip(all_papers, all_judgments):
                if j.is_relevant:
                    pid = self.db._paper_id(p)
                    # 从DB读取增强元数据
                    enriched = self.db.conn.execute(
                        "SELECT unified_level, reading_level, digest FROM papers WHERE id=?", (pid,)
                    ).fetchone()
                    paper_summaries.append({
                        "title": p.title, "year": p.year, "source": p.source.value,
                        "score": j.score, "reason": j.reason,
                        "level": enriched["unified_level"] if enriched else None,
                        "reading_level": enriched["reading_level"] if enriched else None,
                    })

            return {
                "project_id": project_id,
                "report_path": str(report_path),
                "papers": paper_summaries,
                "stats": stats,
                "report_path": str(report_path),
            }

        except Exception as e:
            logger.error(f"[{project_id}] 管道失败: {e}", exc_info=True)
            self.db.update_project(project_id, status="failed")
            return {
                "project_id": project_id,
                "report": f"搜索失败: {e}",
                "papers": [],
                "stats": stats,
                "errors": [str(e)],
            }

    # ── Stage 3 子步骤 ────────────────────────────────────

    async def _search_round(
        self, project_id: str, round_num: int,
        queries: list[str], sources: list[SourceType],
        year_from: Optional[int], year_to: Optional[int],
        stats: dict,
    ) -> list:
        """执行一轮并发多源搜索。"""
        all_papers = []
        for query_text in queries:
            query = SearchQuery(
                keywords=query_text,
                sources=sources,
                max_results=20,
                year_from=year_from,
                year_to=year_to,
            )
            t0 = time.time()
            result = await self.engine.search(query)
            dt = int((time.time() - t0) * 1000)

            for s in sources:
                source_papers = [p for p in result.papers if p.source == s]
                error = next((e for e in result.errors if s.value in e), None)
                self.db.log_search(
                    project_id, round_num, s.value, query_text,
                    len(source_papers), dt, error,
                )
                if error:
                    stats["errors"].append(error)

            all_papers.extend(result.papers)

        return all_papers

    def _dedup_and_store(self, project_id: str, round_num: int, new_papers: list, existing: list) -> list:
        """去重并存入 SQLite。"""
        existing_ids = {self.db._paper_id(p) for p in existing}
        unique = []
        for p in new_papers:
            pid = self.db._paper_id(p)
            if pid not in existing_ids:
                existing_ids.add(pid)
                self.db.upsert_paper(p)
                unique.append(p)
        return unique

    # ── Stage 4: PDF 下载 ─────────────────────────────────

    async def _download_all(self, project_id: str, papers: list, stats: dict) -> int:
        """并发下载所有相关论文 PDF。"""
        if not papers:
            return 0

        downloaded = 0
        for p in papers:
            try:
                result = await self.engine.download(p)
                if result.success:
                    paper_id = self.db._paper_id(p)
                    self.db.mark_pdf_downloaded(project_id, paper_id, result.local_path)
                    downloaded += 1
                else:
                    stats["errors"].append(f"PDF下载失败: {p.title[:60]} - {result.error}")
            except Exception as e:
                # 重试 1 次
                stats["errors"].append(f"PDF下载异常({p.source.value}): {p.title[:40]} - {e}")
                logger.warning(f"PDF下载异常: {p.title[:40]} - {e}")

        return downloaded

    # ── Stage 5: 引用追踪 ─────────────────────────────────

    async def _citation_chase(self, project_id: str, papers: list, stats: dict) -> list:
        """追引 — 找到引用和被引论文（1层）。"""
        from ..providers.semanticscholar_provider import SemanticScholarProvider

        s2 = SemanticScholarProvider(self.config)
        cited_papers = []

        for p in papers:
            if not p.doi and not p.arxiv_id:
                continue
            try:
                # 这里需要通过 Semantic Scholar API 查引用
                # 简化实现：用当前提供商搜索
                pass
            except Exception as e:
                stats["errors"].append(f"引用追踪失败: {p.title[:40]} - {e}")

        return cited_papers

    # ── Stage 6: 报告保存 ─────────────────────────────────

    def _save_report(self, project_id: str, report: str, papers: list,
                     judgments: list, stats: dict) -> Path:
        """保存报告到文件系统。"""
        report_dir = self.config.storage_dir / "reports" / project_id
        report_dir.mkdir(parents=True, exist_ok=True)

        # Markdown 报告
        md_path = report_dir / "report.md"
        md_path.write_text(report, encoding="utf-8")

        # JSON 数据
        import json
        json_path = report_dir / "data.json"
        relevant = [
            {"title": p.title, "authors": p.authors[:5], "year": p.year,
             "doi": p.doi, "source": p.source.value, "score": j.score, "reason": j.reason}
            for p, j in zip(papers, judgments) if j.is_relevant
        ]
        json_path.write_text(json.dumps({
            "project_id": project_id,
            "stats": stats,
            "papers": relevant,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # BibTeX 导出
        bib_path = report_dir / "references.bib"
        bib_entries = []
        for p, j in zip(papers, judgments):
            if j.is_relevant and p.doi:
                entry = self._make_bibtex(p)
                if entry:
                    bib_entries.append(entry)
        bib_path.write_text("\n\n".join(bib_entries), encoding="utf-8")

        return md_path

    def _make_bibtex(self, paper) -> str:
        """生成 BibTeX 条目。"""
        first_author = paper.authors[0].split()[-1] if paper.authors else "Unknown"
        year = str(paper.year) if paper.year else "????"
        key = first_author + year + paper.title.split()[0].lower()[:20]
        doi = paper.doi or ""
        url = paper.source_url or ("https://doi.org/" + doi if doi else "")
        authors_str = " and ".join(paper.authors[:10])
        venue = paper.venue or ""

        nl = "\n"
        return (
            "@article{" + key + "," + nl
            + "  title = {{" + paper.title + "}}," + nl
            + "  author = {" + authors_str + "}," + nl
            + "  year = {" + year + "}," + nl
            + "  journal = {" + venue + "}," + nl
            + "  doi = {" + doi + "}," + nl
            + "  url = {" + url + "}" + nl
            + "}"
        )

    # ── Stage 3.5: 元数据增强 ─────────────────────────

    async def _enrich_metadata(self, project_id: str, papers: list, user_query: str):
        """期刊分级 + LLM摘要提炼 + 方法标签提取。"""
        from .journal_ranker import JournalRanker
        ranker = JournalRanker()

        for p in papers:
            pid = self.db._paper_id(p)
            venue = p.venue or ""
            level = ranker.rank(venue)
            if level:
                self.db.update_paper_meta(pid, unified_level=level)

            # LLM提取摘要+方法标签（限制并发）
            try:
                digest_data = await self.llm.extract_digest(p, level)
                self.db.update_paper_meta(
                    pid,
                    digest=json.dumps(digest_data.get("digest", []), ensure_ascii=False),
                    reading_level=digest_data.get("reading_level", "skim"),
                    method_tags=json.dumps(digest_data.get("method_tags", []), ensure_ascii=False),
                    dataset_info=digest_data.get("dataset_info", ""),
                )
                # 缓存期刊分级
                if level:
                    self.db.upsert_journal_rank(venue, unified=level)
            except Exception as e:
                logger.warning(f"元数据增强失败 {p.title[:40]}: {e}")

    # ── Stage 4.5: PDF→Markdown 转换 ──────────────────

    async def _convert_pdfs(self, project_id: str, papers: list):
        """将下载的PDF转为Markdown。"""
        from .pdf_converter import PDFConverter
        converter = PDFConverter()

        md_dir = self.config.storage_dir / "markdown" / project_id
        for p in papers:
            pid = self.db._paper_id(p)
            row = self.db.conn.execute(
                "SELECT pdf_path FROM project_papers WHERE project_id=? AND paper_id=? AND pdf_downloaded=1",
                (project_id, pid)
            ).fetchone()
            if not row or not row["pdf_path"]:
                continue

            pdf_path = Path(row["pdf_path"])
            md_path = await converter.convert(pdf_path, md_dir)
            if md_path:
                self.db.update_paper_meta(pid, markdown_path=str(md_path))

    # ── Stage 5: 分块入库 ────────────────────────────

    async def _chunk_and_index(self, project_id: str, papers: list):
        """将Markdown分块并装入ChromaDB。"""
        from .chunker import SectionChunker
        chunker = SectionChunker()

        for p in papers:
            pid = self.db._paper_id(p)
            row = self.db.conn.execute(
                "SELECT markdown_path FROM papers WHERE id=? AND markdown_path IS NOT NULL",
                (pid,)
            ).fetchone()
            if not row:
                continue

            md_path = Path(row["markdown_path"])
            if not md_path.exists():
                continue

            md_text = md_path.read_text(encoding="utf-8")
            chunks = chunker.chunk(md_text, pid)
            if chunks:
                self.chroma.add_chunks(chunks)

    # ── Stage 6: Wiki + JSON + Survey ────────────────

    async def _generate_outputs(self, project_id: str, papers: list, judgments: list,
                                user_query: str, stats: dict) -> Path:
        """生成Wiki、JSON元数据、Survey报告。"""
        from .wiki_generator import WikiGenerator

        output_dir = self.config.storage_dir / "outputs" / project_id
        wiki = WikiGenerator(self.llm)

        # 构建增强的论文数据
        papers_data = []
        for p, j in zip(papers, judgments):
            pid = self.db._paper_id(p)
            enriched = self.db.conn.execute(
                "SELECT unified_level, reading_level, digest, method_tags, dataset_info "
                "FROM papers WHERE id=?", (pid,)
            ).fetchone()
            if not enriched:
                continue

            digest = enriched["digest"]
            try:
                digest = json.loads(digest) if digest else []
            except Exception:
                digest = [digest] if digest else []

            papers_data.append({
                "paper_id": pid,
                "title": p.title,
                "authors": p.authors,
                "year": p.year,
                "source": p.source.value,
                "doi": p.doi,
                "venue": p.venue,
                "citation_count": p.citation_count,
                "level": enriched["unified_level"],
                "reading_level": enriched["reading_level"],
                "one_liner": "",
                "digest": digest,
                "method_tags": enriched["method_tags"],
                "dataset_info": enriched["dataset_info"],
            })

        if papers_data:
            # JSON元数据
            await wiki.export_metadata_json(papers_data, output_dir / "metadata.json")
            # Wiki
            await wiki.generate_wiki(papers_data, project_id, output_dir, user_query)

        # LLM综述报告
        report = await self.llm.generate_report(user_query, papers, judgments)
        (output_dir / "survey.md").write_text(report, encoding="utf-8")

        # 保存旧格式报告（兼容）
        self._save_report(project_id, report, papers, judgments, stats)

        return output_dir

    # ── 工具方法 ──────────────────────────────────────────

    def _resolve_sources(self, source_names: list[str]) -> list[SourceType]:
        """解析来源名称列表为 SourceType 枚举。"""
        valid = []
        for name in source_names:
            try:
                valid.append(SourceType(name.strip().lower()))
            except ValueError:
                logger.warning(f"未知来源: {name}")
        return valid if valid else [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

    async def get_status(self, project_id: str) -> dict:
        """获取搜索项目状态。"""
        project = self.db.get_project(project_id)
        if not project:
            return {"error": f"项目不存在: {project_id}"}
        papers = self.db.get_project_papers(project_id)
        return {
            "project": project,
            "papers_count": len(papers),
            "relevant_count": sum(1 for p in papers if p["relevance_score"] >= 0.5),
            "downloaded_count": sum(1 for p in papers if p["pdf_downloaded"]),
        }

    async def list_history(self, limit: int = 10) -> list[dict]:
        """列出历史搜索项目。"""
        return self.db.list_projects(limit)

    async def close(self):
        await self.engine.close()
        self.db.close()
