"""MCP Server v2 — 薄封装层，每个 MCP Tool 对应一个独立 CLI 操作.

启动方式:
    fastmcp run src/paper_search/mcp/server.py
    或
    python -m paper_search.mcp.server

架构:
    MCP Tools → 直接调用 Engine / AgentDB / ChromaStore / 各 CLI 函数
    不再使用一体式 8 阶段 ResearchAgent 管道 —— 已拆分为独立 CLI 工具集。
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

from fastmcp import FastMCP
from pydantic import Field

from ..config import Config
from ..engine import PaperSearchEngine
from ..models import SearchQuery, SourceType

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "Paper Search",
    instructions=(
        "学术论文搜索与 PDF 下载引擎。支持 arXiv、Semantic Scholar、PubMed、"
        "ScienceDirect 等多源文献搜索和 PDF 下载。"
        "核心流程: paper-search → paper-download → paper-convert → paper-index。"
        "辅助工具: paper-evaluate / paper-rank / paper-survey / paper-export / paper-status / paper-clean。"
    ),
)

# ── 惰性初始化 ──────────────────────────────────────────

_engine: Optional[PaperSearchEngine] = None
_db = None


def _get_engine() -> PaperSearchEngine:
    global _engine
    if _engine is None:
        _load_providers()
        _engine = PaperSearchEngine(Config())
    return _engine


def _get_db():
    global _db
    if _db is None:
        from ..agent.db import AgentDB
        _db = AgentDB()
    return _db


def _load_providers():
    for mod in ["arxiv_provider", "semanticscholar_provider", "pubmed_provider",
                "cnki_provider", "ieee_provider", "sciencedirect_provider"]:
        try:
            __import__(f"..providers.{mod}", fromlist=["paper_search.providers"], globals=globals(), level=1)
        except ImportError:
            pass


# ── 辅助 ──────────────────────────────────────────────────


def _paper_to_dict(paper) -> dict:
    return {
        "title": paper.title,
        "authors": paper.authors[:10],
        "year": paper.year,
        "abstract": (paper.abstract[:500] + "..." if paper.abstract and len(paper.abstract) > 500 else paper.abstract),
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "source": paper.source.value,
        "source_url": paper.source_url,
        "pdf_url": paper.pdf_url,
        "citation_count": paper.citation_count,
        "venue": paper.venue,
    }


# ── 核心 MCP Tools ────────────────────────────────────────


@mcp.tool()
async def search_papers(
    keywords: Annotated[str, Field(description="搜索关键词，支持 AND/OR 逻辑。如 'adversarial attack AND robustness'")] = "",
    sources: Annotated[str, Field(description="搜索来源，逗号分隔。可选: arxiv,semantic_scholar,pubmed,cnki,ieee,sciencedirect")] = "arxiv,semantic_scholar",
    title: Annotated[Optional[str], Field(description="按标题精确搜索")] = None,
    author: Annotated[Optional[str], Field(description="按作者筛选")] = None,
    doi: Annotated[Optional[str], Field(description="按 DOI 直接查找")] = None,
    year_from: Annotated[Optional[int], Field(description="起始发表年份")] = None,
    year_to: Annotated[Optional[int], Field(description="截止发表年份")] = None,
    max_results: Annotated[int, Field(description="每个来源最大返回结果数 (1-100, 默认 20)", ge=1, le=100)] = 20,
    project_id: Annotated[Optional[str], Field(description="关联的项目 ID（不提供则自动创建）")] = None,
) -> str:
    """跨多源搜索学术论文 — 去重并将元数据写入 SQLite。

    返回论文元数据列表（标题、作者、年份、摘要、DOI、PDF链接等）。
    来源包括 arXiv、Semantic Scholar、PubMed 等。
    """
    engine = _get_engine()
    db = _get_db()

    source_list = [SourceType(s.strip().lower()) for s in sources.split(",") if s.strip()]
    if not source_list:
        source_list = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

    query = SearchQuery(
        keywords=keywords, title=title, author=author, doi=doi,
        year_from=year_from, year_to=year_to,
        max_results=max_results, sources=source_list,
    )

    result = await engine.search(query)

    # 写入 SQLite
    pid = project_id
    if pid is None:
        pid = db.create_project(user_query=query.effective_query())

    paper_ids = []
    for p in result.papers:
        paper_id = db.upsert_paper(p)
        db.link_paper_to_project(pid, paper_id)
        paper_ids.append(paper_id)

    output = {
        "success": True,
        "project_id": pid,
        "search_term": query.effective_query(),
        "total_found": result.total_found,
        "sources_searched": [s.value for s in source_list],
        "errors": result.errors,
        "paper_ids": paper_ids,
        "papers": [_paper_to_dict(p) for p in result.papers],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp.tool()
async def download_paper(
    title: Annotated[str, Field(description="论文标题（完整或部分）")],
    source: Annotated[str, Field(description="来源: arxiv, semantic_scholar, pubmed, ieee, sciencedirect")],
    paper_id: Annotated[Optional[str], Field(description="论文 DB ID（如已知，优先使用）")] = None,
    doi: Annotated[Optional[str], Field(description="论文 DOI")] = None,
    project_id: Annotated[Optional[str], Field(description="关联的项目 ID")] = None,
    output_dir: Annotated[str, Field(description="下载目录，默认 ~/papers")] = "~/papers",
) -> str:
    """下载单篇论文的 PDF 文件到本地。

    先搜索定位论文（或从 DB 查找），然后下载 PDF。
    文件按 {来源}/{年份}/{作者}_{年份}_{标题}.pdf 格式组织。
    """
    engine = _get_engine()
    db = _get_db()

    source_type = SourceType(source.strip().lower())
    target_dir = Path(output_dir).expanduser().resolve()

    paper = None
    p_id = paper_id

    # 方式1: 从 DB 查找
    if p_id:
        row = db.conn.execute("SELECT * FROM papers WHERE id=?", (p_id,)).fetchone()
        if row:
            from ..models import Paper
            rd = dict(row)
            paper = Paper(
                title=rd["title"], authors=json.loads(rd.get("authors", "[]")),
                year=rd.get("year"), abstract=rd.get("abstract"),
                doi=rd.get("doi"), arxiv_id=rd.get("arxiv_id"),
                source=source_type, source_url=rd.get("source_url"),
                pdf_url=rd.get("pdf_url"),
            )

    # 方式2: 搜索定位
    if paper is None:
        query = SearchQuery(title=title, doi=doi, keywords=title,
                            sources=[source_type], max_results=5)
        result = await engine.search(query)
        if not result.papers:
            return json.dumps({"success": False, "error": f"未找到论文: {title}"}, ensure_ascii=False)
        paper = result.papers[0]
        p_id = db.upsert_paper(paper)

    # 下载
    dl_result = await engine.download(paper, target_dir=target_dir)

    # 更新 DB
    if dl_result.success and p_id:
        if project_id:
            db.mark_pdf_downloaded(project_id, p_id, str(dl_result.local_path))
        db.update_paper_meta(p_id, pdf_path=str(dl_result.local_path))

    return json.dumps({
        "success": dl_result.success,
        "paper_id": p_id,
        "paper_title": paper.title,
        "source": source_type.value,
        "local_path": dl_result.local_path,
        "error": dl_result.error,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def batch_search(
    file_path: Annotated[str, Field(description="查询文件的绝对路径 (.json 或 .csv)")],
    download: Annotated[bool, Field(description="是否同时下载所有论文的 PDF")] = False,
    default_sources: Annotated[str, Field(description="默认来源列表，逗号分隔")] = "arxiv,semantic_scholar",
) -> str:
    """从 JSON/CSV 文件读取多个搜索查询并批量执行。

    JSON 格式: [{"keywords": "transformer", "sources": ["arxiv"], "max_results": 10}, ...]
    CSV 格式: keywords,sources,max_results (来源用 | 分隔)
    """
    engine = _get_engine()
    src_list = [SourceType(s.strip().lower()) for s in default_sources.split(",") if s.strip()]

    try:
        summary = await engine.batch_search_from_file(file_path, download=download, default_sources=src_list)
    except FileNotFoundError:
        return json.dumps({"success": False, "error": f"文件不存在: {file_path}"}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    papers_preview = []
    for r in summary.results[:3]:
        papers_preview.append({
            "query": r.query.effective_query(),
            "papers_found": r.total_found,
            "titles": [p.title[:80] for p in r.papers[:5]],
        })

    return json.dumps({
        "success": True,
        "total_queries": summary.total_queries,
        "total_papers_found": summary.total_papers_found,
        "total_downloaded": summary.total_downloaded,
        "total_failed": summary.total_failed,
        "preview": papers_preview,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_sources() -> str:
    """列出所有可用的文献搜索来源及其状态。

    返回每个来源的名称、类型（免费API / 校内IP）、是否可用。
    """
    engine = _get_engine()
    _load_providers()
    from ..providers import list_providers as get_all

    health = await engine.health_check()
    descriptions = {
        "arxiv": "arXiv 预印本 — CS/AI/数学/物理等领域",
        "semantic_scholar": "Semantic Scholar — 跨学科综合学术搜索",
        "pubmed": "PubMed/PMC — 生物医学文献数据库",
        "cnki": "中国知网 CNKI — 中文学术论文/学位论文/专利",
        "ieee": "IEEE Xplore — 电子/计算机工程论文",
        "sciencedirect": "Elsevier ScienceDirect — 综合学术期刊",
    }

    sources_info = []
    for st in get_all():
        sources_info.append({
            "name": st.value,
            "description": descriptions.get(st.value, ""),
            "available": health.get(st.value, False),
            "type": "校内IP" if st.value in ("cnki", "ieee", "sciencedirect") else "免费API",
        })
    return json.dumps({"total_sources": len(sources_info), "sources": sources_info}, ensure_ascii=False, indent=2)


# ── 项目/论文管理 MCP Tools ───────────────────────────────


@mcp.tool()
async def paper_status(
    project_id: Annotated[Optional[str], Field(description="项目 ID")] = None,
    paper_id: Annotated[Optional[str], Field(description="论文 ID")] = None,
    limit: Annotated[int, Field(description="列出最近 N 个历史项目 (默认 10)")] = 10,
) -> str:
    """查看搜索项目或论文状态 — 对应 CLI: paper-status。

    不指定参数时列出最近项目历史。
    """
    db = _get_db()

    if paper_id:
        row = db.conn.execute(
            """SELECT p.*, pp.relevance_score, pp.pdf_downloaded
               FROM papers p LEFT JOIN project_papers pp ON p.id = pp.paper_id
               WHERE p.id = ?""", (paper_id,)
        ).fetchone()
        if row is None:
            return json.dumps({"error": f"论文不存在: {paper_id}"}, ensure_ascii=False)
        return json.dumps(dict(row), ensure_ascii=False, indent=2, default=str)

    if project_id:
        project = db.get_project(project_id)
        if project is None:
            return json.dumps({"error": f"项目不存在: {project_id}"}, ensure_ascii=False)
        papers = db.get_project_papers(project_id)
        return json.dumps({"project": project, "papers_count": len(papers)}, ensure_ascii=False, indent=2, default=str)

    projects = db.list_projects(limit)
    return json.dumps(projects, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
async def paper_export(
    project_id: Annotated[str, Field(description="项目 ID")],
    format: Annotated[str, Field(description="导出格式: bibtex 或 json")] = "bibtex",
) -> str:
    """导出项目论文为 BibTeX 或 JSON — 对应 CLI: paper-export。"""
    db = _get_db()
    papers = db.get_project_papers(project_id)
    if not papers:
        return json.dumps({"error": f"项目不存在或无论文: {project_id}"}, ensure_ascii=False)

    if format == "bibtex":
        entries = []
        for p in papers:
            authors = json.loads(p.get("authors", "[]")) if isinstance(p.get("authors"), str) else (p.get("authors") or [])
            first_author = (authors[0].split()[-1] if authors else "Unknown").replace(",", "")
            key = f"{first_author}{p.get('year', '????')}{p['title'][:20].replace(' ', '').replace(':', '')}"
            author_str = " and ".join(a for a in authors[:8] if a)
            arxiv_extra = f"  archivePrefix = {{arXiv}},\n  eprint = {{{p.get('arxiv_id')}}},\n" if p.get("arxiv_id") else ""
            doi_extra = f"  doi = {{{p.get('doi')}}},\n" if p.get("doi") else ""
            venue_extra = f"  journal = {{{p.get('venue')}}},\n" if p.get("venue") else ""
            entry = (
                f"@article{{{key},\n"
                f"  title = {{{p['title']}}},\n"
                f"  author = {{{author_str}}},\n"
                f"  year = {{{p.get('year', '????')}}},\n"
                f"{arxiv_extra}{doi_extra}{venue_extra}"
                f"  url = {{{p.get('source_url', '')}}}\n"
                f"}}"
            )
            entries.append(entry)
        return "\n\n".join(entries)
    else:
        clean_papers = [{
            "title": p.get("title"), "authors": p.get("authors"), "year": p.get("year"),
            "doi": p.get("doi"), "arxiv_id": p.get("arxiv_id"), "venue": p.get("venue"),
            "relevance_score": p.get("relevance_score"), "unified_level": p.get("unified_level"),
        } for p in papers]
        return json.dumps(clean_papers, ensure_ascii=False, indent=2)


@mcp.tool()
async def paper_clean(
    project_id: Annotated[Optional[str], Field(description="要清理的项目 ID")] = None,
    all: Annotated[bool, Field(description="清理所有数据（慎用）")] = False,
    keep_pdfs: Annotated[bool, Field(description="保留 PDF 和 Markdown 文件")] = False,
) -> str:
    """清理数据库和索引 — 对应 CLI: paper-clean。"""
    db = _get_db()

    if project_id and not all:
        db.conn.execute("DELETE FROM project_papers WHERE project_id=?", (project_id,))
        db.conn.execute("DELETE FROM search_logs WHERE project_id=?", (project_id,))
        db.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        db.conn.commit()
        return json.dumps({"success": True, "message": f"项目 {project_id} 已清理"}, ensure_ascii=False)

    if all:
        count = db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        db.conn.execute("DELETE FROM project_papers")
        db.conn.execute("DELETE FROM search_logs")
        db.conn.execute("DELETE FROM papers" if not keep_pdfs else "SELECT 1")
        db.conn.execute("DELETE FROM projects")
        db.conn.execute("DELETE FROM journal_ranks")
        db.conn.commit()
        if not keep_pdfs:
            db.conn.execute("DELETE FROM papers")
            db.conn.commit()
        return json.dumps({"success": True, "message": f"已清理 {count} 个项目", "keep_pdfs": keep_pdfs}, ensure_ascii=False)

    return json.dumps({"error": "请指定 --project-id 或 --all"}, ensure_ascii=False)


# ── 引用追踪（保持独立实现）───────────────────────────────


@mcp.tool()
async def citation_chase(
    paper_title: Annotated[str, Field(description="种子论文标题")],
    doi: Annotated[Optional[str], Field(description="论文 DOI")] = None,
) -> str:
    """独立引用追踪 — 找到引用了某篇论文的后继研究和它引用的前人工作（1层）。"""
    engine = _get_engine()
    query = SearchQuery(title=paper_title, doi=doi, sources=[SourceType.SEMANTIC_SCHOLAR], max_results=1)
    result = await engine.search(query)
    if not result.papers:
        return json.dumps({"error": f"未找到论文: {paper_title}"}, ensure_ascii=False)

    paper = result.papers[0]
    from ..providers.semanticscholar_provider import SemanticScholarProvider
    s2 = SemanticScholarProvider()
    citations_info = {"seed_paper": paper.title, "doi": paper.doi, "citing": [], "references": []}

    if paper.doi:
        try:
            async with s2._make_client() as client:
                resp = await client.get(f"/paper/DOI:{paper.doi}/citations",
                                        params={"fields": "title,year,authors,url", "limit": 20})
                if resp.status_code == 200:
                    for item in resp.json().get("data", []):
                        cp = item.get("citingPaper", {})
                        citations_info["citing"].append(
                            {"title": cp.get("title", ""), "year": cp.get("year"), "url": cp.get("url")})
                resp2 = await client.get(f"/paper/DOI:{paper.doi}/references",
                                         params={"fields": "title,year,authors,url", "limit": 20})
                if resp2.status_code == 200:
                    for item in resp2.json().get("data", []):
                        rp = item.get("citedPaper", {})
                        citations_info["references"].append(
                            {"title": rp.get("title", ""), "year": rp.get("year"), "url": rp.get("url")})
        except Exception as e:
            citations_info["error"] = str(e)

    return json.dumps(citations_info, ensure_ascii=False, indent=2)


# ── 直接运行入口 ──────────────────────────────────────────


def main():
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
