"""paper-convert CLI — PDF 转 Markdown.

独立可执行命令:
    paper-convert --paper-id "arxiv:2401.xxxxx"
    paper-convert --pdf-path ~/papers/arxiv/2024/Author_Title.pdf
    paper-convert --project-id <id> --all  # 批量转换项目下所有已下载的 PDF

stdout: JSON (机器可读)
stderr: Rich 进度 + 结果 (人类可读)
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .common import (
    add_output_dir_arg,
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
    parser = argparse.ArgumentParser(
        description="PDF→Markdown 转换 — 使用 docling 将论文 PDF 转为结构化 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 定位方式
    parser.add_argument("--paper-id", type=str, default=None,
                        help="论文唯一 ID（自动从 DB 查找 PDF 路径）")
    parser.add_argument("--pdf-path", type=str, default=None,
                        help="PDF 文件路径（当不从 DB 查找时使用）")
    parser.add_argument("--project-id", type=str, default=None,
                        help="项目 ID（与 --all 配合批量转换）")
    parser.add_argument("--all", action="store_true",
                        help="批量转换项目下所有已下载 PDF")
    # 输出
    from ..config import get_markdown_dir
    add_output_dir_arg(parser, default=str(get_markdown_dir()))
    parser.add_argument("--max-concurrent", type=int, default=2,
                        help="最大并发转换数 (默认: 2)")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    return parser


async def _convert_single(converter, pdf_path: Path, output_dir: Path,
                          paper_id: str = None) -> dict:
    """转换单个 PDF，返回结果 dict。"""
    from ..agent.pdf_converter import PDFConverter
    t0 = time.time()
    md_path = await converter.convert(pdf_path, output_dir)
    elapsed = time.time() - t0

    if md_path:
        return {
            "paper_id": paper_id,
            "pdf_path": str(pdf_path),
            "markdown_path": str(md_path),
            "success": True,
            "elapsed_seconds": round(elapsed, 2),
        }
    else:
        return {
            "paper_id": paper_id,
            "pdf_path": str(pdf_path),
            "success": False,
            "error": "转换失败",
            "elapsed_seconds": round(elapsed, 2),
        }


async def _main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.paper_id and not args.pdf_path and not (args.project_id and args.all):
        parser.print_help()
        console_print("[red]请指定 --paper-id, --pdf-path 或 --project-id --all[/red]")
        return 1

    from ..agent.pdf_converter import PDFConverter

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    converter = PDFConverter(max_concurrent=args.max_concurrent)
    db = create_db()

    console_print(f"[bold cyan]📄 paper-convert[/bold cyan]")
    console_print(f"   输出: {output_dir}")

    t0_total = time.time()
    results = []
    pdf_tasks = []  # [(pdf_path, paper_id), ...]

    # 收集待转换的 PDF
    if args.project_id and args.all and db:
        # 批量：转换项目下所有已下载但未转换的 PDF
        rows = db.conn.execute(
            """SELECT p.id, p.title, pp.pdf_path FROM papers p
               JOIN project_papers pp ON p.id = pp.paper_id
               WHERE pp.project_id = ? AND pp.pdf_downloaded = 1
               AND pp.pdf_path IS NOT NULL""",
            (args.project_id,),
        ).fetchall()

        for row in rows:
            pdf_path = Path(row["pp.pdf_path"] if "pp.pdf_path" in row.keys()
                            else row["pdf_path"])
            if pdf_path.exists():
                # 检查是否已有 markdown_path
                existing = db.conn.execute(
                    "SELECT markdown_path FROM papers WHERE id=?", (row["id"],)
                ).fetchone()
                existing_md = (existing["markdown_path"] if existing and "markdown_path" in existing.keys()
                               else None)
                if existing_md and Path(existing_md).exists():
                    console_print(f"[dim]   ⏭ 跳过（已有MD）: {row['title'][:50]}[/dim]")
                    continue
                pdf_tasks.append((pdf_path, row["id"]))

        console_print(f"   待转换: {len(pdf_tasks)} 个 PDF（来自项目 {args.project_id}）")

    elif args.paper_id and db:
        # 从 DB 查找
        row = db.conn.execute(
            "SELECT id, title, markdown_path FROM papers WHERE id=?",
            (args.paper_id,),
        ).fetchone()
        if row is None:
            output_error(f"未找到论文: {args.paper_id}")
            db.close()
            return 1

        # 查找 PDF 路径
        pp = db.conn.execute(
            "SELECT pdf_path FROM project_papers WHERE paper_id=? AND pdf_downloaded=1",
            (args.paper_id,),
        ).fetchone()
        pdf_path_str = pp["pdf_path"] if pp else None

        if pdf_path_str is None:
            output_error(f"论文尚未下载 PDF: {args.paper_id}")

            db.close()
            return 1

        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists():
            output_error(f"PDF 文件不存在: {pdf_path}")
            db.close()
            return 1

        pdf_tasks.append((pdf_path, args.paper_id))

    elif args.pdf_path:
        pdf_path = Path(args.pdf_path).expanduser().resolve()
        if not pdf_path.exists():
            output_error(f"PDF 文件不存在: {pdf_path}")
            db.close()
            return 1
        pdf_tasks.append((pdf_path, args.paper_id or pdf_path.stem))

    if not pdf_tasks:
        console_print("[yellow]⚠ 没有需要转换的 PDF[/yellow]")
        output_json({"success": True, "converted": 0, "results": []})
        db.close()
        return 0

    # 执行转换
    console_print(f"   开始转换 {len(pdf_tasks)} 个文件...\n")

    tasks = []
    for pdf_path, pid in pdf_tasks:
        tasks.append(_convert_single(converter, pdf_path, output_dir, pid))

    results = await asyncio.gather(*tasks)

    # 更新 DB
    for r in results:
        if r["success"] and r["paper_id"] and db:
            try:
                db.update_paper_meta(r["paper_id"], markdown_path=r["markdown_path"])
            except Exception:
                pass

    db.close()

    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count
    total_elapsed = time.time() - t0_total

    # 输出摘要
    if success_count > 0:
        console_print(f"\n[green]✅ 转换完成: {success_count} 成功, "
                      f"{fail_count} 失败 ({format_duration(total_elapsed)})[/green]")
        for r in results:
            if r["success"]:
                md_name = Path(r["markdown_path"]).name
                console_print(f"   📝 {md_name}")

    if fail_count > 0:
        for r in results:
            if not r["success"]:
                console_print(f"[red]   ❌ {r['paper_id']}: {r.get('error')}[/red]")

    output_json({
        "success": True,
        "converted": success_count,
        "failed": fail_count,
        "elapsed_seconds": round(total_elapsed, 2),
        "results": results,
    })


def main():
    return run_async(_main())


if __name__ == "__main__":
    sys.exit(main())
