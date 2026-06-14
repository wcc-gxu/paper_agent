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

from ..config import Config, get_papers_dir, get_markdown_dir, get_outputs_dir
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
    output_dir: Annotated[str, Field(description="下载目录")] = "",
) -> str:
    """下载单篇论文的 PDF 文件到本地。

    先搜索定位论文（或从 DB 查找），然后下载 PDF。
    文件按 {来源}/{年份}/{作者}_{年份}_{标题}.pdf 格式组织。
    """
    engine = _get_engine()
    db = _get_db()

    source_type = SourceType(source.strip().lower())
    target_dir = Path(output_dir or str(get_papers_dir())).expanduser().resolve()

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


# ── 转换/索引/评估/排名/综述 MCP Tools ──────────────────────


@mcp.tool()
async def convert_paper(
    paper_id: Annotated[Optional[str], Field(description="论文唯一 ID（从 DB 查找 PDF 路径）")] = None,
    pdf_path: Annotated[Optional[str], Field(description="PDF 文件路径（直接指定）")] = None,
    project_id: Annotated[Optional[str], Field(description="项目 ID（与 --all 配合批量转换）")] = None,
    all: Annotated[bool, Field(description="批量转换项目下所有已下载 PDF")] = False,
    output_dir: Annotated[str, Field(description="输出目录")] = "",
    max_concurrent: Annotated[int, Field(description="最大并发转换数")] = 2,
) -> str:
    """PDF 转 Markdown — 使用 pymupdf4llm 将论文 PDF 转为结构化 Markdown。

    支持单篇转换（--paper-id / --pdf-path）和批量转换（--project-id --all）。
    """
    import asyncio
    import time
    from pathlib import Path
    from ..agent.pdf_converter import PDFConverter

    db = _get_db()
    out_dir = Path(output_dir or str(get_markdown_dir())).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    converter = PDFConverter(max_concurrent=max_concurrent)
    pdf_tasks = []

    if project_id and all:
        rows = db.conn.execute(
            """SELECT p.id, p.title, pp.pdf_path FROM papers p
               JOIN project_papers pp ON p.id = pp.paper_id
               WHERE pp.project_id = ? AND pp.pdf_downloaded = 1
               AND pp.pdf_path IS NOT NULL""",
            (project_id,),
        ).fetchall()
        for row in rows:
            rd = dict(row)
            ppath = Path(rd.get("pdf_path", ""))
            if ppath.exists():
                existing = db.conn.execute(
                    "SELECT markdown_path FROM papers WHERE id=?", (rd["id"],)
                ).fetchone()
                if existing and existing["markdown_path"] and Path(existing["markdown_path"]).exists():
                    continue
                pdf_tasks.append((ppath, rd["id"]))
    elif paper_id:
        row = db.conn.execute("SELECT id, title FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return json.dumps({"success": False, "error": f"论文不存在: {paper_id}"}, ensure_ascii=False)
        pp = db.conn.execute(
            "SELECT pdf_path FROM project_papers WHERE paper_id=? AND pdf_downloaded=1",
            (paper_id,),
        ).fetchone()
        pdf_path_str = pp["pdf_path"] if pp else None
        if not pdf_path_str:
            return json.dumps({"success": False, "error": f"论文尚未下载 PDF: {paper_id}"}, ensure_ascii=False)
        ppath = Path(pdf_path_str)
        if not ppath.exists():
            return json.dumps({"success": False, "error": f"PDF 文件不存在: {pdf_path_str}"}, ensure_ascii=False)
        pdf_tasks.append((ppath, paper_id))
    elif pdf_path:
        ppath = Path(pdf_path).expanduser().resolve()
        if not ppath.exists():
            return json.dumps({"success": False, "error": f"文件不存在: {pdf_path}"}, ensure_ascii=False)
        pdf_tasks.append((ppath, ppath.stem))
    else:
        return json.dumps({"success": False, "error": "请指定 --paper-id, --pdf-path 或 --project-id --all"}, ensure_ascii=False)

    if not pdf_tasks:
        return json.dumps({"success": True, "converted": 0, "message": "没有需要转换的 PDF"}, ensure_ascii=False)

    results = []
    for ppath, pid in pdf_tasks:
        t0 = time.time()
        md_path = await converter.convert(ppath, out_dir)
        elapsed = time.time() - t0
        r = {
            "paper_id": pid,
            "pdf_path": str(ppath),
            "markdown_path": str(md_path) if md_path else None,
            "success": md_path is not None,
            "elapsed_seconds": round(elapsed, 2),
        }
        if r["success"] and pid:
            db.update_paper_meta(pid, markdown_path=str(md_path))
        results.append(r)

    success = sum(1 for r in results if r["success"])
    return json.dumps({
        "success": True, "converted": success, "failed": len(results) - success, "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def index_paper(
    paper_id: Annotated[Optional[str], Field(description="论文唯一 ID")] = None,
    project_id: Annotated[Optional[str], Field(description="项目 ID（与 --all 配合批量索引）")] = None,
    all: Annotated[bool, Field(description="批量索引项目下所有已转换论文")] = False,
    index_type: Annotated[str, Field(description="索引类型: abstract(摘要), fulltext(全文分块), both(两者)")] = "both",
) -> str:
    """论文索引入库 — 摘要 + 全文分块 → ChromaDB 双 Collection。

    索引到 papers_abstract (快速筛选) 和 papers_fulltext (深度检索) 两个 Collection。
    """
    import time
    from pathlib import Path
    from ..agent.chroma_store import ChromaStoreV2
    from ..agent.chunker import SectionChunker

    db = _get_db()
    store = ChromaStoreV2()

    paper_ids = []
    if paper_id:
        paper_ids = [paper_id]
    elif project_id and all:
        rows = db.conn.execute(
            """SELECT p.id FROM papers p
               JOIN project_papers pp ON p.id = pp.paper_id
               WHERE pp.project_id = ? AND p.markdown_path IS NOT NULL""",
            (project_id,),
        ).fetchall()
        paper_ids = [dict(r)["id"] for r in rows]
    else:
        return json.dumps({"success": False, "error": "请指定 --paper-id 或 --project-id --all"}, ensure_ascii=False)

    if not paper_ids:
        return json.dumps({"success": True, "indexed": 0, "message": "没有需要索引的论文"}, ensure_ascii=False)

    t0 = time.time()
    results = []
    for pid in paper_ids:
        row = db.conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
        if row is None:
            results.append({"paper_id": pid, "success": False, "error": "论文不存在"})
            continue
        rd = dict(row)
        r = {"paper_id": pid, "title": rd.get("title", "")}

        if index_type in ("abstract", "both"):
            title = rd.get("title", "")
            abstract = rd.get("abstract", "") or ""
            if title or abstract:
                store.add_paper_abstract(
                    paper_id=pid, title=title, abstract=abstract,
                    metadata={"year": rd.get("year"), "source": rd.get("source"), "venue": rd.get("venue")},
                )
                r["abstract_indexed"] = True
            else:
                r["abstract_indexed"] = False

        if index_type in ("fulltext", "both"):
            md_path = rd.get("markdown_path")
            if md_path and Path(md_path).exists():
                md_text = Path(md_path).read_text(encoding="utf-8")
                chunks = SectionChunker().chunk(md_text, pid)
                if chunks:
                    count = store.add_fulltext_chunks(chunks)
                    r["fulltext_chunks"] = count
                    r["fulltext_indexed"] = True
                else:
                    r["fulltext_chunks"] = 0
                    r["fulltext_indexed"] = False
            else:
                r["fulltext_indexed"] = False

        r["success"] = r.get("abstract_indexed") or r.get("fulltext_indexed")
        if r["success"]:
            db.update_paper_meta(pid, embedding_id=f"chroma:{pid}")
        results.append(r)

    success = sum(1 for r in results if r["success"])
    elapsed = time.time() - t0
    return json.dumps({
        "success": True, "indexed": success, "total": len(results),
        "elapsed_seconds": round(elapsed, 2), "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def evaluate_papers(
    project_id: Annotated[str, Field(description="项目 ID")],
    query: Annotated[str, Field(description="原始搜索意图（用于判断相关性）")] = "",
    paper_ids: Annotated[Optional[str], Field(description="要评估的 paper_id 列表（逗号分隔）")] = None,
    all: Annotated[bool, Field(description="评估项目下所有未评估的论文")] = False,
    max_concurrent: Annotated[int, Field(description="最大并发 LLM 调用数")] = 5,
) -> str:
    """LLM 批量评估论文相关性 — 使用 LLM 对每篇论文打分 (0-1)。

    更新 DB 中的 relevance_score 和 relevance_reason。
    """
    import asyncio
    import time

    db = _get_db()
    project = db.get_project(project_id)
    if project is None:
        return json.dumps({"success": False, "error": f"项目不存在: {project_id}"}, ensure_ascii=False)

    user_query = query or project.get("user_query", "")

    if paper_ids:
        pids = [p.strip() for p in paper_ids.split(",")]
        papers = []
        for pid in pids:
            row = db.conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
            if row:
                papers.append(dict(row))
    else:
        papers = db.get_project_papers(project_id)
        if all:
            papers = [p for p in papers if p.get("relevance_score", 0.5) == 0.5 and p.get("relevance_reason", "") == ""]

    if not papers:
        return json.dumps({"success": True, "evaluated": 0, "message": "没有需要评估的论文"}, ensure_ascii=False)

    from ..agent.llm_client import LLMClient
    llm = LLMClient()
    sem = asyncio.Semaphore(max_concurrent)
    t0 = time.time()

    async def evaluate_one(p):
        async with sem:
            try:
                # Evaluate using title+abstract
                title = p.get("title", "")
                abstract = p.get("abstract", "") or ""
                result = await llm._chat_json(
                    llm.RELEVANCE_SYSTEM_PROMPT,
                    f"用户研究需求: {user_query}\n\n论文标题: {title}\n摘要: {abstract[:500]}\n",
                )
                score = float(result.get("score", 0.5))
                reason = result.get("reason", "")
                return {
                    "paper_id": p["id"], "title": title[:80], "score": score,
                    "reason": reason, "is_relevant": score >= 0.5, "success": True,
                }
            except Exception as e:
                return {
                    "paper_id": p["id"], "title": p.get("title", "")[:80],
                    "score": 0.5, "reason": f"评估失败: {e}", "is_relevant": True,
                    "success": False, "error": str(e),
                }

    tasks = [evaluate_one(p) for p in papers]
    results = await asyncio.gather(*tasks)

    for r in results:
        db.link_paper_to_project(project_id, r["paper_id"],
                                 relevance_score=r["score"],
                                 relevance_reason=(r.get("reason") or "")[:500])

    elapsed = time.time() - t0
    relevant = sum(1 for r in results if r.get("is_relevant"))
    return json.dumps({
        "success": True, "project_id": project_id, "evaluated": len(results),
        "relevant": relevant, "irrelevant": len(results) - relevant,
        "elapsed_seconds": round(elapsed, 2), "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def rank_papers(
    project_id: Annotated[Optional[str], Field(description="项目 ID")] = None,
    paper_id: Annotated[Optional[str], Field(description="单篇论文 ID")] = None,
    all: Annotated[bool, Field(description="评定项目下所有论文")] = False,
) -> str:
    """期刊等级评定 — CCF / SCI 分级，统一为 A+ / A / B / C。

    将等级缓存到 journal_ranks 表并更新论文的 unified_level。
    """
    db = _get_db()
    from ..agent.journal_ranker import JournalRanker
    ranker = JournalRanker()

    papers = []
    if paper_id:
        row = db.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return json.dumps({"success": False, "error": f"论文不存在: {paper_id}"}, ensure_ascii=False)
        papers = [dict(row)]
    elif project_id and all:
        papers = db.get_project_papers(project_id)
    else:
        return json.dumps({"success": False, "error": "请指定 --paper-id 或 --project-id --all"}, ensure_ascii=False)

    results = []
    for p in papers:
        venue = p.get("venue", "")
        if not venue:
            results.append({"paper_id": p["id"], "title": p.get("title", "")[:60], "venue": None, "level": None})
            continue
        level = ranker.rank(venue)
        if level:
            db.upsert_journal_rank(venue, unified=level)
            db.update_paper_meta(p["id"], unified_level=level)
        results.append({
            "paper_id": p["id"], "title": p.get("title", "")[:60],
            "venue": venue, "level": level,
        })

    levels = {"A+": 0, "A": 0, "B": 0, "C": 0, None: 0}
    for r in results:
        lvl = r["level"]
        levels[lvl] = levels.get(lvl, 0) + 1

    return json.dumps({
        "success": True, "total": len(results),
        "distribution": {k: v for k, v in levels.items() if v > 0},
        "results": results,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def generate_survey(
    project_id: Annotated[str, Field(description="项目 ID")],
    output: Annotated[Optional[str], Field(description="输出文件路径")] = None,
) -> str:
    """生成 AI 文献综述报告 — 使用 LLM 为项目生成结构化综述 Markdown。

    包含搜索概况、关键论文、研究方向分类、建议等。
    """
    from pathlib import Path

    db = _get_db()
    project = db.get_project(project_id)
    if project is None:
        return json.dumps({"success": False, "error": f"项目不存在: {project_id}"}, ensure_ascii=False)

    user_query = project.get("user_query", "")
    papers = db.get_project_papers(project_id, relevant_only=True)
    if not papers:
        papers = db.get_project_papers(project_id)

    if not papers:
        return json.dumps({"success": False, "error": "项目下没有论文"}, ensure_ascii=False)

    from ..agent.llm_client import LLMClient
    llm = LLMClient()

    # 构建论文摘要列表
    paper_items = []
    for p in papers[:50]:
        paper_items.append(
            f"- [{p.get('relevance_score', 0.5):.2f}] {p['title']} "
            f"({p.get('year', '?')}) | {p.get('source', '')} | {p.get('venue', '')}"
        )

    report = await llm.generate_report(user_query, papers, judgments=[])

    # 保存报告
    out_path = Path(output) if output else get_outputs_dir(project_id) / "survey.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    # 更新项目
    db.update_project(project_id, report_path=str(out_path), status="completed")

    return json.dumps({
        "success": True, "project_id": project_id,
        "papers_included": min(len(papers), 50),
        "report_path": str(out_path),
        "report_preview": report[:1000] + "..." if len(report) > 1000 else report,
    }, ensure_ascii=False, indent=2)


# ── 直接运行入口 ──────────────────────────────────────────


def main():
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
