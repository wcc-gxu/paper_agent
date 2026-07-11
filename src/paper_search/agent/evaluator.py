"""Retrieval quality evaluator — Recall@K, MRR, NDCG@K (v3 Phase 4).

Measures search quality against annotated test sets.

Usage:
    from .evaluator import RecallEvaluator
    ev = RecallEvaluator()
    results = await ev.evaluate(test_set, search_fn)
    print(f"Recall@10: {results['recall_at_10']:.3f}")
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    """Single-query evaluation result."""
    query: str
    domain: str
    relevant_ids: list[str]
    retrieved_ids: list[str]
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    recall_at_20: float = 0.0
    mrr: float = 0.0
    ndcg_at_10: float = 0.0


class RecallEvaluator:
    """Retrieval quality evaluator.

    Metrics:
      - Recall@5/10/20: fraction of relevant papers found in top-K
      - MRR (Mean Reciprocal Rank): average of 1/rank for first relevant
      - NDCG@10: Normalized Discounted Cumulative Gain

    Test set format (JSONL):
      {"query": "...", "domain": "...", "relevant_paper_ids": ["id1", "id2", ...]}
    """

    def __init__(self):
        self.results: list[EvalResult] = []

    async def evaluate(
        self,
        test_set: list[dict],
        search_fn: Callable,
        k_values: tuple[int, ...] = (5, 10, 20),
    ) -> dict:
        """Run evaluation on a test set.

        Args:
            test_set: list of {"query", "domain", "relevant_paper_ids"}
            search_fn: async fn(query, top_k) → list[dict] with "paper_id" keys
            k_values: K values for Recall@K

        Returns:
            dict with aggregate metrics
        """
        self.results = []
        for item in test_set:
            query = item["query"]
            relevant = set(item["relevant_paper_ids"])
            retrieved = await search_fn(query, max(k_values))
            retrieved_ids = [r.get("paper_id", "") for r in retrieved]

            r = EvalResult(
                query=query,
                domain=item.get("domain", ""),
                relevant_ids=list(relevant),
                retrieved_ids=retrieved_ids,
            )

            # Recall@K
            for k in k_values:
                top_k_ids = set(retrieved_ids[:k])
                hits = len(relevant & top_k_ids)
                recall = hits / len(relevant) if relevant else 0.0
                setattr(r, f"recall_at_{k}", recall)

            # MRR
            for rank, pid in enumerate(retrieved_ids, 1):
                if pid in relevant:
                    r.mrr = 1.0 / rank
                    break

            # NDCG@10
            r.ndcg_at_10 = self._ndcg(relevant, retrieved_ids[:10])

            self.results.append(r)

        return self._aggregate(k_values)

    def _aggregate(self, k_values: tuple[int, ...]) -> dict:
        if not self.results:
            return {}

        n = len(self.results)
        agg = {
            "num_queries": n,
            "domains": {},
        }

        for k in k_values:
            values = [getattr(r, f"recall_at_{k}") for r in self.results]
            agg[f"recall_at_{k}"] = sum(values) / n

        agg["mrr"] = sum(r.mrr for r in self.results) / n
        agg["ndcg_at_10"] = sum(r.ndcg_at_10 for r in self.results) / n

        # Per domain
        for r in self.results:
            d = r.domain or "general"
            agg["domains"].setdefault(d, {"count": 0, "recall_at_10": 0.0})
            agg["domains"][d]["count"] += 1
            agg["domains"][d]["recall_at_10"] += r.recall_at_10

        for d in agg["domains"]:
            c = agg["domains"][d]["count"]
            agg["domains"][d]["recall_at_10"] /= c if c else 1

        return agg

    @staticmethod
    def _ndcg(relevant_ids: set[str], retrieved_ids: list[str], k: int = 10) -> float:
        """Compute NDCG@K."""
        dcg = 0.0
        for i, pid in enumerate(retrieved_ids[:k]):
            if pid in relevant_ids:
                dcg += 1.0 / math.log2(i + 2)  # i+2 because i is 0-indexed

        # Ideal DCG: all relevant at top
        ideal_k = min(len(relevant_ids), k)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_k))

        return dcg / idcg if idcg > 0 else 0.0

    def report(self) -> str:
        """Generate a Markdown evaluation report."""
        if not self.results:
            return "No results."
        agg = self._aggregate((5, 10, 20))

        lines = [
            "# Retrieval Evaluation Report",
            "",
            f"**Queries**: {agg['num_queries']}",
            "",
            "## Aggregate Metrics",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Recall@5 | {agg.get('recall_at_5', 0):.3f} |",
            f"| Recall@10 | {agg.get('recall_at_10', 0):.3f} |",
            f"| Recall@20 | {agg.get('recall_at_20', 0):.3f} |",
            f"| MRR | {agg.get('mrr', 0):.3f} |",
            f"| NDCG@10 | {agg.get('ndcg_at_10', 0):.3f} |",
            "",
            "## Per Domain",
            "",
        ]
        for domain, stats in sorted(agg.get("domains", {}).items()):
            lines.append(f"- **{domain}**: {stats['count']} queries, Recall@10={stats['recall_at_10']:.3f}")

        lines.extend([
            "",
            "## Per-Query Details",
            "",
        ])
        for r in self.results:
            lines.append(f"- `{r.query[:80]}` — R@10={r.recall_at_10:.3f}, MRR={r.mrr:.3f}")

        return "\n".join(lines)

    @classmethod
    def load_test_set(cls, path: Path) -> list[dict]:
        """Load test set from JSONL file."""
        items = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        return items

    @classmethod
    def save_test_set(cls, items: list[dict], path: Path):
        """Save test set to JSONL file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
