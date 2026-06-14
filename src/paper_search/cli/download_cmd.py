"""paper-download CLI — 下载单篇论文 PDF.

独立可执行命令:
    paper-download --paper-id "arxiv:2401.xxxxx" --source arxiv
    paper-download --title "Attention Is All You Need" --source arxiv
    paper-download --doi "10.1000/xyz" --source semantic_scholar

stdout: JSON (机器可读)
stderr: Rich 进度 + 结果 (人类可读)
"""

import argparse
import sys
import time
from pathlib import Path

from .common import (
    add_output_dir_arg,
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
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载论文 PDF — 通过 paper_id/title/doi 定位并下载 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 定位方式（三选一）
    parser.add_argument("--paper-id", type=str, default=None,
                        help="论文唯一 ID (如 arxiv:2401.xxxxx, doi:10.xxx)")
    parser.add_argument("--title", type=str, default=None, help="论文标题（模糊匹配）")
    parser.add_argument("--doi", type=str, default=None, help="论文 DOI")
    # 来源和输出
    parser.add_argument(
        "--source", "-s", type=str,
        default="arxiv",
        help="来源 (默认: arxiv, 当 paper_id 不含前缀时必须指定)",
    )
    add_output_dir_arg(parser)
    add_project_id_arg(parser)
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.paper_id and not args.title and not args.doi:
        parser.print_help()
        console_print("[red]请指定 --paper-id, --title 或 --doi[/red]")
        return 1

    from ..models import SourceType

    source = SourceType(args.source) if args.source else SourceType.ARXIV
    output_dir = Path(args.output_dir).expanduser().resolve()

    console_print(f"[bold cyan]📥 paper-download[/bold cyan]")
    console_print(f"   目标: {args.paper_id or args.title or args.doi}")
    console_print(f"   来源: {source.value}")
    console_print(f"   输出: {output_dir}")

    t0 = time.time()
    engine = create_engine()
    db = create_db()

    paper = None
    paper_id = args.paper_id

    # 方式1: 直接用 paper_id 从 DB 读取
    if paper_id and db:
        row = db.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row:
            paper_data = dict(row)
            # 重建 Paper 对象
            paper = _row_to_paper(paper_data, source)
            console_print(f"[dim]   从DB读取: {paper.title[:60]}[/dim]")

    # 方式2: 通过 title/doi 搜索
    if paper is None:
        from ..models import SearchQuery

        with progress_spinner(f"搜索定位论文: {args.title or args.doi}...") as spinner:
            query = SearchQuery(
                title=args.title,
                doi=args.doi,
                keywords=args.title or args.doi or "",
                sources=[source],
                max_results=5,
            )
            result = await engine.search(query)
            if not result.papers:
                output_error(f"未找到论文: {args.title or args.doi}")
                await engine.close()
                db.close()
                return 1
            paper = result.papers[0]
            paper_id = db.upsert_paper(paper) if db else None
            if spinner:
                spinner.update(f"已定位: {paper.title[:50]}...")

    if paper is None:
        output_error("无法定位论文")
        await engine.close()
        db.close()
        return 1

    console_print(f"   📄 {paper.title[:80]}")
    console_print()

    # 下载
    with progress_spinner(f"下载中: {paper.title[:50]}...") as spinner:
        dl_result = await engine.download(paper, target_dir=output_dir)
        if spinner:
            spinner.update("下载完成")

    elapsed = time.time() - t0

    if dl_result.success:
        local_path = str(dl_result.local_path)
        # 更新 DB
        if db and paper_id and args.project_id:
            db.mark_pdf_downloaded(args.project_id, paper_id, local_path)
        if db and paper_id:
            db.update_paper_meta(paper_id, pdf_path=local_path)

        console_print(f"[green]✅ 下载成功 ({format_duration(elapsed)})[/green]")
        console_print(f"   📁 {local_path}")

        output_json({
            "success": True,
            "paper_id": paper_id,
            "paper_title": paper.title,
            "source": source.value,
            "local_path": local_path,
            "elapsed_seconds": round(elapsed, 2),
        })
    else:
        console_print(f"[red]❌ 下载失败: {dl_result.error}[/red]")
        output_error(f"下载失败: {dl_result.error}")

    await engine.close()
    db.close()
    return 0


def _row_to_paper(row: dict, source) -> "Paper":
    """从 DB row dict 重建 Paper 对象。"""
    import json
    from ..models import Paper as PaperModel
    authors = json.loads(row.get("authors", "[]")) if isinstance(row.get("authors"), str) else (row.get("authors") or [])
    return PaperModel(
        title=row["title"],
        authors=authors,
        year=row.get("year"),
        abstract=row.get("abstract"),
        doi=row.get("doi"),
        arxiv_id=row.get("arxiv_id"),
        pmid=row.get("pmid"),
        source=source,
        source_url=row.get("source_url"),
        pdf_url=row.get("pdf_url"),
        citation_count=row.get("citation_count"),
        venue=row.get("venue"),
    )


if __name__ == "__main__":
    sys.exit(run_async(main()))
