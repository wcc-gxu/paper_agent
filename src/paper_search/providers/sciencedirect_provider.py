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

    # ── PDF 下载 (官方 Article Retrieval API) ──────────────

    # Elsevier Article Retrieval API 端点
    ARTICLE_API = "https://api.elsevier.com/content/article/doi"

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """通过 Elsevier Article Retrieval API 获取 PDF URL。

        官方文档: https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl
        端点: GET /content/article/doi/{DOI}?httpAccept=application/pdf

        需要: API Key (免费注册) + 机构订阅（校内IP可下载全文）
        """
        if not paper.doi:
            return None
        if not self.config.elsevier_api_key:
            logger.warning("ScienceDirect: 未设置 ELSEVIER_API_KEY")
            return None

        return (
            f"{self.ARTICLE_API}/{paper.doi}"
            f"?APIKey={self.config.elsevier_api_key}"
            f"&httpAccept=application/pdf"
        )

    async def download_pdf(self, paper: Paper, target_dir: Path) -> Optional[Path]:
        """通过官方 Article Retrieval API 下载 PDF。

        端点: GET /content/article/doi/{DOI}?httpAccept=application/pdf
        权限: API Key + 机构订阅（校内IP）
        """
        from ..downloaders.filename_utils import build_pdf_filename, ensure_unique_path

        if not paper.doi:
            logger.warning("ScienceDirect: 无 DOI，无法下载")
            return None
        if not self.config.elsevier_api_key:
            logger.warning("ScienceDirect: 未设置 API Key")
            return None

        filename = build_pdf_filename(paper, naming_format="author_year_title")
        target_path = ensure_unique_path(target_dir / filename)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"{self.ARTICLE_API}/{paper.doi}"
        params = {
            "APIKey": self.config.elsevier_api_key,
            "httpAccept": "application/pdf",
        }

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=60.0,
                headers={"Accept": "application/pdf"},
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()

                content = resp.content
                if len(content) >= 4 and content[:4] == b"%PDF":
                    target_path.write_bytes(content)
                    logger.info(
                        f"ScienceDirect PDF: {target_path} ({len(content)/1024:.0f} KB)"
                    )
                    return target_path
                else:
                    # 可能返回的是 XML 错误（无权限）
                    text = content.decode("utf-8", errors="replace")[:200]
                    if "service-error" in text.lower() or "quota" in text.lower():
                        logger.warning(
                            f"ScienceDirect: 无全文权限 (DOI: {paper.doi})"
                        )
                    else:
                        logger.warning(f"ScienceDirect: 非 PDF 响应: {text[:100]}")
                    return None

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                logger.warning(
                    f"ScienceDirect: 403 无权限或超出配额 (DOI: {paper.doi})"
                )
            elif e.response.status_code == 401:
                logger.warning("ScienceDirect: 401 API Key 无效")
            else:
                logger.error(f"ScienceDirect PDF HTTP {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"ScienceDirect PDF 异常: {e}")
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
