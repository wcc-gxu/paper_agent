"""arXiv Provider — 通过 arXiv API 搜索和下载预印本论文."""

import logging
from pathlib import Path
from typing import Optional

import arxiv

from ..config import Config
from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)


@register(SourceType.ARXIV)
class ArxivProvider(BaseProvider):
    """arXiv 论文搜索与下载 Provider。

    使用 `arxiv` PyPI 库，无需 API Key。
    默认请求间隔 3 秒（符合 arXiv API 使用条款）。
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.ARXIV

    async def search(self, query: SearchQuery) -> list[Paper]:
        """通过 arXiv API 搜索论文。"""
        client = arxiv.Client(
            page_size=min(query.max_results, 100),
            delay_seconds=3,
            num_retries=3,
        )

        search_text = self._build_search_text(query)
        if not search_text:
            logger.warning("arXiv: 搜索词为空，跳过搜索")
            return []

        # 构建 arXiv 搜索对象
        search = arxiv.Search(
            query=search_text,
            max_results=query.max_results,
            sort_by=arxiv.SortCriterion.Relevance,
        )

        papers: list[Paper] = []
        try:
            # arxiv 库的 client.results() 是同步的，在线程池中执行
            import asyncio
            results = await asyncio.to_thread(
                lambda: list(client.results(search))
            )
        except Exception as e:
            logger.error(f"arXiv 搜索失败: {e}")
            return []

        for result in results:
            paper = Paper(
                title=result.title or "Untitled",
                authors=[a.name for a in (result.authors or [])],
                year=result.published.year if result.published else None,
                abstract=result.summary.replace("\n", " ") if result.summary else None,
                doi=result.doi,
                arxiv_id=result.get_short_id(),
                source=SourceType.ARXIV,
                source_url=result.entry_id if result.entry_id else None,
                pdf_url=result.pdf_url if result.pdf_url else None,
                venue=result.journal_ref if result.journal_ref else None,
            )
            papers.append(paper)

        logger.info(f"arXiv: 找到 {len(papers)} 篇论文 (query={search_text[:60]})")
        return papers

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """arXiv 论文的 PDF URL 已知（search 阶段已获取）。"""
        if paper.pdf_url:
            return paper.pdf_url
        # 如果有 arxiv_id，构建标准 PDF URL
        if paper.arxiv_id:
            return f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"
        return None

    async def health_check(self) -> bool:
        """检查 arXiv API 连通性。"""
        try:
            import asyncio
            client = arxiv.Client(page_size=1, delay_seconds=3)
            search = arxiv.Search(query="test", max_results=1)
            await asyncio.to_thread(lambda: list(client.results(search)))
            return True
        except Exception:
            return False
