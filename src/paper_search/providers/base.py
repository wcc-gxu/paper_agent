"""Provider 抽象基类 — 所有论文搜索来源的接口定义."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..config import Config
from ..models import Paper, SearchQuery, SourceType


class BaseProvider(ABC):
    """所有论文搜索 Provider 的抽象基类。

    每个具体 Provider 实现 search() 和 download_pdf()。
    resolve_pdf_url() 有默认实现。
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

    # ── 必须实现的抽象方法 ──────────────────────────────────

    @property
    @abstractmethod
    def source_type(self) -> SourceType:
        """返回此 Provider 对应的来源类型。"""
        ...

    @abstractmethod
    async def search(self, query: SearchQuery) -> list[Paper]:
        """搜索论文。

        Args:
            query: 统一搜索查询对象。

        Returns:
            匹配的论文列表（由 Provider 负责限制条数）。
        """
        ...

    @abstractmethod
    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """解析论文的可下载 PDF URL。

        Args:
            paper: 论文对象（至少包含 doi / title 等标识信息）。

        Returns:
            可下载的 PDF URL，如果无法获取则返回 None。
        """
        ...

    # ── 可覆盖的方法 ──────────────────────────────────────

    async def download_pdf(self, paper: Paper, target_dir: Path) -> Optional[Path]:
        """下载论文 PDF 到指定目录。

        默认实现：先 resolve_pdf_url，再用 HttpDownloader 下载。
        需要特殊认证的 Provider（如 IEEE）可以覆盖此方法。

        Args:
            paper: 论文对象。
            target_dir: 目标目录。

        Returns:
            保存的 PDF 文件路径，失败返回 None。
        """
        from ..downloaders.http_downloader import HttpDownloader
        from ..downloaders.filename_utils import build_pdf_filename, ensure_unique_path

        pdf_url = await self.resolve_pdf_url(paper)
        if not pdf_url:
            return None

        filename = build_pdf_filename(
            paper, naming_format="author_year_title"
        )
        target_path = ensure_unique_path(target_dir / filename)

        downloader = HttpDownloader(config=self.config)
        success = await downloader.download(pdf_url, target_path)
        return target_path if success else None

    async def health_check(self) -> bool:
        """检查此 Provider 当前是否可用。

        默认返回 True。子类可覆盖以实现真实的连通性检查。
        """
        return True

    # ── 工具方法 ──────────────────────────────────────────

    def _build_search_text(self, query: SearchQuery) -> str:
        """根据 SearchQuery 构建适合该 Provider 的搜索文本。"""
        if query.doi:
            return query.doi
        if query.title:
            return query.title
        parts = []
        if query.author:
            parts.append(query.author)
        if query.keywords:
            parts.append(query.keywords)
        return " ".join(parts) if parts else ""
