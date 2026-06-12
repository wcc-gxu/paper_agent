"""paper-search CLI — 单次学术论文搜索（仅元数据，不含PDF）.

独立可执行命令，也可注册为 console_scripts:
    paper-search --keywords "transformer" --sources arxiv,semantic_scholar --year-from 2024

stdout: JSON (机器可读)
stderr: Rich 进度 + 结果表格 (人类可读)
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .common import (
    add_max_results_arg,
    add_project_id_arg,
    console_print,
    create_db,
    create_engine,
    format_duration,
    output_error,
    output_json,
    parse_sources,
    progress_spinner,
    run_async,
    show_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="学术论文搜索 — 多源并发搜索，去重后返回元数据 + 写入 SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--keywords", "-k", type=str, default="",
        help="搜索关键词，支持 AND/OR 逻辑",
    )
    parser.add_argument("--title", "-t", type=str, default=None, help="按标题精确搜索")
    parser.add_argument("--author", "-a", type=str, default=None, help="按作者筛选")
    parser.add_argument("--doi", "-d", type=str, default=None, help="按 DOI 查找")
    parser.add_argument("--year-from", type=int, default=None, help="起始年份")
    parser.add_argument("--year-to", type=int, default=None, help="截止年份")
    parser.add_argument(
        "--sources", "-s", type=str, default="arxiv,semantic_scholar",
        help="来源，逗号分隔 (默认: arxiv,semantic_scholar)",
    )
    add_max_results_arg(parser, default=40)
    add_project_id_arg(parser)
    parser.add_argument(
        "--no-db", action="store_true",
        help="不写入 SQLite 数据库（仅 stdout JSON）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    return parser


def _paper_to_dict(paper) -> dict:
    """将 Paper 转为 JSON 友好的 dict。"""
    return {
        "title": paper.title,
        "authors": paper.authors[:10] if paper.authors else [],
        "year": paper.year,
        "abstract": (paper.abstract[:500] + "...") if paper.abstract and len(paper.abstract) > 500 else paper.abstract,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "source": paper.source.value if hasattr(paper.source, "value") else str(paper.source),
        "source_url": paper.source_url,
        "pdf_url": paper.pdf_url,
        "citation_count": paper.citation_count,
        "venue": paper.venue,
    }


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.keywords and not args.title and not args.doi and not args.author:
        parser.print_help()
        return 1

    from ..models import SearchQuery

    sources = parse_sources(args.sources)
    query = SearchQuery(
        keywords=args.keywords or "",
        title=args.title,
        author=args.author,
        doi=args.doi,
        year_from=args.year_from,
        year_to=args.year_to,
        max_results=args.max_results,
        sources=sources,
    )

    console_print(f"[bold cyan]🔍 paper-search[/bold cyan]")
    console_print(f"   查询: {query.effective_query()[:80]}")
    console_print(f"   来源: {[s.value for s in sources]}")
    console_print(f"   年份: {args.year_from or '不限'}–{args.year_to or '不限'}")
    console_print()

    t0 = time.time()
    engine = create_engine()
    db = None if args.no_db else create_db()

    # 如果指定了 project_id，创建或确认项目
    project_id = args.project_id
    if db and project_id is None:
        project_id = db.create_project(
            user_query=query.effective_query(),
            parsed_intent={"keywords": args.keywords, "sources": args.sources},
        )
        console_print(f"[dim]   project_id: {project_id}[/dim]\n")
    elif db and project_id:
        # 确认项目存在
        existing = db.get_project(project_id)
        if existing is None:
            db.create_project(
                user_query=query.effective_query(),
                parsed_intent={"keywords": args.keywords, "sources": args.sources},
                project_id=project_id,
            )

    # 执行搜索（带 spinner）
    errors = []
    papers = []
    with progress_spinner(f"搜索中: {query.effective_query()[:60]}...") as spinner:
        try:
            result = await engine.search(query)
            papers = result.papers
            errors = result.errors
        except Exception as e:
            output_error(f"搜索失败: {e}")
            await engine.close()
            if db:
                db.close()
            return 1

    elapsed = time.time() - t0

    # 写入 SQLite
    paper_ids = []
    if db and papers:
        for p in papers:
            pid = db.upsert_paper(p)
            paper_ids.append(pid)
            if project_id:
                db.link_paper_to_project(project_id, pid)

        console_print(f"[green]✅ 搜索完成: {len(papers)} 篇论文 "
                      f"({format_duration(elapsed)})[/green]")

    # 显示错误
    if errors:
        for err in errors:
            console_print(f"[yellow]  ⚠ {err}[/yellow]")

    # Rich 结果表格
    if papers:
        rows = []
        for i, p in enumerate(papers[:20], 1):
            title = p.title[:80] + ("..." if len(p.title) > 80 else "")
            author = (p.authors[0] if p.authors else "N/A")[:20]
            year = str(p.year) if p.year else "----"
            src = p.source.value if hasattr(p.source, "value") else str(p.source)
            cite = str(p.citation_count) if p.citation_count else "0"
            rows.append([str(i), title, author, year, src, cite])

        show_table(
            f"搜索结果 (前 {min(20, len(papers))}/{len(papers)} 篇)",
            ["#", "标题", "一作", "年", "源", "引用"],
            rows,
        )
        if len(papers) > 20:
            console_print(f"[dim]  ... 还有 {len(papers) - 20} 篇[/dim]")

    await engine.close()
    if db:
        db.close()

    # stdout JSON 输出
    output = {
        "success": True,
        "project_id": project_id,
        "total_found": len(papers),
        "errors": errors[:5],
        "elapsed_seconds": round(elapsed, 2),
        "paper_ids": paper_ids if db else [p.doi or p.arxiv_id or p.title[:50] for p in papers],
        "papers": [_paper_to_dict(p) for p in papers],
    }
    output_json(output)


if __name__ == "__main__":
    sys.exit(run_async(main()))
