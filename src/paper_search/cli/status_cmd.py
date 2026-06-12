"""paper-status CLI — 查看项目或论文状态.

独立可执行命令:
    paper-status --project-id <id>
    paper-status --paper-id "arxiv:xxx"
    paper-status  # 列出最近项目

stdout: JSON
stderr: Rich 表格
"""

import argparse
import sys

from .common import (
    create_db,
    add_project_id_arg,
    console_print,
    output_error,
    output_json,
    run_async,
    show_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看搜索项目或论文状态")
    add_project_id_arg(parser)
    parser.add_argument("--paper-id", type=str, default=None, help="论文 ID")
    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    db = create_db()

    if args.paper_id:
        # 单篇论文详情
        row = db.conn.execute(
            """SELECT p.*, pp.relevance_score, pp.relevance_reason,
                      pp.pdf_downloaded, pp.pdf_path
               FROM papers p
               LEFT JOIN project_papers pp ON p.id = pp.paper_id
               WHERE p.id = ?""",
            (args.paper_id,),
        ).fetchone()

        if row is None:
            output_error(f"论文不存在: {args.paper_id}")
            db.close()
            return 1

        pd = dict(row)
        console_print(f"[bold cyan]📄 论文详情[/bold cyan]")
        console_print(f"   ID: {pd['id']}")
        console_print(f"   标题: {pd.get('title', '')}")
        console_print(f"   年份: {pd.get('year', '')} | 来源: {pd.get('source', '')}")
        console_print(f"   期刊: {pd.get('venue', '')} | 等级: {pd.get('unified_level', '?')}")
        console_print(f"   引用: {pd.get('citation_count', 0)}")
        console_print(f"   相关性: {pd.get('relevance_score', 'N/A')}")
        console_print(f"   PDF: {'✅' if pd.get('pdf_downloaded') else '❌'} {pd.get('pdf_path', '')}")
        console_print(f"   MD: {'✅' if pd.get('markdown_path') else '❌'} {pd.get('markdown_path', '')}")
        console_print(f"   Digest: {(pd.get('digest') or '')[:200]}")

        output_json(pd)

    elif args.project_id:
        # 项目详情
        project = db.get_project(args.project_id)
        if project is None:
            output_error(f"项目不存在: {args.project_id}")
            db.close()
            return 1

        papers = db.get_project_papers(args.project_id)

        console_print(f"[bold cyan]📊 项目状态[/bold cyan]")
        console_print(f"   ID: {args.project_id}")
        console_print(f"   查询: {project.get('user_query', '')[:100]}")
        console_print(f"   状态: {project.get('status', 'unknown')}")
        console_print(f"   论文总数: {project.get('total_papers_found', len(papers))}")
        console_print(f"   相关论文: {len([p for p in papers if p.get('relevance_score', 0) >= 0.5])}")
        console_print(f"   已下载: {project.get('total_downloaded', sum(1 for p in papers if p.get('pdf_downloaded')))}")
        console_print(f"   创建时间: {project.get('created_at', '')}")
        console_print()

        if papers:
            rows = []
            for p in papers[:20]:
                status = "📥" if p.get("pdf_downloaded") else ("✅" if p.get("relevance_score", 0) >= 0.5 else "⏳")
                rows.append([
                    status,
                    f"{p.get('relevance_score', 0):.1f}",
                    (p.get('title') or '')[:50],
                    str(p.get('year', '')),
                    p.get('unified_level', '?'),
                ])
            show_table("项目论文", ["", "得分", "标题", "年", "等级"], rows)

        output_json({
            "project": project,
            "papers": papers,
        })

    else:
        # 列出最近项目
        projects = db.list_projects(10)
        console_print(f"[bold cyan]📋 最近项目 ({len(projects)})[/bold cyan]")
        if projects:
            rows = []
            for p in projects:
                rows.append([
                    p["id"],
                    (p.get("user_query") or "")[:50],
                    p.get("status", "?"),
                    str(p.get("total_papers_found", 0)),
                    str(p.get("total_relevant", 0)),
                ])
            show_table("最近项目", ["ID", "查询", "状态", "论文数", "相关"], rows)
        else:
            console_print("[dim]尚无项目[/dim]")

        output_json({"projects": projects})

    db.close()


if __name__ == "__main__":
    sys.exit(run_async(main()))
