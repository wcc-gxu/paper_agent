"""PaperSearchEngine — 统一搜索/下载门面，协调所有 Provider."""

import asyncio
import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from .config import Config
from .models import (
    BatchSummary,
    DownloadResult,
    Paper,
    SearchQuery,
    SearchResult,
    SourceType,
)
from .providers import get_provider, list_providers
from .providers.base import BaseProvider

logger = logging.getLogger(__name__)

# 去重阈值：标题相似度 >= 此值视为重复
_DEDUP_TITLE_SIMILARITY = 0.85


class PaperSearchEngine:
    """统一搜索与下载门面。

    用法:
        engine = PaperSearchEngine()
        result = await engine.search(SearchQuery(keywords="transformer", sources=[...]))
        dl_result = await engine.download(result.papers[0])
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._providers: dict[SourceType, BaseProvider] = {}

    def _get_provider(self, source: SourceType) -> BaseProvider:
        """获取或创建 Provider 实例（惰性初始化 + 缓存）。"""
        if source not in self._providers:
            self._providers[source] = get_provider(source, config=self.config)
        return self._providers[source]

    # ── 搜索 ──────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> SearchResult:
        """多源并发搜索，去重后返回聚合结果。

        Args:
            query: 统一搜索查询（指定关键词、来源、最大结果数等）。

        Returns:
            SearchResult: 包含去重后的论文列表和各来源错误信息。
        """
        sources = query.sources or [SourceType.ARXIV]
        if not sources:
            return SearchResult(query=query)

        all_papers: list[Paper] = []
        errors: list[str] = []

        # 并发搜索所有来源
        async def search_source(source: SourceType) -> tuple[SourceType, list[Paper], Optional[str]]:
            try:
                provider = self._get_provider(source)
                papers = await provider.search(query)
                return source, papers, None
            except Exception as e:
                logger.error(f"{source.value} 搜索异常: {e}")
                return source, [], f"{source.value}: {e}"

        tasks = [search_source(s) for s in sources]
        results = await asyncio.gather(*tasks)

        for source, papers, error in results:
            if error:
                errors.append(error)
            all_papers.extend(papers)

        # 去重
        all_papers = self._deduplicate(all_papers)

        logger.info(
            f"搜索完成: {len(all_papers)} 篇论文 (去重后), "
            f"{len(errors)} 个错误, 来源: {[s.value for s in sources]}"
        )
        return SearchResult(
            query=query,
            papers=all_papers,
            total_found=len(all_papers),
            errors=errors,
        )

    async def search_single_source(
        self, query: SearchQuery, source: SourceType
    ) -> list[Paper]:
        """在单个来源中搜索（不聚合）。"""
        provider = self._get_provider(source)
        return await provider.search(query)

    # ── 下载 ──────────────────────────────────────────────

    async def download(
        self,
        paper: Paper,
        target_dir: Optional[Path] = None,
    ) -> DownloadResult:
        """下载单篇论文的 PDF。

        Args:
            paper: 要下载的论文对象（需包含 source 信息）。
            target_dir: 目标目录，默认使用配置中的 storage_dir / source / year。

        Returns:
            DownloadResult: 包含本地路径和成功/失败状态。
        """
        if target_dir is None:
            from .downloaders.filename_utils import build_storage_path
            target_path = build_storage_path(paper, self.config.storage_dir)
            target_dir = target_path.parent

        provider = self._get_provider(paper.source)

        try:
            local_path = await provider.download_pdf(paper, target_dir)
            if local_path:
                return DownloadResult(
                    paper=paper,
                    local_path=str(local_path),
                    success=True,
                )
            else:
                return DownloadResult(
                    paper=paper,
                    local_path="",
                    success=False,
                    error="无法获取 PDF URL 或下载失败",
                )
        except Exception as e:
            logger.error(f"下载失败: {paper.title[:60]} - {e}")
            return DownloadResult(
                paper=paper,
                local_path="",
                success=False,
                error=str(e),
            )

    async def download_many(
        self,
        papers: list[Paper],
        max_concurrent: Optional[int] = None,
    ) -> list[DownloadResult]:
        """批量下载多篇论文 PDF。

        Args:
            papers: 论文列表。
            max_concurrent: 最大并发下载数，默认使用配置值。

        Returns:
            下载结果列表。
        """
        if max_concurrent is None:
            max_concurrent = self.config.max_concurrent_downloads

        semaphore = asyncio.Semaphore(max_concurrent)

        async def download_with_limit(p: Paper) -> DownloadResult:
            async with semaphore:
                return await self.download(p)

        results = await asyncio.gather(*[download_with_limit(p) for p in papers])
        return list(results)

    # ── 批量搜索 ──────────────────────────────────────────

    async def batch_search(
        self,
        queries: list[SearchQuery],
        download: bool = False,
    ) -> BatchSummary:
        """批量执行多个搜索查询。

        Args:
            queries: 搜索查询列表。
            download: 是否同时下载所有论文的 PDF。

        Returns:
            BatchSummary: 汇总结果。
        """
        summary = BatchSummary(total_queries=len(queries))

        for query in queries:
            try:
                result = await self.search(query)
                summary.results.append(result)
                summary.total_papers_found += result.total_found

                if download and result.papers:
                    dl_results = await self.download_many(result.papers)
                    summary.downloads.extend(dl_results)
                    summary.total_downloaded += sum(1 for d in dl_results if d.success)
                    summary.total_failed += sum(1 for d in dl_results if not d.success)
            except Exception as e:
                logger.error(f"批量搜索失败 ({query.keywords[:60]}): {e}")
                summary.results.append(
                    SearchResult(query=query, errors=[str(e)])
                )

        return summary

    async def batch_search_from_file(
        self,
        file_path: str,
        download: bool = False,
        default_sources: Optional[list[SourceType]] = None,
    ) -> BatchSummary:
        """从 JSON 或 CSV 文件读取查询列表并执行。

        JSON 格式:
        [
            {"keywords": "...", "sources": ["arxiv", "pubmed"], "max_results": 10},
            {"title": "Attention Is All You Need", "sources": ["arxiv"]}
        ]

        CSV 格式（简单）:
        keywords,sources,max_results
        "transformer",arxiv,20
        "attention mechanism",arxiv|semantic_scholar,10
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"查询文件不存在: {file_path}")

        if path.suffix.lower() == ".json":
            queries = self._parse_json_queries(path, default_sources)
        elif path.suffix.lower() == ".csv":
            queries = self._parse_csv_queries(path, default_sources)
        else:
            raise ValueError(f"不支持的文件格式: {path.suffix}，只支持 .json 和 .csv")

        return await self.batch_search(queries, download=download)

    # ── 工具方法 ──────────────────────────────────────────

    def _deduplicate(self, papers: list[Paper]) -> list[Paper]:
        """按 DOI / 标题相似度去重。

        规则:
        1. 如果有 DOI 且相同 → 去重（保留信息更完整的那篇）
        2. 标题相似度 >= 85% → 去重
        """
        if len(papers) <= 1:
            return papers

        uniq: list[Paper] = []
        seen_dois: set[str] = set()

        for paper in papers:
            # DOI 去重
            if paper.doi:
                doi_lower = paper.doi.lower()
                if doi_lower in seen_dois:
                    # 找到已有的，保留信息更完整的
                    for i, existing in enumerate(uniq):
                        if existing.doi and existing.doi.lower() == doi_lower:
                            uniq[i] = self._merge_papers(existing, paper)
                            break
                    continue
                seen_dois.add(doi_lower)
                uniq.append(paper)
                continue

            # 标题相似度去重
            is_dup = False
            for i, existing in enumerate(uniq):
                sim = _title_similarity(paper.title, existing.title)
                if sim >= _DEDUP_TITLE_SIMILARITY:
                    uniq[i] = self._merge_papers(existing, paper)
                    is_dup = True
                    break
            if not is_dup:
                uniq.append(paper)

        return uniq

    @staticmethod
    def _merge_papers(a: Paper, b: Paper) -> Paper:
        """合并两篇论文的信息，保留更完整的字段。"""
        merged = a.model_copy()
        # 用非空值覆盖空值
        if not merged.abstract and b.abstract:
            merged.abstract = b.abstract
        if not merged.pdf_url and b.pdf_url:
            merged.pdf_url = b.pdf_url
        if not merged.doi and b.doi:
            merged.doi = b.doi
        if not merged.arxiv_id and b.arxiv_id:
            merged.arxiv_id = b.arxiv_id
        if not merged.pmid and b.pmid:
            merged.pmid = b.pmid
        if not merged.citation_count and b.citation_count:
            merged.citation_count = b.citation_count
        if not merged.venue and b.venue:
            merged.venue = b.venue
        if not merged.keywords and b.keywords:
            merged.keywords = b.keywords
        # 保留更多作者信息
        if len(b.authors) > len(merged.authors):
            merged.authors = b.authors
        return merged

    def _parse_json_queries(
        self, path: Path, default_sources: Optional[list[SourceType]] = None
    ) -> list[SearchQuery]:
        """从 JSON 文件解析 SearchQuery 列表。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("JSON 文件必须是查询对象数组")

        queries: list[SearchQuery] = []
        for item in data:
            # 处理 sources 字符串 → SourceType
            if "sources" in item and isinstance(item["sources"], list):
                item["sources"] = [
                    SourceType(s) if isinstance(s, str) else s
                    for s in item["sources"]
                ]
            elif default_sources:
                item["sources"] = default_sources
            queries.append(SearchQuery(**item))
        return queries

    def _parse_csv_queries(
        self, path: Path, default_sources: Optional[list[SourceType]] = None
    ) -> list[SearchQuery]:
        """从 CSV 文件解析 SearchQuery 列表。"""
        import csv

        queries: list[SearchQuery] = []
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                kwargs: dict = {}
                if "keywords" in row:
                    kwargs["keywords"] = row["keywords"]
                if "title" in row and row["title"]:
                    kwargs["title"] = row["title"]
                if "author" in row and row["author"]:
                    kwargs["author"] = row["author"]
                if "doi" in row and row["doi"]:
                    kwargs["doi"] = row["doi"]
                if "max_results" in row and row["max_results"]:
                    kwargs["max_results"] = int(row["max_results"])
                if "sources" in row and row["sources"]:
                    kwargs["sources"] = [
                        SourceType(s.strip())
                        for s in row["sources"].split("|")
                    ]
                elif default_sources:
                    kwargs["sources"] = default_sources
                queries.append(SearchQuery(**kwargs))
        return queries

    # ── 健康检查 ──────────────────────────────────────────

    async def health_check(self) -> dict[str, bool]:
        """检查所有注册 Provider 的可用性。"""
        results = {}
        for source_type in list_providers():
            try:
                provider = self._get_provider(source_type)
                results[source_type.value] = await provider.health_check()
            except Exception:
                results[source_type.value] = False
        return results

    async def close(self) -> None:
        """关闭所有 Provider 的资源。"""
        for provider in self._providers.values():
            if hasattr(provider, "close"):
                await provider.close()
        self._providers.clear()


def _title_similarity(a: str, b: str) -> float:
    """计算两个标题的相似度（0.0 - 1.0）。"""
    # 标准化：小写，去除多余空白
    a_norm = " ".join(a.lower().split())
    b_norm = " ".join(b.lower().split())
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()
