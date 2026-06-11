"""ScienceDirect (Elsevier) Provider — API 搜索 + Playwright PDF 下载.

注意: Elsevier Search API 免费（5000 req/week），PDF 下载需要校内IP。
Phase 1B 完整实现。
"""

import logging

from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)


@register(SourceType.SCIENCEDIRECT)
class ScienceDirectProvider(BaseProvider):
    """Elsevier ScienceDirect 论文搜索与下载 Provider (Phase 1B - Stub).

    完整实现需要:
    - Elsevier Search API (免费注册，5000 req/week)
    - Playwright PDF 下载（校内IP）
    - 检查 IP 授权状态
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.SCIENCEDIRECT

    async def search(self, query: SearchQuery) -> list[Paper]:
        """搜索 ScienceDirect（待实现）。"""
        logger.warning("ScienceDirect Provider 尚未实现 (Phase 1B)")
        return []

    async def resolve_pdf_url(self, paper: Paper) -> str | None:
        """解析 ScienceDirect PDF URL（待实现）。"""
        return None

    async def health_check(self) -> bool:
        """检查 ScienceDirect 连通性（待实现）。"""
        return False
