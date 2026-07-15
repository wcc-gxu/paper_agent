"""Reranker — Cross-Encoder 重排序 (bge-reranker-v2-m3 via SiliconFlow).

替代原有的 LLM-based reranker，提供确定性的相关性分数。
失败时 hard error（不降级到 LLM fallback）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from paper_search.config import RERANK_API_KEY, RERANK_BASE_URL, RERANK_MODEL

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """Reranker 调用失败（重试耗尽或不可重试的错误）."""


@dataclass
class RerankResult:
    """单条重排序结果."""

    index: int
    score: float
    text: str


class RerankerClient:
    """Cross-Encoder 重排序客户端.

    调用 SiliconFlow BGE-reranker-v2-m3 API（Cohere 兼容格式）。
    重试 3 次 + 指数退避，失败抛 RerankError（hard error，不降级）。

    使用方式::

        client = RerankerClient()
        results = client.rerank("什么是向量数据库", [
            "向量数据库是存储向量的...",
            "今天天气很好...",
        ], top_k=3)
        for r in results:
            print(r.score, r.text)
    """

    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 1.0
    REQUEST_TIMEOUT: float = 60.0

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
    ):
        self.api_key = api_key or RERANK_API_KEY
        self.base_url = (base_url or RERANK_BASE_URL).rstrip("/")
        self.model = model or RERANK_MODEL

    # ── Public API ──────────────────────────────────────────

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: Optional[int] = None,
    ) -> list[RerankResult]:
        """对文档列表执行 Cross-Encoder 重排序.

        Args:
            query: 查询文本.
            documents: 候选文档列表（建议每条截断到 ~2000 chars / 512 tokens）.
            top_k: 返回 top-K 结果，默认全部返回.

        Returns:
            按相关性降序排列的 RerankResult 列表.

        Raises:
            RerankError: 所有重试耗尽或遇到不可重试的错误.
        """
        if not documents:
            return []

        effective_top_k = top_k if top_k is not None else len(documents)
        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return self._call_once(query, documents, effective_top_k)
            except RerankError:
                # 不可重试的错误（如 400/401/403），直接抛出
                raise
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Rerank network error (attempt {attempt + 1}/{self.MAX_RETRIES}): "
                        f"{e}, retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    raise RerankError(
                        f"Rerank failed after {self.MAX_RETRIES + 1} attempts "
                        f"(network error): {e}"
                    ) from e
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status in (429, 502, 503, 504):
                    last_error = e
                    if attempt < self.MAX_RETRIES:
                        retry_after = (
                            e.response.headers.get("Retry-After", "")
                            if e.response is not None
                            else ""
                        )
                        delay = (
                            float(retry_after)
                            if retry_after.replace(".", "").isdigit()
                            else self.RETRY_BASE_DELAY * (2 ** attempt)
                        )
                        logger.warning(
                            f"Rerank HTTP {status} "
                            f"(attempt {attempt + 1}/{self.MAX_RETRIES}), "
                            f"waiting {delay:.1f}s"
                        )
                        time.sleep(delay)
                    else:
                        raise RerankError(
                            f"Rerank HTTP {status} after "
                            f"{self.MAX_RETRIES + 1} attempts"
                        ) from e
                else:
                    raise RerankError(
                        f"Rerank non-retryable HTTP {status}: {e}"
                    ) from e
            except Exception as e:
                raise RerankError(f"Rerank unexpected error: {e}") from e

        raise RerankError(
            f"Rerank failed after {self.MAX_RETRIES + 1} attempts"
        )

    # ── Internal ────────────────────────────────────────────

    def _call_once(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[RerankResult]:
        """单次 rerank API 调用（无重试）.

        Raises:
            RerankError: API key 未配置或返回不可重试错误.
            requests.HTTPError: HTTP 错误（由调用方决定是否重试）.
        """
        if not self.api_key:
            raise RerankError("RERANK_API_KEY 未配置")

        resp = requests.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": top_k,
                "max_chunks_per_doc": 1,
            },
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        # 按 score 降序排列（API 可能不保证顺序）
        results.sort(key=lambda r: r.get("relevance_score", 0.0), reverse=True)

        return [
            RerankResult(
                index=r.get("index", i),
                score=r.get("relevance_score", 0.0),
                text=(
                    documents[r.get("index", i)]
                    if r.get("index", i) < len(documents)
                    else ""
                ),
            )
            for i, r in enumerate(results)
        ]


# ── Module-level singleton ─────────────────────────────────

_reranker: Optional[RerankerClient] = None


def get_reranker() -> RerankerClient:
    """获取共享的 RerankerClient 单例（延迟初始化）."""
    global _reranker
    if _reranker is None:
        _reranker = RerankerClient()
    return _reranker
