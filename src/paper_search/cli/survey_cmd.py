"""paper-survey CLI — 生成文献综述报告.

独立可执行命令:
    paper-survey --project-id <id>
    paper-survey --project-id <id> --output ~/papers/outputs/<id>/survey.md

stdout: JSON
stderr: Rich 进度
"""

import argparse
import sys
import time
from pathlib import Path

from .common import (
    add_project_id_arg,
    console_print,
    create_db,
    format_duration,
    output_error,
    output_json,
    progress_spinner,
    run_async,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 AI 文献综述报告")
    add_project_id_arg(parser)
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="输出文件路径 (默认: ~/papers/outputs/<project_id>/survey.md)")
    return parser


async def _main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.project_id:
        output_error("请指定 --project-id")
        return 1

    db = create_db()
    project = db.get_project(args.project_id)
    if project is None:
        output_error(f"项目不存在: {args.project_id}")
        db.close()
        return 1

    user_query = project.get("user_query", "")
    papers = db.get_project_papers(args.project_id, relevant_only=True)
    if not papers:
        papers = db.get_project_papers(args.project_id)

    if not papers:
        output_error("项目下没有论文")
        db.close()
        return 1

    from ..config import Config
    config = Config()
    output_dir = Path(args.output) if args.output else (
        config.storage_dir / "outputs" / args.project_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "survey.md"

    console_print(f"[bold cyan]📝 paper-survey[/bold cyan]")
    console_print(f"   项目: {args.project_id} | 论文数: {len(papers)}")
    console_print(f"   查询: {user_query[:80]}")

    t0 = time.time()

    with progress_spinner("生成综述...") as spinner:
        try:
            from ..agent.llm_client import LLMClient
            llm = LLMClient()

            # 构建论文摘要列表
            paper_list = []
            for i, p in enumerate(papers[:50], 1):  # 最多50篇
                title = p.get("title", "")
                year = p.get("year", "")
                venue = p.get("venue", "")
                digest = p.get("digest", "") or ""
                paper_list.append(f"{i}. [{year}] {title}\n   Venue: {venue or 'N/A'}\n   Digest: {digest}")

            papers_text = "\n\n".join(paper_list)

            survey_md = await llm.generate_report(
                user_query=user_query,
                papers_text=papers_text,
            )

            output_path.write_text(survey_md or "", encoding="utf-8")
            spinner.update("综述生成完成")

        except Exception as e:
            console_print(f"[red]综述生成失败: {e}[/red]")
            # 回退：生成基础 markdown
            fallback = _generate_fallback_survey(user_query, papers)
            output_path.write_text(fallback, encoding="utf-8")

    db.close()
    elapsed = time.time() - t0

    console_print(f"[green]✅ 综述已生成 ({format_duration(elapsed)})[/green]")
    console_print(f"   📁 {output_path}")

    output_json({
        "success": True,
        "project_id": args.project_id,
        "output_path": str(output_path),
        "paper_count": len(papers),
        "elapsed_seconds": round(elapsed, 2),
    })


def _generate_fallback_survey(user_query: str, papers: list[dict]) -> str:
    """在 LLM 不可用时生成基础综述。"""
    lines = [
        f"# Literature Survey: {user_query}",
        "",
        f"*Auto-generated survey based on {len(papers)} papers.*",
        "",
        "## Papers Overview",
        "",
    ]
    for i, p in enumerate(papers[:50], 1):
        title = p.get("title", "Unknown")
        year = str(p.get("year", ""))
        venue = p.get("venue", "")
        lines.append(f"{i}. **{title}** ({year}) — *{venue}*")
    return "\n".join(lines)


def main():
    return run_async(_main())


if __name__ == "__main__":
    sys.exit(main())
