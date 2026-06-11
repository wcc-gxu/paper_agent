"""CNKI (知网) Provider — 通过校内IP搜索和下载中文论文.

注意: 需要在校内网络环境下使用（校园网IP直连）。
Phase 1B 完整实现。
"""

import logging

from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)


@register(SourceType.CNKI)
class CnkiProvider(BaseProvider):
    """知网论文搜索与下载 Provider (Phase 1B - Stub).

    完整实现需要:
    - requests 搜索 kns.cnki.net
    - BeautifulSoup 解析搜索结果
    - 提取 PDF/CAJ 下载链接
    - CAPTCHA 处理 (pytesseract + 手动降级)
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.CNKI

    async def search(self, query: SearchQuery) -> list[Paper]:
        """搜索知网（待实现）。"""
        logger.warning("CNKI Provider 尚未实现 (Phase 1B)")
        return []

    async def resolve_pdf_url(self, paper: Paper) -> str | None:
        """解析知网 PDF URL（待实现）。"""
        return None

    async def health_check(self) -> bool:
        """检查知网连通性（待实现）。"""
        return False
