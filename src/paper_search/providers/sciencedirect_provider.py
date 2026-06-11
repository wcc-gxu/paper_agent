"""ScienceDirect (Elsevier) Provider — API 搜索 + Playwright PDF 下载.

Elsevier ScienceDirect Search API:
- 免费注册: dev.elsevier.com
- 5000 req/week (足够个人使用)
- 搜索端点: GET https://api.elsevier.com/content/search/sciencedirect

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

# Elsevier API 端点
ELSEVIER_API_BASE = "https://api.elsevier.com/content"


@register(SourceType.SCIENCEDIRECT)
class ScienceDirectProvider(BaseProvider):
    """Elsevier ScienceDirect 论文搜索与下载 Provider。

    搜索: Elsevier ScienceDirect Search API
    下载: Playwright 浏览器 + 校内IP
    """

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config)
        self._cookie_cache = None

    @property
    def source_type(self) -> SourceType:
        return SourceType.SCIENCEDIRECT

    def _get_api_headers(self) -> dict:
        """构建 Elsevier API 请求头。"""
        headers = {
            "Accept": "application/json",
            "X-ELS-APIKey": self.config.elsevier_api_key,
        }
        return headers

    def _get_http_client(self) -> httpx.AsyncClient:
        """创建通用 HTTP 客户端。"""
        return httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/json,*/*",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    # ── 搜索 ──────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Paper]:
        """通过 Elsevier ScienceDirect API 搜索论文。"""
        search_text = self._build_search_text(query)
        if not search_text:
            logger.warning("ScienceDirect: 搜索词为空")
            return []

        if not self.config.elsevier_api_key:
            logger.warning("ScienceDirect: 未设置 ELSEVIER_API_KEY")

        # 构建搜索查询
        search_query = search_text
        if query.year_from:
            search_query += f" AND PUBYEAR > {query.year_from - 1}"
        if query.year_to:
            search_query += f" AND PUBYEAR < {query.year_to + 1}"

        params = {
            "query": search_query,
            "count": min(query.max_results, 25),
            "start": 0,
            "view": "COMPLETE",
        }

        async with httpx.AsyncClient(
            base_url=ELSEVIER_API_BASE,
            headers=self._get_api_headers(),
            timeout=30.0,
        ) as client:
            try:
                resp = await client.get(
                    "/search/sciencedirect",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.warning("ScienceDirect API: 403 — API Key 可能无效或超限")
                elif e.response.status_code == 401:
                    logger.warning("ScienceDirect API: 401 — API Key 无效")
                else:
                    logger.error(f"ScienceDirect API 搜索失败: HTTP {e.response.status_code}")
                return []
            except Exception as e:
                logger.error(f"ScienceDirect API 搜索异常: {e}")
                return []

        papers = self._parse_search_response(data)
        logger.info(f"ScienceDirect: 找到 {len(papers)} 篇论文 (query={search_text[:60]})")
        return papers

    def _parse_search_response(self, data: dict) -> list[Paper]:
        """解析 Elsevier API 搜索响应。"""
        papers: list[Paper] = []

        results = data.get("search-results", {})
        entries = results.get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]  # 单结果时是 dict

        for entry in entries:
            try:
                # 标题
                title = entry.get("dc:title", "Untitled")

                # 作者
                author_str = entry.get("dc:creator", "")
                authors = [a.strip() for a in author_str.split(";") if a.strip()] if author_str else []

                # 年份
                year = None
                date_str = entry.get("prism:coverDate", "")
                if date_str:
                    from datetime import datetime
                    try:
                        year = datetime.strptime(date_str[:10], "%Y-%m-%d").year
                    except ValueError:
                        try:
                            year = int(date_str[:4])
                        except (ValueError, TypeError):
                            pass

                # DOI
                doi = entry.get("prism:doi")

                # PII (用于构造 ScienceDirect 页面 URL)
                pii = entry.get("pii")

                # URL
                source_url = None
                if pii:
                    source_url = f"https://www.sciencedirect.com/science/article/pii/{pii}"
                elif doi:
                    source_url = f"https://www.sciencedirect.com/science/article/pii/{doi}"

                # 摘要
                abstract = entry.get("dc:description")

                # 期刊
                venue = entry.get("prism:publicationName")

                # 关键词
                keyword_str = entry.get("authkeywords", "")
                keywords = [k.strip() for k in keyword_str.split(";")] if keyword_str else []

                paper = Paper(
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    doi=doi,
                    source=SourceType.SCIENCEDIRECT,
                    source_url=source_url,
                    venue=venue,
                    keywords=keywords,
                    citation_count=entry.get("citedby-count"),
                )
                papers.append(paper)

            except Exception as e:
                logger.debug(f"ScienceDirect: 解析论文失败: {e}")

        return papers

    # ── PDF 下载 ──────────────────────────────────────────

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """尝试解析 ScienceDirect 论文的 PDF URL。

        ScienceDirect PDF URL 模式:
        https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true
        """
        if paper.source_url and "science/article/pii/" in paper.source_url:
            # 构造 PDF 下载 URL
            return f"{paper.source_url}/pdfft?isDTMRedir=true&download=true"

        return None

    async def download_pdf(self, paper: Paper, target_dir: Path) -> Optional[Path]:
        """下载 ScienceDirect 论文 PDF。

        优先直接 HTTP 下载（校内IP自动授权），失败则用 Playwright。
        """
        from ..downloaders.filename_utils import build_pdf_filename, ensure_unique_path

        filename = build_pdf_filename(paper, naming_format="author_year_title")
        target_path = ensure_unique_path(target_dir / filename)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 策略 1: 直接 HTTP 下载（校内IP下通常可用）
        pdf_url = await self.resolve_pdf_url(paper)
        if pdf_url:
            from ..downloaders.http_downloader import HttpDownloader
            downloader = HttpDownloader(config=self.config)
            success = await downloader.download(pdf_url, target_path)
            await downloader.close()
            if success:
                return target_path
            logger.info("ScienceDirect: 直接下载失败，尝试 Playwright")

        # 策略 2: Playwright 浏览器下载
        if not paper.source_url:
            logger.warning("ScienceDirect: 无 source_url，无法下载")
            return None

        return await self._playwright_download(paper.source_url, target_path)

    async def _playwright_download(self, page_url: str, target_path: Path) -> Optional[Path]:
        """通过 Playwright 下载 ScienceDirect PDF。"""
        from ..downloaders.browser_downloader import CookieCache, PlaywrightDownloader

        if self._cookie_cache is None:
            self._cookie_cache = CookieCache(self.config.cookie_cache_dir)

        cookies = self._cookie_cache.load("sciencedirect")

        try:
            async with PlaywrightDownloader(headless=True) as dl:
                result = await dl.download(
                    url=page_url,
                    target_path=target_path,
                    auth_cookies=cookies,
                    pdf_selector=(
                        'a[href*="pdfft"], '
                        'a[href*="/pdf/"], '
                        'a.pdf-download-link, '
                        'a.download-pdf-link, '
                        'a[aria-label*="PDF"], '
                        'a.link.pdf-link'
                    ),
                )

                if result:
                    saved_path, new_cookies = result
                    if new_cookies:
                        self._cookie_cache.save("sciencedirect", new_cookies)
                    return saved_path

                # Cookie 过期重试
                if cookies:
                    logger.info("ScienceDirect: Cookie 可能过期，清除缓存重试")
                    self._cookie_cache.clear("sciencedirect")
                    async with PlaywrightDownloader(headless=True) as dl2:
                        result2 = await dl2.download(
                            url=page_url,
                            target_path=target_path,
                            auth_cookies=None,
                            pdf_selector='a[href*="pdfft"], a[href*="pdf"], a.download-pdf-link',
                        )
                        if result2:
                            saved_path2, new_cookies2 = result2
                            if new_cookies2:
                                self._cookie_cache.save("sciencedirect", new_cookies2)
                            return saved_path2

        except Exception as e:
            logger.error(f"ScienceDirect Playwright 下载异常: {e}")

        return None

    # ── 健康检查 ──────────────────────────────────────────

    async def health_check(self) -> bool:
        """检查 ScienceDirect 连通性。"""
        # 检查校内IP直接访问
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://www.sciencedirect.com/")
                if resp.status_code == 200 and "sciencedirect" in resp.text.lower():
                    return True
                if resp.status_code == 403:
                    logger.info("ScienceDirect: 访问被拒绝 — 当前IP不在授权范围内")
        except Exception:
            pass

        # 如果有 API key，检查 API
        if self.config.elsevier_api_key:
            try:
                async with httpx.AsyncClient(
                    base_url=ELSEVIER_API_BASE,
                    headers=self._get_api_headers(),
                    timeout=15.0,
                ) as client:
                    resp = await client.get(
                        "/search/sciencedirect",
                        params={"query": "test", "count": 1},
                    )
                    return resp.status_code == 200
            except Exception:
                pass

        return False
