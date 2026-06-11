"""IEEE Xplore Provider — 通过 API 搜索 + Playwright 下载 PDF.

注意: IEEE API 免费（200 req/day），PDF 下载需要校内IP。
Phase 1B 完整实现。
"""

import logging

from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)


@register(SourceType.IEEE)
class IeeeProvider(BaseProvider):
    """IEEE Xplore 论文搜索与下载 Provider (Phase 1B - Stub).

    完整实现需要:
    - IEEE Xplore Metadata Search API (免费注册，200 req/day)
    - Playwright PDF 下载（校内IP）
    - Cookie 缓存
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.IEEE

    async def search(self, query: SearchQuery) -> list[Paper]:
        """搜索 IEEE Xplore（待实现）。"""
        logger.warning("IEEE Provider 尚未实现 (Phase 1B)")
        return []

    async def resolve_pdf_url(self, paper: Paper) -> str | None:
        """解析 IEEE PDF URL（待实现）。"""
        return None

    async def health_check(self) -> bool:
        """检查 IEEE 连通性（待实现）。"""
        return False
