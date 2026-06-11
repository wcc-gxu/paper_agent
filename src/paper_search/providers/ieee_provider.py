"""IEEE Xplore Provider — API 搜索 + Playwright PDF 下载.

IEEE Xplore Metadata Search API:
- 免费注册: developer.ieee.org
- 200 req/day
- 搜索端点: GET https://ieeexploreapi.ieee.org/api/v1/search/articles

PDF 下载: 校内IP环境下，通过 Playwright 访问论文页面下载。
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx

from ..config import Config
from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)

# IEEE API 端点
IEEE_API_BASE = "https://ieeexploreapi.ieee.org/api/v1"


@register(SourceType.IEEE)
class IeeeProvider(BaseProvider):
    """IEEE Xplore 论文搜索与下载 Provider。

    搜索: IEEE Xplore Metadata Search API
    下载: Playwright 浏览器 + 校内IP
    """

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config)
        self._cookie_cache = None

    @property
    def source_type(self) -> SourceType:
        return SourceType.IEEE

    def _get_api_client(self) -> httpx.AsyncClient:
        """创建 IEEE API 客户端。"""
        params = {}
        if self.config.ieee_api_key:
            params["apikey"] = self.config.ieee_api_key
        return httpx.AsyncClient(
            base_url=IEEE_API_BASE,
            params=params if self.config.ieee_api_key else {},
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    # ── 搜索 ──────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Paper]:
        """通过 IEEE API 搜索论文。"""
        search_text = self._build_search_text(query)
        if not search_text:
            logger.warning("IEEE: 搜索词为空")
            return []

        if not self.config.ieee_api_key:
            logger.warning("IEEE: 未设置 IEEE_API_KEY，搜索可能受限")

        params = {
            "querytext": search_text,
            "max_records": min(query.max_results, 50),
            "format": "json",
        }
        if query.year_from:
            params["start_year"] = str(query.year_from)
        if query.year_to:
            params["end_year"] = str(query.year_to)

        async with self._get_api_client() as client:
            try:
                resp = await client.get(
                    "/search/articles",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.warning("IEEE API: 403 — API Key 可能无效或超限 (200 req/day)")
                else:
                    logger.error(f"IEEE API 搜索失败: HTTP {e.response.status_code}")
                return []
            except Exception as e:
                logger.error(f"IEEE API 搜索异常: {e}")
                return []

        papers = self._parse_search_response(data)
        logger.info(f"IEEE: 找到 {len(papers)} 篇论文 (query={search_text[:60]})")
        return papers

    def _parse_search_response(self, data: dict) -> list[Paper]:
        """解析 IEEE API 搜索响应。"""
        papers: list[Paper] = []
        articles = data.get("articles", [])

        for item in articles:
            try:
                # 解析作者
                authors_data = item.get("authors", {}).get("authors", [])
                authors = []
                for a in authors_data:
                    name = a.get("full_name", "")
                    if name:
                        authors.append(name)

                # 年份
                year = None
                py = item.get("publication_year")
                if py:
                    try:
                        year = int(py)
                    except (ValueError, TypeError):
                        pass

                # DOI
                doi = item.get("doi")

                # URL
                article_number = item.get("article_number")
                source_url = None
                if article_number:
                    source_url = f"https://ieeexplore.ieee.org/document/{article_number}"
                elif doi:
                    source_url = f"https://doi.org/{doi}"

                # PDF URL（OA 情况下可能直接可用）
                pdf_url = item.get("pdf_url")

                # 摘要
                abstract = item.get("abstract")

                # 期刊/会议
                venue = item.get("publication_title")

                # 标题
                title = item.get("title", "Untitled")

                paper = Paper(
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    doi=doi,
                    source=SourceType.IEEE,
                    source_url=source_url,
                    pdf_url=pdf_url,
                    citation_count=item.get("citing_paper_count"),
                    venue=venue,
                )
                papers.append(paper)

            except Exception as e:
                logger.debug(f"IEEE: 解析论文失败: {e}")

        return papers

    # ── PDF 下载 ──────────────────────────────────────────

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """尝试获取 IEEE 论文的 PDF URL。

        策略:
        1. 如果 API 已返回 pdf_url (OA论文)，直接使用
        2. 如果有 article_number，尝试构造 stampPDF URL
        3. 否则需要通过 Playwright 获取
        """
        if paper.pdf_url:
            return paper.pdf_url

        # 从 URL 中提取 article number
        if paper.source_url:
            import re
            match = re.search(r"/document/(\d+)", paper.source_url)
            if match:
                article_num = match.group(1)
                return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={article_num}"

        return None

    async def download_pdf(self, paper: Paper, target_dir: Path) -> Optional[Path]:
        """下载 IEEE 论文 PDF。

        优先尝试直接 HTTP 下载，失败则回退到 Playwright。
        """
        from ..downloaders.filename_utils import build_pdf_filename, ensure_unique_path

        filename = build_pdf_filename(paper, naming_format="author_year_title")
        target_path = ensure_unique_path(target_dir / filename)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 策略 1: 尝试直接 HTTP 下载（OA 论文或 stampPDF URL）
        pdf_url = await self.resolve_pdf_url(paper)
        if pdf_url:
            from ..downloaders.http_downloader import HttpDownloader
            downloader = HttpDownloader(config=self.config)
            success = await downloader.download(pdf_url, target_path)
            await downloader.close()
            if success:
                return target_path
            logger.info("IEEE: 直接下载失败，尝试 Playwright")

        # 策略 2: Playwright 浏览器下载
        if not paper.source_url:
            logger.warning("IEEE: 无 source_url，无法使用 Playwright 下载")
            return None

        return await self._playwright_download(paper.source_url, target_path)

    async def _playwright_download(self, page_url: str, target_path: Path) -> Optional[Path]:
        """通过 Playwright 浏览器下载 IEEE PDF。"""
        from ..downloaders.browser_downloader import CookieCache, PlaywrightDownloader

        # 初始化 cookie 缓存
        if self._cookie_cache is None:
            self._cookie_cache = CookieCache(self.config.cookie_cache_dir)

        # 尝试加载缓存 cookies
        cookies = self._cookie_cache.load("ieee")

        try:
            async with PlaywrightDownloader(headless=True) as dl:
                result = await dl.download(
                    url=page_url,
                    target_path=target_path,
                    auth_cookies=cookies,
                    pdf_selector=(
                        'a[href*="stampPDF"], '
                        'a[href*="pdf"], '
                        'a.pdf-download-btn, '
                        'button[data-type="pdf"], '
                        'a.stats-document-pdf-download'
                    ),
                )

                if result:
                    saved_path, new_cookies = result
                    # 缓存新 cookies
                    if new_cookies:
                        self._cookie_cache.save("ieee", new_cookies)
                    return saved_path

                # 如果 cookies 过期，清除缓存并重试一次
                if cookies:
                    logger.info("IEEE: Cookie 可能过期，清除缓存重试")
                    self._cookie_cache.clear("ieee")

                    # 重新打开浏览器（不使用 cookies）
                    async with PlaywrightDownloader(headless=True) as dl2:
                        result2 = await dl2.download(
                            url=page_url,
                            target_path=target_path,
                            auth_cookies=None,
                            pdf_selector=(
                                'a[href*="stampPDF"], '
                                'a[href*="pdf"], '
                                'a.pdf-download-btn, '
                                'button[data-type="pdf"]'
                            ),
                        )
                        if result2:
                            saved_path2, new_cookies2 = result2
                            if new_cookies2:
                                self._cookie_cache.save("ieee", new_cookies2)
                            return saved_path2

        except Exception as e:
            logger.error(f"IEEE Playwright 下载异常: {e}")

        return None

    # ── 健康检查 ──────────────────────────────────────────

    async def health_check(self) -> bool:
        """检查 IEEE 连通性。"""
        # 检查 API 连通性
        if self.config.ieee_api_key:
            async with self._get_api_client() as client:
                try:
                    resp = await client.get(
                        "/search/articles",
                        params={"querytext": "test", "max_records": 1, "format": "json"},
                    )
                    if resp.status_code == 200:
                        return True
                    if resp.status_code == 403:
                        logger.info("IEEE API: API Key 可能超限")
                except Exception:
                    pass

        # 检查校内IP直接访问
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://ieeexplore.ieee.org/")
                if resp.status_code == 200:
                    return True
                if resp.status_code == 403:
                    logger.info("IEEE: 访问被拒绝 — 当前IP不在授权范围内")
        except Exception:
            pass

        return False
