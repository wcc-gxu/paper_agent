"""paper-clean CLI — 清理数据库和索引数据.

独立可执行命令:
    paper-clean --project-id <id>
    paper-clean --project-id <id> --keep-pdfs
    paper-clean --all --keep-pdfs  # 清空所有数据但保留 PDF
    paper-clean --all  # 完全清空

stdout: JSON
stderr: Rich 进度
"""

import argparse
import sys

from .common import (
    add_project_id_arg,
    console_print,
    create_db,
    output_error,
    output_json,
    run_async,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清理数据库和索引 — 保留 PDF 文件")
    add_project_id_arg(parser)
    parser.add_argument("--all", action="store_true", help="清理所有数据（慎用）")
    parser.add_argument("--keep-pdfs", action="store_true",
                        help="保留 PDF 和 Markdown 文件，仅清理数据库和向量索引")
    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.project_id and not args.all:
        parser.print_help()
        console_print("[red]请指定 --project-id 或 --all[/red]")
        return 1

    db = create_db()

    if args.project_id and not args.all:
        # 清理单个项目
        project = db.get_project(args.project_id)
        if project is None:
            console_print(f"[yellow]项目不存在: {args.project_id}[/yellow]")
        else:
            # 删除 project_papers 关联
            db.conn.execute("DELETE FROM project_papers WHERE project_id=?",
                            (args.project_id,))
            # 删除 search_logs
            db.conn.execute("DELETE FROM search_logs WHERE project_id=?",
                            (args.project_id,))
            # 删除 project
            db.conn.execute("DELETE FROM projects WHERE id=?",
                            (args.project_id,))
            db.conn.commit()
            console_print(f"[green]✅ 已清理项目: {args.project_id}[/green]")

    elif args.all:
        count_papers = db.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        count_projects = db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

        if args.keep_pdfs:
            # 清空所有表但保留 PDF/MD 文件
            db.conn.execute("DELETE FROM project_papers")
            db.conn.execute("DELETE FROM search_logs")
            db.conn.execute("DELETE FROM papers")
            db.conn.execute("DELETE FROM projects")
            db.conn.execute("DELETE FROM journal_ranks")
            db.conn.commit()

            # 清理 ChromaDB
            try:
                from ..agent.chroma_store import ChromaStoreV2
                store = ChromaStoreV2()
                store.client.delete_collection("papers_abstract")
                store.client.delete_collection("papers_fulltext")
                console_print("[dim]   ChromaDB 索引已清理[/dim]")
            except Exception as e:
                console_print(f"[dim]   ChromaDB 清理跳过: {e}[/dim]")

            console_print(f"[green]✅ 已清理全部数据 ({count_projects} 项目, "
                          f"{count_papers} 论文)，PDF 文件已保留[/green]")
        else:
            # 完全清空
            db.conn.executescript("""
                DELETE FROM project_papers;
                DELETE FROM search_logs;
                DELETE FROM papers;
                DELETE FROM projects;
                DELETE FROM journal_ranks;
            """)
            db.conn.commit()

            try:
                from pathlib import Path
                chroma_dir = Path("~/.paper_search/chroma").expanduser()
                if chroma_dir.exists():
                    import shutil
                    shutil.rmtree(chroma_dir, ignore_errors=True)
                console_print("[dim]   ChromaDB 索引已删除[/dim]")
            except Exception as e:
                console_print(f"[dim]   ChromaDB 清理跳过: {e}[/dim]")

            console_print(f"[yellow]⚠ 已完全清空 ({count_projects} 项目, "
                          f"{count_papers} 论文) 包括索引[/yellow]")

    db.close()

    output_json({"success": True, "cleaned": True})


if __name__ == "__main__":
    sys.exit(run_async(main()))
