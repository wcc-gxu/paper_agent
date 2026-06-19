"""paper-index CLI — 论文全文+摘要索引入库.

独立可执行命令:
    paper-index --paper-id "arxiv:2401.xxxxx" --project-id <id>
    paper-index --paper-id "arxiv:2401.xxxxx" --index-type abstract  # 仅摘要索引
    paper-index --paper-id "arxiv:2401.xxxxx" --index-type fulltext  # 仅全文索引
    paper-index --project-id <id> --all  # 批量索引项目下所有已转换论文

索引到 ChromaDB 双 Collection:
    papers_abstract: paper_id + title + abstract → 快速筛选
    papers_fulltext: paper_id:chunk_index:section → 深度检索

stdout: JSON (机器可读)
stderr: Rich 进度 + 结果 (人类可读)
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
    parser = argparse.ArgumentParser(
        description="论文索引入库 — 摘要 + 全文分块 → ChromaDB 双 Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--paper-id", type=str, default=None,
                        help="论文唯一 ID")
    parser.add_argument("--project-id", type=str, default=None,
                        help="项目 ID")
    parser.add_argument("--all", action="store_true",
                        help="批量索引项目下所有已转换但未索引的论文")
    parser.add_argument(
        "--index-type", type=str, default="both",
        choices=["abstract", "fulltext", "both"],
        help="索引类型: abstract(摘要), fulltext(全文分块), both(两者, 默认)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    return parser


def _get_chroma_store():
    """获取双 Collection ChromaStore 实例。"""
    from ..agent.chroma_store import ChromaStoreV2
    return ChromaStoreV2()


async def _index_single(store, db, paper_id: str, index_type: str) -> dict:
    """索引单篇论文。"""
    row = db.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        return {"paper_id": paper_id, "success": False, "error": "论文不存在"}

    rowd = dict(row)
    result = {"paper_id": paper_id, "title": rowd.get("title", "")}

    # 摘要索引
    if index_type in ("abstract", "both"):
        title = rowd.get("title", "")
        abstract = rowd.get("abstract", "") or ""
        if title or abstract:
            store.add_paper_abstract(
                paper_id=paper_id,
                title=title,
                abstract=abstract,
                metadata={
                    "year": rowd.get("year"),
                    "source": rowd.get("source"),
                    "venue": rowd.get("venue"),
                },
            )
            result["abstract_indexed"] = True
        else:
            result["abstract_indexed"] = False

    # 全文索引
    if index_type in ("fulltext", "both"):
        md_path_str = rowd.get("markdown_path")
        if md_path_str and Path(md_path_str).exists():
            md_text = Path(md_path_str).read_text(encoding="utf-8")
            from ..agent.chunker import SectionChunker
            chunker = SectionChunker()
            chunks = chunker.chunk(md_text, paper_id)
            if chunks:
                count = store.add_fulltext_chunks(chunks)
                result["fulltext_chunks"] = count
                result["fulltext_indexed"] = True
            else:
                result["fulltext_chunks"] = 0
                result["fulltext_indexed"] = False
        else:
            result["fulltext_indexed"] = False
            result["fulltext_error"] = "无 Markdown 全文" if not md_path_str else "Markdown 文件不存在"

    result["success"] = result.get("abstract_indexed") or result.get("fulltext_indexed")
    return result


async def _main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.paper_id and not (args.project_id and args.all):
        parser.print_help()
        console_print("[red]请指定 --paper-id 或 --project-id --all[/red]")
        return 1

    console_print(f"[bold cyan]🗂 paper-index[/bold cyan]")
    console_print(f"   索引类型: {args.index_type}")

    db = create_db()
    store = _get_chroma_store()

    t0 = time.time()
    paper_ids = []

    if args.paper_id:
        paper_ids = [args.paper_id]
    elif args.project_id and args.all:
        # 批量：索引项目下所有已转换为 MD 但尚未全文索引的论文
        rows = db.conn.execute(
            """SELECT p.id, p.title, p.markdown_path, p.abstract FROM papers p
               JOIN project_papers pp ON p.id = pp.paper_id
               WHERE pp.project_id = ? AND p.markdown_path IS NOT NULL""",
            (args.project_id,),
        ).fetchall()
        paper_ids = [dict(r)["id"] for r in rows]
        console_print(f"   论文数: {len(paper_ids)} (来自项目 {args.project_id})")

    if not paper_ids:
        console_print("[yellow]⚠ 没有需要索引的论文[/yellow]")
        output_json({"success": True, "indexed": 0, "results": []})
        db.close()
        return 0

    # 执行索引
    results = []
    for pid in paper_ids:
        with progress_spinner(f"索引中: {pid[:50]}...") as spinner:
            r = await _index_single(store, db, pid, args.index_type)
            results.append(r)
            if spinner:
                status = "✅" if r["success"] else "❌"
                spinner.update(f"{status} {pid[:50]}")

    # 更新 DB
    for r in results:
        if r["success"] and args.index_type in ("fulltext", "both"):
            try:
                db.update_paper_meta(
                    r["paper_id"],
                    embedding_id=f"chroma:{r['paper_id']}",
                )
            except Exception:
                pass

    db.close()

    success_count = sum(1 for r in results if r["success"])
    total_elapsed = time.time() - t0

    console_print(f"\n[green]✅ 索引完成: {success_count}/{len(results)} 篇 "
                  f"({format_duration(total_elapsed)})[/green]")

    # 统计
    abstract_count = sum(1 for r in results if r.get("abstract_indexed"))
    fulltext_count = sum(1 for r in results if r.get("fulltext_indexed"))
    total_chunks = sum(r.get("fulltext_chunks", 0) for r in results)
    console_print(f"   摘要索引: {abstract_count} | 全文索引: {fulltext_count} | 块数: {total_chunks}")

    output_json({
        "success": True,
        "indexed": success_count,
        "total": len(results),
        "abstract_indexed": abstract_count,
        "fulltext_indexed": fulltext_count,
        "total_chunks": total_chunks,
        "elapsed_seconds": round(total_elapsed, 2),
        "results": results,
    })


def main():
    return run_async(_main())


if __name__ == "__main__":
    sys.exit(main())
