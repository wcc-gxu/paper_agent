"""Semantic Scholar Provider — 通过 S2 REST API 搜索论文."""

import logging
from typing import Optional

import httpx

from ..config import Config
from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)

# Semantic Scholar API 基础 URL
S2_API_BASE = "https://api.semanticscholar.org/graph/v1"

# 请求的字段
S2_PAPER_FIELDS = (
    "title,authors,year,abstract,externalIds,openAccessPdf,"
    "url,citationCount,journal,publicationVenue"
)


@register(SourceType.SEMANTIC_SCHOLAR)
class SemanticScholarProvider(BaseProvider):
    """Semantic Scholar 论文搜索与下载 Provider。

    使用官方 REST API (graph/v1)，可选 API Key 提高速率限制：
    - 无 Key: 100 req/min
    - 有 Key: 更高限制
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.SEMANTIC_SCHOLAR

    def _make_client(self) -> httpx.AsyncClient:
        """创建带认证头的 httpx 客户端。"""
        headers = {
            "User-Agent": "paper-search/0.1 (mailto:user@example.com)",
        }
        if self.config.semantic_scholar_api_key:
            headers["x-api-key"] = self.config.semantic_scholar_api_key
        return httpx.AsyncClient(
            base_url=S2_API_BASE,
            headers=headers,
            timeout=self.config.download_timeout,
        )

    async def search(self, query: SearchQuery) -> list[Paper]:
        """通过 Semantic Scholar API 搜索论文。"""
        search_text = self._build_search_text(query)
        if not search_text:
            logger.warning("Semantic Scholar: 搜索词为空，跳过搜索")
            return []

        # 构建查询 URL
        params: dict = {
            "query": search_text,
            "limit": min(query.max_results, 100),
            "fields": S2_PAPER_FIELDS,
        }
        if query.year_from or query.year_to:
            year_filter = ""
            if query.year_from:
                year_filter += f"{query.year_from}-"
            if query.year_to:
                year_filter += str(query.year_to)
            params["year"] = year_filter

        async with self._make_client() as client:
            try:
                resp = await self._request_with_retry(client, "/paper/search", params)
            except httpx.HTTPStatusError as e:
                if query.title or query.doi:
                    logger.info("Semantic Scholar: 尝试标题/DOI 匹配搜索")
                    match_params = {"query": search_text, "fields": S2_PAPER_FIELDS}
                    try:
                        resp = await self._request_with_retry(client, "/paper/search/match", match_params)
                    except httpx.HTTPStatusError:
                        return []
                else:
                    if e.response.status_code == 429:
                        logger.warning("Semantic Scholar: 速率限制 (429)，稍后重试")
                    else:
                        logger.warning(f"Semantic Scholar 搜索失败: HTTP {e.response.status_code}")
                    return []

            data = resp.json()
            # 处理不同的响应结构
            if "data" in data:
                items = data["data"]
            elif "paperId" in data:  # match 端点返回单篇
                items = [data]
            else:
                items = []

            papers: list[Paper] = []
            for item in items:
                paper = self._parse_paper(item)
                if paper:
                    papers.append(paper)

        logger.info(f"Semantic Scholar: 找到 {len(papers)} 篇论文 (query={search_text[:60]})")
        return papers

    def _parse_paper(self, item: dict) -> Optional[Paper]:
        """将 API 响应解析为 Paper 模型。"""
        try:
            # 提取开放获取 PDF URL
            oa_info = item.get("openAccessPdf") or {}
            pdf_url = oa_info.get("url")

            # 提取外部 ID
            ext_ids = item.get("externalIds") or {}
            doi = ext_ids.get("DOI")
            arxiv_id = ext_ids.get("ArXiv")

            # 提取作者
            authors_raw = item.get("authors") or []
            authors = [a.get("name", "Unknown") for a in authors_raw]

            # 提取会议/期刊
            venue = None
            if item.get("publicationVenue"):
                venue = item["publicationVenue"].get("name")
            elif item.get("journal"):
                venue = item["journal"].get("name")

            return Paper(
                title=item.get("title", "Untitled"),
                authors=authors,
                year=item.get("year"),
                abstract=item.get("abstract"),
                doi=doi,
                arxiv_id=arxiv_id,
                source=SourceType.SEMANTIC_SCHOLAR,
                source_url=item.get("url"),
                pdf_url=pdf_url,
                citation_count=item.get("citationCount"),
                venue=venue,
            )
        except Exception as e:
            logger.warning(f"Semantic Scholar: 解析论文失败: {e}")
            return None

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """Semantic Scholar 论文的 PDF URL 通常在搜索阶段已获取。

        如果没有，尝试用 DOI 查找 OA 版本。
        """
        if paper.pdf_url:
            return paper.pdf_url

        # 如果有 DOI，查询 Semantic Scholar 获取 OA PDF
        if paper.doi:
            async with self._make_client() as client:
                try:
                    resp = await client.get(
                        f"/paper/DOI:{paper.doi}",
                        params={"fields": "openAccessPdf"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    oa = data.get("openAccessPdf") or {}
                    return oa.get("url")
                except Exception:
                    pass
        return None

    async def _request_with_retry(
        self, client: httpx.AsyncClient, path: str, params: dict,
        max_retries: int = 3, base_delay: float = 1.5,
    ) -> httpx.Response:
        """带指数退避的请求重试，处理 429 速率限制。"""
        import asyncio

        for attempt in range(max_retries):
            try:
                resp = await client.get(path, params=params)
                if resp.status_code == 429:
                    delay = base_delay * (2 ** attempt)
                    logger.debug(f"Semantic Scholar 429 — {delay:.1f}s 后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError(f"Semantic Scholar: {max_retries} 次重试后仍失败")

    async def health_check(self) -> bool:
        """检查 Semantic Scholar API 连通性。"""
        try:
            async with self._make_client() as client:
                resp = await client.get(
                    "/paper/search",
                    params={"query": "test", "limit": 1, "fields": "title"},
                )
                return resp.status_code == 200
        except Exception:
            return False
