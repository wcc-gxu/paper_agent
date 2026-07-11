#!/usr/bin/env python3
"""Evaluation pipeline — load test sets, run retrieval, compute metrics, output report.

Usage:
    python scripts/run_evaluation.py --test-set test_sets/retrieval/ --output reports/
    python scripts/run_evaluation.py --test-set test_sets/anti_hallucination/ --mode anti_hallucination
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval")


def load_test_sets(path: Path) -> list[dict]:
    """Load all JSONL test set files from a directory."""
    items = []
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("*.jsonl"))
    for f in files:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    logger.info(f"Loaded {len(items)} queries from {len(files)} file(s)")
    return items


async def run_retrieval_eval(test_set: list[dict], output_dir: Path):
    """Run retrieval quality evaluation."""
    from paper_search.agent.evaluator import RecallEvaluator
    from paper_search.agent.chroma_store import ChromaStoreV2

    chroma = ChromaStoreV2()

    async def search_fn(query: str, top_k: int = 20) -> list[dict]:
        try:
            results = chroma.search_similar(query, n_results=top_k)
            return results
        except Exception as e:
            logger.warning(f"Search failed for '{query[:60]}': {e}")
            return []

    evaluator = RecallEvaluator()
    metrics = await evaluator.evaluate(test_set, search_fn)
    report = evaluator.report()

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"eval_report_{time.strftime('%Y%m%d_%H%M%S')}.md"
    report_path.write_text(report)

    json_path = output_dir / f"eval_metrics_{time.strftime('%Y%m%d_%H%M%S')}.json"
    json_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))

    logger.info(f"Report: {report_path}")
    logger.info(f"Metrics JSON: {json_path}")
    logger.info(f"Recall@10: {metrics.get('recall_at_10', 0):.3f}, MRR: {metrics.get('mrr', 0):.3f}")

    return metrics


async def run_anti_hallucination_eval(test_set: list[dict], output_dir: Path):
    """Run anti-hallucination red team evaluation."""
    from paper_search.agent.verifier import CitationVerifier

    verifier = CitationVerifier(db=None, llm_client=None)
    results = []

    for item in test_set:
        text = item.get("text", "")
        expected_fake = item.get("expected_fake_citations", [])
        expected_real = item.get("expected_real_citations", [])

        # Parse citations from text
        citations = verifier._parser.extract(text)
        found_fake = sum(1 for c in citations if not c.get("matched", False))
        found_real = sum(1 for c in citations if c.get("matched", False))

        results.append({
            "test_id": item.get("id", ""),
            "total_citations": len(citations),
            "fake_detected": found_fake,
            "real_verified": found_real,
            "expected_fake": len(expected_fake),
            "expected_real": len(expected_real),
        })

    summary = {
        "total_tests": len(results),
        "total_citations": sum(r["total_citations"] for r in results),
        "fake_detection_rate": (
            sum(r["fake_detected"] for r in results) /
            max(sum(r["expected_fake"] for r in results), 1)
        ),
        "real_verification_rate": (
            sum(r["real_verified"] for r in results) /
            max(sum(r["expected_real"] for r in results), 1)
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"anti_hallucination_{time.strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps({"summary": summary, "details": results}, indent=2, ensure_ascii=False))
    logger.info(f"Report: {report_path}")
    logger.info(f"Fake detection rate: {summary['fake_detection_rate']:.2%}")
    logger.info(f"Real verification rate: {summary['real_verification_rate']:.2%}")

    return summary


def seed_test_sets(output_dir: Path):
    """Generate seed test sets with example queries."""
    retrieval_dir = output_dir / "retrieval"
    retrieval_dir.mkdir(parents=True, exist_ok=True)

    retrieval_samples = [
        {"query": "transformer attention mechanism for natural language processing", "domain": "nlp",
         "relevant_paper_ids": ["arxiv:1706.03762"]},
        {"query": "graph neural networks for molecular property prediction", "domain": "chemistry",
         "relevant_paper_ids": []},
        {"query": "diffusion models for image generation", "domain": "cv",
         "relevant_paper_ids": []},
        {"query": "reinforcement learning from human feedback", "domain": "rl",
         "relevant_paper_ids": []},
        {"query": "联邦学习隐私保护", "domain": "ml_security",
         "relevant_paper_ids": []},
    ]

    with open(retrieval_dir / "seed_queries.jsonl", "w") as f:
        for item in retrieval_samples:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logger.info(f"Seeded {len(retrieval_samples)} retrieval queries to {retrieval_dir}")

    anti_h_dir = output_dir / "anti_hallucination"
    anti_h_dir.mkdir(parents=True, exist_ok=True)

    anti_hallucination_samples = [
        {"id": "fake_doi_1", "text": "As shown by Smith et al. [ext:10.9999/fake-doi-12345], the results are significant.",
         "expected_fake_citations": ["10.9999/fake-doi-12345"], "expected_real_citations": []},
        {"id": "real_doi_1", "text": "The transformer architecture [ext:10.48550/arXiv.1706.03762] revolutionized NLP.",
         "expected_fake_citations": [], "expected_real_citations": ["10.48550/arXiv.1706.03762"]},
        {"id": "mixed_1", "text": "Attention mechanisms [1] improved performance by 30% [2]. As noted in [3], scaling laws apply.",
         "expected_fake_citations": [], "expected_real_citations": []},
    ]

    with open(anti_h_dir / "seed_tests.jsonl", "w") as f:
        for item in anti_hallucination_samples:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logger.info(f"Seeded {len(anti_hallucination_samples)} anti-hallucination tests to {anti_h_dir}")


def main():
    p = argparse.ArgumentParser(description="Evaluation pipeline")
    p.add_argument("--test-set", default="test_sets/", help="Test set directory or file")
    p.add_argument("--output", default="reports/", help="Output directory for reports")
    p.add_argument("--mode", default="retrieval", choices=["retrieval", "anti_hallucination"])
    p.add_argument("--seed", action="store_true", help="Generate seed test sets")
    args = p.parse_args()

    test_set_path = Path(args.test_set)
    output_dir = Path(args.output)

    if args.seed:
        seed_test_sets(test_set_path)
        return

    test_set = load_test_sets(test_set_path)
    if not test_set:
        logger.warning("No test queries found. Run with --seed to generate examples.")
        sys.exit(1)

    import asyncio
    if args.mode == "retrieval":
        asyncio.run(run_retrieval_eval(test_set, output_dir))
    else:
        asyncio.run(run_anti_hallucination_eval(test_set, output_dir))


if __name__ == "__main__":
    main()
