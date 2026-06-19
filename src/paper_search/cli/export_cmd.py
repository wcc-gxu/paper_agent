"""paper-export CLI — 导出论文引用为 BibTeX 或 JSON.

独立可执行命令:
    paper-export --project-id <id> --format bibtex
    paper-export --project-id <id> --format json --output papers.json

stdout: JSON (或 BibTeX 文本)
stderr: Rich 进度
"""

import argparse
import json
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
    run_async,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出论文引用 — BibTeX 或 JSON")
    add_project_id_arg(parser)
    parser.add_argument("--format", "-f", choices=["bibtex", "json"], default="bibtex",
                        help="导出格式 (默认: bibtex)")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="输出文件路径 (默认: stdout)")
    return parser


def _paper_to_bibtex(p: dict) -> str:
    """将论文转为 BibTeX 条目。"""
    title = p.get("title", "Unknown")
    year = p.get("year", "")
    authors = p.get("authors", "[]")
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except (json.JSONDecodeError, TypeError):
            authors = [authors]
    doi = p.get("doi", "")
    venue = p.get("venue", "")
    source = p.get("source", "")
    arxiv_id = p.get("arxiv_id", "")
    url = p.get("source_url", "")

    # 生成 citation key
    first_author = (authors[0].split()[-1] if authors else "unknown").replace(",", "")
    key = f"{first_author}{year}_{title[:20].replace(' ', '').replace(':', '').replace('-', '')}"

    if arxiv_id:
        entry_type = "article"
        extra = f"  archivePrefix = {{arXiv}},\n  eprint = {{{arxiv_id}}},\n"
    elif doi:
        entry_type = "article"
        extra = f"  doi = {{{doi}}},\n"
    else:
        entry_type = "misc"
        extra = ""

    author_str = " and ".join(a for a in authors[:8] if a)
    if venue:
        extra += f"  journal = {{{venue}}},\n"

    bibtex = f"""@{entry_type}{{{key},
  title = {{{title}}},
  author = {{{author_str}}},
  year = {{{year}}},
{extra}  url = {{{url}}}
}}"""
    return bibtex


async def _main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.project_id:
        output_error("请指定 --project-id")
        return 1

    db = create_db()
    papers = db.get_project_papers(args.project_id)
    db.close()

    if not papers:
        output_error("项目下没有论文")
        return 1

    console_print(f"[bold cyan]📤 paper-export[/bold cyan]")
    console_print(f"   项目: {args.project_id} | 论文: {len(papers)} | 格式: {args.format}")

    t0 = time.time()
    output_text = ""

    if args.format == "bibtex":
        entries = []
        for p in papers:
            try:
                entries.append(_paper_to_bibtex(p))
            except Exception as e:
                entries.append(f"% ERROR for {p.get('title', '?')[:50]}: {e}")
        output_text = "\n\n".join(entries)
    else:  # JSON
        output_text = json.dumps([{
            "title": p.get("title"),
            "authors": p.get("authors"),
            "year": p.get("year"),
            "doi": p.get("doi"),
            "arxiv_id": p.get("arxiv_id"),
            "venue": p.get("venue"),
            "source": p.get("source"),
            "citation_count": p.get("citation_count"),
            "relevance_score": p.get("relevance_score"),
        } for p in papers], ensure_ascii=False, indent=2)

    elapsed = time.time() - t0

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        console_print(f"[green]✅ 已导出到 {args.output} ({format_duration(elapsed)})[/green]")
    else:
        # stdout 直接输出（不加 JSON 包裹）
        print(output_text)

    # stderr 统计
    console_print(f"[dim]   条目数: {len(papers)} | 耗时: {format_duration(elapsed)}[/dim]")

    # 如果输出到文件，stdout 返回 JSON 摘要
    if args.output:
        output_json({
            "success": True,
            "project_id": args.project_id,
            "format": args.format,
            "output_path": args.output,
            "entries": len(papers),
            "elapsed_seconds": round(elapsed, 2),
        })


def main():
    return run_async(_main())


if __name__ == "__main__":
    sys.exit(main())
