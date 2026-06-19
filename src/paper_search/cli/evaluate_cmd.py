"""paper-evaluate CLI — LLM 批量评估论文相关性.

独立可执行命令:
    paper-evaluate --project-id <id> --query "original search intent"
    paper-evaluate --project-id <id> --paper-ids "id1,id2,id3"
    paper-evaluate --project-id <id> --all  # 评估所有未评估的论文

stdout: JSON (机器可读)
stderr: Rich 进度 + 评分表格 (人类可读)
"""

import argparse
import asyncio
import sys
import time

from .common import (
    add_project_id_arg,
    console_print,
    create_db,
    format_duration,
    output_error,
    output_json,
    progress_spinner,
    run_async,
    show_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LLM 批量评估论文相关性 — 使用火山引擎 LLM 对论文打分 (0-1)",
    )
    add_project_id_arg(parser)
    parser.add_argument("--query", type=str, default="",
                        help="原始搜索意图（用于判断论文相关性）")
    parser.add_argument("--paper-ids", type=str, default=None,
                        help="要评估的 paper_id 列表，逗号分隔")
    parser.add_argument("--all", action="store_true",
                        help="评估项目下所有未评估的论文")
    parser.add_argument("--max-concurrent", type=int, default=5,
                        help="最大并发 LLM 调用数 (默认: 5)")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    return parser


async def _evaluate_one(llm_client, paper: dict, user_query: str,
                        index: int, total: int) -> dict:
    """评估单篇论文的相关性。"""
    title = paper.get("title", "")
    abstract = paper.get("abstract", "") or ""
    paper_id = paper["id"]

    try:
        result = await llm_client.evaluate_relevance(
            title=title,
            abstract=abstract,
            user_query=user_query,
        )
        return {
            "paper_id": paper_id,
            "title": title[:80],
            "score": result.score,
            "reason": result.reason,
            "is_relevant": result.is_relevant,
            "success": True,
        }
    except Exception as e:
        return {
            "paper_id": paper_id,
            "title": title[:80],
            "score": 0.5,
            "reason": f"评估失败: {e}",
            "is_relevant": True,  # 保守：失败时保留
            "success": False,
            "error": str(e),
        }


async def _main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.project_id:
        output_error("请指定 --project-id")
        return 1

    from ..agent.llm_client import LLMClient

    db = create_db()
    project = db.get_project(args.project_id)
    if project is None:
        output_error(f"项目不存在: {args.project_id}")
        db.close()
        return 1

    user_query = args.query or project.get("user_query", "")
    console_print(f"[bold cyan]🤖 paper-evaluate[/bold cyan]")
    console_print(f"   项目: {args.project_id} | 查询: {user_query[:60]}")

    # 获取待评估论文
    if args.paper_ids:
        paper_ids = [pid.strip() for pid in args.paper_ids.split(",")]
        papers = []
        for pid in paper_ids:
            row = db.conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
            if row:
                papers.append(dict(row))
    else:
        papers = db.get_project_papers(args.project_id)
        if args.all:
            # 筛选未评估的（score == 0.5 默认值的）
            papers = [p for p in papers
                      if p.get("relevance_score", 0.5) == 0.5
                      and p.get("relevance_reason", "") == ""]

    if not papers:
        console_print("[yellow]⚠ 没有需要评估的论文[/yellow]")
        output_json({"success": True, "evaluated": 0, "results": []})
        db.close()
        return 0

    console_print(f"   论文数: {len(papers)} | 并发: {args.max_concurrent}")

    # 批量评估（带并发控制）
    llm = LLMClient()
    sem = asyncio.Semaphore(args.max_concurrent)
    t0 = time.time()

    async def bounded_evaluate(paper, idx, total):
        async with sem:
            return await _evaluate_one(llm, paper, user_query, idx, total)

    tasks = [bounded_evaluate(p, i, len(papers)) for i, p in enumerate(papers)]
    results = await asyncio.gather(*tasks)

    # 更新 DB
    for r in results:
        try:
            db.link_paper_to_project(
                args.project_id, r["paper_id"],
                relevance_score=r["score"],
                relevance_reason=r.get("reason", "")[:500],
            )
        except Exception:
            pass

    db.close()
    elapsed = time.time() - t0

    # 统计
    relevant = [r for r in results if r.get("is_relevant")]
    console_print(f"\n[green]✅ 评估完成: {len(relevant)}/{len(results)} 相关 "
                  f"({format_duration(elapsed)})[/green]")

    # 评分表格
    rows = []
    for r in sorted(results, key=lambda x: x["score"], reverse=True)[:20]:
        icon = "🟢" if r["score"] >= 0.7 else ("🟡" if r["score"] >= 0.4 else "🔴")
        rows.append([icon, f"{r['score']:.2f}", r["title"][:60],
                      (r.get("reason") or "")[:40]])

    show_table("相关性评估结果 (前20)", ["", "得分", "标题", "理由"], rows)

    output_json({
        "success": True,
        "project_id": args.project_id,
        "evaluated": len(results),
        "relevant": len(relevant),
        "irrelevant": len(results) - len(relevant),
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
    })


def main():
    return run_async(_main())


if __name__ == "__main__":
    sys.exit(main())
