"""L3 反幻觉防线 — 外部 DOI / arXiv 跨源验证.

职责：对 LLM 输出里出现的论文引用做**外部存在性校验**，识别"我自己库里没有
但 LLM 生成了看似合理的 DOI / arXiv ID / 标题作者年份组合"这类幻觉。

校验源（按优先级）：
  1. Crossref (https://api.crossref.org) — 主流 DOI 注册机构，免费 ~50 req/s
  2. arXiv API (http://export.arxiv.org/api/query) — 预印本，免费但需 1 req/3s 节流

策略：
  - 引用带 DOI → Crossref 直查
  - 引用带 arxiv_id → arXiv 直查
  - 否则用 (title, author, year) 模糊匹配 Crossref
  - 任一源命中 → exists=True；都不命中 → exists=False (可能编造)
  - API 不可达 → exists=None (优雅降级，不阻塞 publish 但加 unverified 标记)

缓存：SQLite `external_validations` 表，TTL 30 天，同一 DOI/arxiv 一个月内不重复查。

主入口：
    validator = ExternalValidator(db)
    result = await validator.validate(ref)
    # 或批量并发：
    results = await validator.validate_batch(refs, max_concurrent=5)

接入位置：
  - 当前 Phase A 骨架版本不接入主链路（generate_report 仍只走 L2 CitationVerifier）
  - Phase B 计划：CitationVerifier 内部对 action=delete 的引用追加 L3 校验
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import httpx

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class ExtractedReference:
    """从 LLM 输出里抽出来的待验证引用条目。"""
    raw_text: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None

    @property
    def cache_key(self) -> str:
        """缓存键：DOI > arxiv_id > (title+first_author+year) MD5。

        用完整 32-hex MD5 而非截断 16-hex，避免不同 (title, author, year) 三元组
        在 64-bit 空间里偶尔碰撞导致缓存别名（让一篇幻觉论文蹭到另一篇的 exists=True）。
        """
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower()}"
        fa = (self.authors[0] if self.authors else "").lower().strip()
        sig = f"{self.title.lower().strip()}|{fa}|{self.year or ''}"
        return f"md5:{hashlib.md5(sig.encode('utf-8')).hexdigest()}"


@dataclass
class ExternalValidation:
    """单条引用的外部验证结果。"""
    cache_key: str
    exists: Optional[bool]  # True=找到, False=三源都没找到（可能编造）, None=API 不可达
    source: Optional[Literal["crossref", "arxiv", "semantic_scholar"]] = None
    normalized_doi: Optional[str] = None
    normalized_arxiv_id: Optional[str] = None
    normalized_title: Optional[str] = None
    confidence: float = 0.0  # 0~1，外部源返回的匹配置信度
    reason: str = ""
    fetched_at: Optional[str] = None  # ISO 时间戳
    from_cache: bool = False


# ═══════════════════════════════════════════════════════════════
# DOI / arXiv ID 抽取（regex 兜底，给只有原始文本的引用补元数据）
# ═══════════════════════════════════════════════════════════════


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(
    # 新格式：YYMM.NNNNN (e.g. 2301.08727)
    # 旧格式：category/NNNNNNN (e.g. cs.CL/0102001 — 类别可含点号)
    r"\barXiv[:\s]*(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[a-z\-]+)?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)

# 标题相似度用的极简 stopword 表（够 Crossref 标题对比即可，不上 NLP）
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "with",
    "via", "using", "based", "by", "from", "is", "are", "be", "this", "that",
    "we", "our", "as",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _title_tokens(title: str) -> set[str]:
    """把标题切成去停用词的 token 集合，用于粗 Jaccard 相似度。"""
    if not title:
        return set()
    return {t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOPWORDS and len(t) > 1}


def extract_identifiers(raw_text: str) -> tuple[Optional[str], Optional[str]]:
    """从原始引用文本里抓 DOI / arxiv_id，找不到返回 (None, None)。"""
    doi_match = _DOI_RE.search(raw_text)
    arxiv_match = _ARXIV_RE.search(raw_text)

    # 清理 DOI 尾部可能附带的句尾标点：. , ; : ) 等
    doi = None
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;:)!?")
        # 如果 rstrip 后变空则丢弃（整段都是标点的极端情况）
        if not doi:
            doi = None

    return (
        doi,
        arxiv_match.group(1) if arxiv_match else None,
    )


# ═══════════════════════════════════════════════════════════════
# SQLite 缓存 schema
# ═══════════════════════════════════════════════════════════════


CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    source TEXT,
    exists_flag INTEGER,                 -- 0=NotFound, 1=Found, NULL=Unreachable
    normalized_doi TEXT,
    normalized_arxiv_id TEXT,
    normalized_title TEXT,
    confidence REAL DEFAULT 0.0,
    reason TEXT,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extval_key ON external_validations(cache_key);
CREATE INDEX IF NOT EXISTS idx_extval_exp ON external_validations(expires_at);
"""

CACHE_TTL_DAYS = 30


# ═══════════════════════════════════════════════════════════════
# ExternalValidator
# ═══════════════════════════════════════════════════════════════


class ExternalValidator:
    """跨源 DOI/arXiv 真伪校验器（L3）。

    Args:
        db: AgentDB 实例，用于 SQLite 缓存读写
        crossref_mailto: 加进 Crossref 的 User-Agent，礼貌使用
        timeout: 单次 HTTP 调用超时（秒）
    """

    CROSSREF_BASE = "https://api.crossref.org/works"
    ARXIV_BASE = "http://export.arxiv.org/api/query"

    def __init__(
        self,
        db=None,
        crossref_mailto: str = "paper-agent@local",
        timeout: float = 8.0,
    ):
        self._db = db
        self._mailto = crossref_mailto
        self._timeout = timeout
        self._ensure_cache_schema()

    def _ensure_cache_schema(self):
        """首次使用时建表（幂等）。

        关键：用独立 sqlite3 连接执行 DDL，避免 executescript() 在共享 conn 上
        触发隐式 COMMIT — 否则任何外层未提交的事务会被强制提交。
        """
        if self._db is None:
            return
        try:
            import sqlite3
            db_path = getattr(self._db, "db_path", None) or getattr(self._db, "path", None)
            if db_path:
                # 独立连接，绝不污染主连接的事务状态
                with sqlite3.connect(str(db_path)) as ddl_conn:
                    ddl_conn.executescript(CACHE_SCHEMA)
            else:
                # 拿不到路径时退而求其次 — 用 execute 逐条而非 executescript，
                # 避免隐式 COMMIT。
                cur = self._db.conn.cursor()
                for stmt in [s.strip() for s in CACHE_SCHEMA.split(";") if s.strip()]:
                    cur.execute(stmt)
                # 不在这里 commit；让上层事务边界决定
        except Exception as e:
            logger.warning(f"建 external_validations 表失败: {e}")

    # ── 缓存读写 ────────────────────────────────────────

    def _get_cached(self, cache_key: str) -> Optional[ExternalValidation]:
        if self._db is None:
            return None
        try:
            row = self._db.conn.execute(
                """SELECT * FROM external_validations
                   WHERE cache_key=? AND expires_at > ?""",
                (cache_key, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).fetchone()
            if not row:
                return None
            return ExternalValidation(
                cache_key=row["cache_key"],
                exists=bool(row["exists_flag"]) if row["exists_flag"] is not None else None,
                source=row["source"],
                normalized_doi=row["normalized_doi"],
                normalized_arxiv_id=row["normalized_arxiv_id"],
                normalized_title=row["normalized_title"],
                confidence=row["confidence"] or 0.0,
                reason=row["reason"] or "",
                fetched_at=row["fetched_at"],
                from_cache=True,
            )
        except Exception as e:
            logger.debug(f"读 external_validations 缓存失败: {e}")
            return None

    def _put_cached(self, v: ExternalValidation):
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(days=CACHE_TTL_DAYS)
            self._db.conn.execute(
                """INSERT OR REPLACE INTO external_validations
                   (cache_key, source, exists_flag, normalized_doi,
                    normalized_arxiv_id, normalized_title, confidence,
                    reason, fetched_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    v.cache_key,
                    v.source,
                    None if v.exists is None else int(v.exists),
                    v.normalized_doi,
                    v.normalized_arxiv_id,
                    v.normalized_title,
                    v.confidence,
                    v.reason,
                    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            self._db.conn.commit()
        except Exception as e:
            logger.debug(f"写 external_validations 缓存失败: {e}")

    # ── 外部源调用 ──────────────────────────────────────

    async def _query_crossref_doi(self, doi: str) -> Optional[dict]:
        """直接按 DOI 查 Crossref。"""
        url = f"{self.CROSSREF_BASE}/{doi}"
        headers = {"User-Agent": f"PaperAgent (mailto:{self._mailto})"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("message") if isinstance(data, dict) else None

    async def _query_crossref_bibliographic(
        self, title: str, author: str = "", year: Optional[int] = None
    ) -> Optional[dict]:
        """用 title+author+year 模糊查 Crossref，返回**高置信**候选或 None。

        反幻觉关键：Crossref 标题查询会对任意关键词返回结果（150M+ 记录），
        必须验证返回项与请求的相似度，否则会把幻觉论文映射到一篇无关真论文。

        通过门槛：
          - 命中项标题与请求标题 token 重合率 ≥ 0.6（粗 Jaccard）
          - 若指定了 year，命中项年份必须在 ±1 范围内
        否则返回 None（让上层标 exists=False，触发幻觉警告）。
        """
        params = {"query.bibliographic": title, "rows": 5}
        if author:
            params["query.author"] = author
        headers = {"User-Agent": f"PaperAgent (mailto:{self._mailto})"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(self.CROSSREF_BASE, params=params, headers=headers)
        r.raise_for_status()
        items = (r.json().get("message") or {}).get("items") or []
        if not items:
            return None

        request_tokens = _title_tokens(title)
        if not request_tokens:
            return None

        best_item = None
        best_score = 0.0
        for item in items:
            cand_title = (item.get("title") or [""])[0]
            if not cand_title:
                continue
            cand_tokens = _title_tokens(cand_title)
            if not cand_tokens:
                continue
            overlap = len(request_tokens & cand_tokens)
            score = overlap / max(len(request_tokens | cand_tokens), 1)

            # 年份门：要求请求年与命中年差 ≤ 1（无年信息则不卡）
            if year:
                issued = item.get("issued", {}).get("date-parts", [[None]])
                iy = (issued[0][0] if issued and issued[0] else None)
                if iy is None or abs(int(iy) - int(year)) > 1:
                    continue

            if score > best_score:
                best_score = score
                best_item = item

        # 没有候选通过相似度门槛 → 视为未找到（绝不返回 items[0] 兜底，
        # 否则 Crossref 几乎对任何输入都"找得到"，反幻觉防线形同虚设）
        if best_score < 0.6 or best_item is None:
            return None
        return best_item

    class _ArxivUnreachable(Exception):
        """arXiv API 不可达（非 200/解析失败）— 让上层把 exists 标为 None 而非 False。"""

    async def _query_arxiv(self, arxiv_id: str) -> Optional[str]:
        """按 arXiv ID 查 arXiv，返回标题（命中则非空，未找到返回 None）。

        关键：non-200 / 网络错误 → 抛 _ArxivUnreachable，由上层 except 兜进
        exists=None 路径，避免把 429/5xx 当 "not found" 缓存 30 天。
        """
        params = {"id_list": arxiv_id, "max_results": 1}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(self.ARXIV_BASE, params=params)
        if r.status_code != 200:
            # 区分"明确未找到"和"不可达"：arXiv 对未知 id 也返回 200 + 空 feed，
            # 所以这里 non-200 一律视为不可达
            raise self._ArxivUnreachable(
                f"arXiv API {r.status_code} for id={arxiv_id}"
            )
        text = r.text
        # 简化提取：找第二个 <title>（第一个是 feed 自身的标题）
        titles = re.findall(r"<title>([^<]+)</title>", text)
        if len(titles) >= 2:
            t = titles[1].strip()
            # arXiv 对未知 id 也会返回 "Error" 类标题，简单过滤
            if t and not t.lower().startswith("error"):
                return t
        return None

    # ── 单条验证 ────────────────────────────────────────

    async def validate(self, ref: ExtractedReference) -> ExternalValidation:
        """单条引用的跨源验证。

        命中策略：DOI 优先 → arXiv → 标题模糊匹配 Crossref。
        任一命中即返回 exists=True 并写缓存。
        """
        key = ref.cache_key

        # 1. 查缓存
        cached = self._get_cached(key)
        if cached is not None:
            logger.debug(f"L3 cache hit: {key}")
            return cached

        # 2. 按优先级查外部源
        try:
            # 2.1 DOI 直查
            if ref.doi:
                msg = await self._query_crossref_doi(ref.doi)
                if msg:
                    v = ExternalValidation(
                        cache_key=key,
                        exists=True,
                        source="crossref",
                        normalized_doi=msg.get("DOI"),
                        normalized_title=(msg.get("title") or [""])[0],
                        confidence=1.0,
                        reason="Crossref DOI 直查命中",
                    )
                    self._put_cached(v)
                    return v

            # 2.2 arXiv 直查（错误抛 _ArxivUnreachable，进入下面 except 兜底）
            if ref.arxiv_id:
                try:
                    title = await self._query_arxiv(ref.arxiv_id)
                except self._ArxivUnreachable as e:
                    # 关键：arXiv 不可达 → 不写 exists=False，直接返回 None（不缓存）
                    # 避免一次 429 把真论文冻 30 天
                    logger.warning(f"L3 arXiv 不可达，跳过该篇并标 unverified: {e}")
                    return ExternalValidation(
                        cache_key=key,
                        exists=None,
                        reason=f"arXiv API 不可达: {e}",
                    )
                if title:
                    v = ExternalValidation(
                        cache_key=key,
                        exists=True,
                        source="arxiv",
                        normalized_arxiv_id=ref.arxiv_id,
                        normalized_title=title,
                        confidence=1.0,
                        reason="arXiv ID 直查命中",
                    )
                    self._put_cached(v)
                    return v

            # 2.3 模糊匹配 Crossref（标题+作者+年份）
            if ref.title:
                first_author = ref.authors[0] if ref.authors else ""
                msg = await self._query_crossref_bibliographic(
                    ref.title, first_author, ref.year,
                )
                if msg:
                    v = ExternalValidation(
                        cache_key=key,
                        exists=True,
                        source="crossref",
                        normalized_doi=msg.get("DOI"),
                        normalized_title=(msg.get("title") or [""])[0],
                        confidence=0.7,  # 模糊匹配，置信度降低
                        reason="Crossref 标题+作者+年份模糊匹配",
                    )
                    self._put_cached(v)
                    return v

            # 三源都没命中
            v = ExternalValidation(
                cache_key=key,
                exists=False,
                reason="Crossref + arXiv 三源都未找到，可能为编造",
            )
            self._put_cached(v)
            return v

        except Exception as e:
            # API 不可达：exists=None（与 fail-closed 区分 — 这里不能拒发表，
            # 因为外部 API 不稳定不能阻塞主流程，但要明确标 unverified）
            logger.warning(f"L3 外部 API 调用失败: {e}, 标 unverified")
            return ExternalValidation(
                cache_key=key,
                exists=None,
                reason=f"外部 API 不可达 ({type(e).__name__}): {e}",
            )

    # ── 批量并发 ────────────────────────────────────────

    async def validate_batch(
        self,
        refs: list[ExtractedReference],
        max_concurrent: int = 5,
    ) -> list[ExternalValidation]:
        """并发批量验证，限流 max_concurrent 同时在飞。"""
        sem = asyncio.Semaphore(max_concurrent)

        async def guarded(r: ExtractedReference) -> ExternalValidation:
            async with sem:
                return await self.validate(r)

        return await asyncio.gather(*[guarded(r) for r in refs])


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════


def ref_from_citation_match(cm) -> ExtractedReference:
    """从 CitationVerifier 的 CitationMatch 构造 ExtractedReference。

    便于 Phase B 时把 L2 的 action=delete 项追加灌进 L3 复验。
    """
    doi, arxiv = extract_identifiers(cm.raw_text)
    return ExtractedReference(
        raw_text=cm.raw_text,
        title=cm.matched_title or "",
        doi=doi,
        arxiv_id=arxiv,
    )


__all__ = [
    "ExtractedReference",
    "ExternalValidation",
    "ExternalValidator",
    "extract_identifiers",
    "ref_from_citation_match",
    "CACHE_SCHEMA",
    "CACHE_TTL_DAYS",
]
