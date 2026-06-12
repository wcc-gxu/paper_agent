"""paper-rank CLI — 期刊等级评定.

独立可执行命令:
    paper-rank --project-id <id>
    paper-rank --paper-id "arxiv:xxx"
    paper-rank --project-id <id> --all

stdout: JSON
stderr: Rich 表格
"""

import argparse
import sys
import time

from .common import (
    add_project_id_arg,
    console_print,
    create_db,
    format_duration,
    output_error,
    output_json,
    run_async,
    show_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="期刊/会议等级评定 — CCF/SCI 分级")
    add_project_id_arg(parser)
    parser.add_argument("--paper-id", type=str, default=None, help="单篇论文 ID")
    parser.add_argument("--all", action="store_true", help="评定项目下所有论文")
    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    from ..agent.journal_ranker import JournalRanker

    db = create_db()
    ranker = JournalRanker()

    papers = []

    if args.paper_id:
        row = db.conn.execute("SELECT * FROM papers WHERE id=?", (args.paper_id,)).fetchone()
        if row is None:
            output_error(f"论文不存在: {args.paper_id}")
            db.close()
            return 1
        papers = [dict(row)]
    elif args.project_id and args.all:
        papers = db.get_project_papers(args.project_id)
    else:
        output_error("请指定 --paper-id 或 --project-id --all")
        db.close()
        return 1

    console_print(f"[bold cyan]🏷 paper-rank[/bold cyan]")
    console_print(f"   论文数: {len(papers)}")

    t0 = time.time()
    results = []

    for p in papers:
        venue = p.get("venue", "")
        if venue:
            level = ranker.rank(venue)
            unified = level.get("unified_level") if isinstance(level, dict) else level
            if isinstance(level, dict):
                db.upsert_journal_rank(
                    venue,
                    ccf=level.get("ccf_level"),
                    sci=level.get("sci_zone"),
                    unified=unified,
                )
                db.update_paper_meta(p["id"], unified_level=unified)
            results.append({
                "paper_id": p["id"],
                "title": (p.get("title") or "")[:60],
                "venue": venue,
                "level": unified,
            })

    db.close()
    elapsed = time.time() - t0

    # 显示分级表格
    if results:
        rows = []
        for r in results:
            icon = {"A+": "🏆", "A": "⭐", "B": "📄", "C": "📎"}.get(r.get("level", ""), "❓")
            rows.append([icon, r.get("level", "?"), r["title"][:50],
                          (r.get("venue") or "")[:30]])

        show_table(f"期刊分级结果 ({len(results)} 篇)", ["", "等级", "标题", "期刊/会议"], rows)
        levels = {}
        for r in results:
            lvl = r.get("level", "?")
            levels[lvl] = levels.get(lvl, 0) + 1
        console_print(f"   分布: {dict(sorted(levels.items()))}")

    console_print(f"[green]✅ 分级完成 ({format_duration(elapsed)})[/green]")

    output_json({
        "success": True,
        "total": len(results),
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
    })


if __name__ == "__main__":
    sys.exit(run_async(main()))
