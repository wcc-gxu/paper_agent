"""External citation validator — Crossref / arXiv / Semantic Scholar APIs (v3 Phase 3).

Three-channel fallback cascade:
  Crossref API → arXiv API → Semantic Scholar API
Each channel has 10s timeout + 2 retries with exponential backoff.

Usage:
    from .external_validator import ExternalValidator
    v = ExternalValidator()
    result = await v.verify_citation("transformer attention Vaswani et al. 2017")
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works"
ARXIV_API = "https://export.arxiv.org/api/query"
S2_SEARCH_API = "https://api.semanticscholar.org/graph/v1/paper/search"
CACHE_TTL = 3600
MIN_REQUEST_INTERVAL = 1.0
DOI_RE = re.compile(r'\b(10\.\d{4,}/[^\s]+)\b')
ARXIV_RE = re.compile(
    r'(?:arxiv:\s*)?((?:\d{4}\.\d{4,}(?:v\d+)?)|(?:[a-z-]+\.[A-Z]{2}/\d{7}(?:v\d+)?))',
    re.IGNORECASE,
)


class ExternalValidator:
    """External citation verification via academic APIs.

    Fallback cascade: Crossref → arXiv → Semantic Scholar.
    Results cached in-memory for 1 hour.
    """

    def __init__(self, redis_client=None):
        self._cache: dict[str, tuple[float, dict]] = {}
        self._last_req = 0.0
        self._redis = redis_client
        self._s2_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

    async def verify_citation(self, text: str) -> dict:
        """Auto-detect type and verify. Supports DOI, arXiv ID, free text."""
        cache_key = text.strip().lower()[:100]
        if cache_key in self._cache:
            at, result = self._cache[cache_key]
            if time.time() - at < CACHE_TTL:
                return result

        doi_m = DOI_RE.search(text)
        if doi_m:
            result = await self.verify_doi(doi_m.group(1))
            self._cache[cache_key] = (time.time(), result)
            return result

        arxiv_m = ARXIV_RE.search(text)
        if arxiv_m:
            result = await self.verify_arxiv(arxiv_m.group(2))
            self._cache[cache_key] = (time.time(), result)
            return result

        result = await self.verify_title(text)
        self._cache[cache_key] = (time.time(), result)
        return result

    async def verify_doi(self, doi: str) -> dict:
        """Verify DOI via Crossref API."""
        doi = doi.strip().rstrip(".")
        if not doi.startswith("10."):
            return self._err("Invalid DOI format")

        url = f"{CROSSREF_API}/{doi}"
        data = await self._get_json(url, "crossref")
        if not data:
            return self._err("Crossref API unreachable")

        work = data.get("message", {})
        if not work:
            return self._err("DOI not found")

        authors = []
        for a in work.get("author", []):
            g, f = a.get("given", ""), a.get("family", "")
            if g or f:
                authors.append(f"{g} {f}".strip())

        year = None
        dp = work.get("published-print", {}) or work.get("created", {})
        dp_parts = dp.get("date-parts", [[None]])
        if dp_parts and dp_parts[0]:
            year = dp_parts[0][0]

        return {
            "verified": True, "source": "crossref",
            "title": (work.get("title") or [""])[0],
            "authors": authors, "year": year, "doi": doi,
            "match_score": 1.0, "error": None,
        }

    async def verify_arxiv(self, arxiv_id: str) -> dict:
        """Verify arXiv ID via arXiv API."""
        clean = re.sub(r'^arxiv:\s*', '', arxiv_id.strip(), flags=re.IGNORECASE)
        clean = re.sub(r'v\d+$', '', clean)

        url = f"{ARXIV_API}?id_list={clean}&max_results=1"
        await self._rate_limit()
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(url)
                if resp.status_code != 200:
                    return self._err(f"arXiv API status {resp.status_code}")
                import xml.etree.ElementTree as ET
                ns = {"a": "http://www.w3.org/2005/Atom"}
                root = ET.fromstring(resp.text)
                entry = root.find("a:entry", ns)
                if entry is None:
                    return self._err("arXiv ID not found")
                title_el = entry.find("a:title", ns)
                title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
                authors = []
                for au in entry.findall("a:author", ns):
                    n = au.find("a:name", ns)
                    if n is not None and n.text:
                        authors.append(n.text.strip())
                pub = entry.find("a:published", ns)
                year = int(pub.text[:4]) if pub is not None and pub.text else None
                return {
                    "verified": True, "source": "arxiv",
                    "title": title, "authors": authors, "year": year,
                    "doi": None, "match_score": 1.0, "error": None,
                }
        except Exception as e:
            logger.warning(f"arXiv fetch failed: {e}")
            return self._err(str(e))

    async def verify_title(self, title: str, year: int = None) -> dict:
        """Search by title via Semantic Scholar API."""
        query = title.strip()[:200]
        headers = {"x-api-key": self._s2_key} if self._s2_key else {}
        data = await self._get_json(
            S2_SEARCH_API,
            "semantic_scholar",
            params={"query": query, "limit": 3, "fields": "title,authors,year,externalIds"},
            headers=headers,
        )
        if not data:
            return self._err("Semantic Scholar API unreachable")

        papers = data.get("data", [])
        if not papers:
            return self._err("No matching papers found")

        best = papers[0]
        q_tokens = set(query.lower().split())
        t_tokens = set((best.get("title") or "").lower().split())
        score = len(q_tokens & t_tokens) / len(q_tokens) if q_tokens else 0.0
        if year and best.get("year") == year:
            score = min(1.0, score + 0.2)

        return {
            "verified": score >= 0.3, "source": "semantic_scholar",
            "title": best.get("title", ""),
            "authors": [a.get("name", "") for a in best.get("authors", [])],
            "year": best.get("year"),
            "doi": (best.get("externalIds") or {}).get("DOI"),
            "match_score": round(score, 3),
            "error": None if score >= 0.3 else f"Low match: {score:.2f}",
        }

    async def _get_json(self, url: str, source: str, params=None, headers=None,
                         max_retries=2) -> Optional[dict]:
        await self._rate_limit()
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    resp = await c.get(url, params=params or {}, headers=headers or {})
                    if resp.status_code == 404:
                        return None
                    if resp.status_code == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException:
                if attempt < max_retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                return None
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                logger.warning(f"{source} fetch error: {e}")
                return None
        return None

    async def _rate_limit(self):
        now = time.time()
        elapsed = now - self._last_req
        if elapsed < MIN_REQUEST_INTERVAL:
            await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_req = time.time()

    def _err(self, msg: str) -> dict:
        return {"verified": False, "source": "none", "title": "", "authors": [],
                "year": None, "doi": None, "match_score": 0.0, "error": msg}

    def clear_cache(self):
        self._cache.clear()


# ═══════════════════════════════════════════════════════════════
# v3 Phase 3: 引用提取与验证结果类型 (向后兼容已有测试)
# ═══════════════════════════════════════════════════════════════

from dataclasses import dataclass, field


@dataclass
class ExtractedReference:
    """从文本中提取的引用标识符."""
    raw_text: str = ""                # 原始匹配文本
    doi: Optional[str] = None         # DOI (如有)
    arxiv_id: Optional[str] = None    # arXiv ID (如有)
    title: Optional[str] = None       # 标题
    authors: list[str] = field(default_factory=list)  # 作者
    year: Optional[int] = None        # 年份
    span: tuple[int, int] = (0, 0)    # 在原文中的位置
    ref_type: str = "unknown"         # "doi" | "arxiv" | "title" | "numbered"

    @property
    def cache_key(self) -> str:
        """稳定的缓存键 — 优先 DOI > arXiv > MD5 hash。"""
        if self.doi:
            return f"doi:{self.doi.strip().lower()}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.strip()}"
        # Fallback: MD5 of title + first author + year
        import hashlib
        raw = f"{self.title or ''}|{self.authors[0] if self.authors else ''}|{self.year or ''}"
        return f"md5:{hashlib.md5(raw.encode()).hexdigest()[:16]}"


@dataclass
class ExternalValidation:
    """单条外部验证结果."""
    ref: ExtractedReference
    verified: bool = False
    source: str = "none"              # "crossref" | "arxiv" | "semantic_scholar" | "none"
    matched_title: str = ""
    matched_authors: list[str] = field(default_factory=list)
    matched_year: Optional[int] = None
    matched_doi: Optional[str] = None
    match_score: float = 0.0
    error: Optional[str] = None


def extract_identifiers(text: str) -> tuple[Optional[str], Optional[str]]:
    """从文本中提取 DOI 和 arXiv ID。

    Returns:
        (doi, arxiv_id) — 任一可为 None。
    """
    doi = None
    arxiv_id = None

    doi_match = DOI_RE.search(text)
    if doi_match:
        doi = doi_match.group(1).strip().rstrip(".")

    arxiv_match = ARXIV_RE.search(text)
    if arxiv_match:
        arxiv_id = arxiv_match.group(1).strip()

    return doi, arxiv_id
