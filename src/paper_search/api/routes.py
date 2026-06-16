"""REST API 路由 — Paper Agent 全部端点."""

from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import get_papers_dir, get_markdown_dir, get_outputs_dir

router = APIRouter(prefix="/api", tags=["paper-agent"])


# ═══════════════════════════════════════════════════════════════
# Request/Response Models
# ═══════════════════════════════════════════════════════════════


class SearchRequest(BaseModel):
    keywords: str = ""
    sources: str = "arxiv,semantic_scholar"
    title: Optional[str] = None
    author: Optional[str] = None
    doi: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    max_results: int = 20
    project_id: Optional[str] = None


class KnowledgeQuestion(BaseModel):
    question: str
    top_k: int = 5
    use_fulltext: bool = True
    project_id: Optional[str] = None


class PlanConfirmRequest(BaseModel):
    task_id: str
    confirmed: bool = True
    modifications: Optional[dict] = None


class SubscriptionRequest(BaseModel):
    name: str
    keywords: str
    sources: list[str] = ["arxiv", "semantic_scholar"]
    interval_hours: int = 24


# ═══════════════════════════════════════════════════════════════
# Dependencies
# ═══════════════════════════════════════════════════════════════

def _get_db():
    from .app import get_db
    return get_db()


def _get_engine():
    from .app import get_engine
    return get_engine()


def _get_llm():
    from .app import get_llm
    return get_llm()


def _get_kb():
    from .app import get_kb
    return get_kb()


# ═══════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}


@router.get("/sources")
async def list_sources():
    """列出所有可用搜索来源及其状态."""
    engine = _get_engine()
    health = await engine.health_check()
    from ..providers import list_providers as get_all

    desc = {
        "arxiv": "arXiv 预印本",
        "semantic_scholar": "Semantic Scholar",
        "pubmed": "PubMed",
        "cnki": "CNKI 中国知网",
        "ieee": "IEEE Xplore",
        "sciencedirect": "ScienceDirect",
    }
    sources = []
    for st in get_all():
        sources.append({
            "name": st.value,
            "description": desc.get(st.value, ""),
            "available": health.get(st.value, False),
        })
    return {"total": len(sources), "sources": sources}


# ═══════════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════════

@router.post("/search")
async def search_papers(req: SearchRequest):
    """跨多源搜索学术论文."""
    from ..models import SearchQuery, SourceType

    source_list = [SourceType(s.strip().lower()) for s in req.sources.split(",") if s.strip()]
    if not source_list:
        source_list = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

    query = SearchQuery(
        keywords=req.keywords, title=req.title, author=req.author, doi=req.doi,
        year_from=req.year_from, year_to=req.year_to,
        max_results=req.max_results, sources=source_list,
    )

    engine = _get_engine()
    result = await engine.search(query)

    db = _get_db()
    pid = req.project_id or db.create_project(user_query=query.effective_query())
    paper_ids = []
    for p in result.papers:
        paper_id = db.upsert_paper(p)
        db.link_paper_to_project(pid, paper_id)
        paper_ids.append(paper_id)

    return {
        "success": True,
        "project_id": pid,
        "total_found": result.total_found,
        "sources_searched": [s.value for s in source_list],
        "errors": result.errors,
        "paper_ids": paper_ids,
        "papers": [
            {
                "title": p.title,
                "authors": p.authors[:10],
                "year": p.year,
                "abstract": (p.abstract or "")[:500],
                "doi": p.doi,
                "arxiv_id": p.arxiv_id,
                "source": p.source.value,
                "citation_count": p.citation_count,
                "venue": p.venue,
            }
            for p in result.papers
        ],
    }


# ═══════════════════════════════════════════════════════════════
# Papers
# ═══════════════════════════════════════════════════════════════

@router.get("/papers")
async def list_papers(
    project_id: Optional[str] = None,
    relevant_only: bool = False,
    limit: int = 50,
):
    """列出论文."""
    db = _get_db()
    if project_id:
        papers = db.get_project_papers(project_id, relevant_only=relevant_only)
    else:
        rows = db.conn.execute(
            "SELECT * FROM papers ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        papers = [dict(r) for r in rows]
    return {"total": len(papers), "papers": papers}


@router.get("/papers/{paper_id}")
async def get_paper(paper_id: str):
    """获取单篇论文详情."""
    db = _get_db()
    row = db.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Paper not found: {paper_id}")
    return dict(row)


@router.post("/papers/upload")
async def upload_paper(file: UploadFile = File(...), project_id: Optional[str] = None):
    """上传本地 PDF 论文."""
    if not file.filename or not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    upload_dir = get_papers_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 保存文件
    safe_name = file.filename.replace(" ", "_")
    pdf_path = upload_dir / safe_name
    content = await file.read()
    pdf_path.write_bytes(content)

    # 转换为 Markdown
    from ..agent.pdf_converter import PDFConverter
    converter = PDFConverter(max_concurrent=1)
    md_dir = get_markdown_dir()
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = await converter.convert(pdf_path, md_dir)

    db = _get_db()
    # 创建基本的 paper 记录
    paper_id = f"sha256:{uuid.uuid4().hex[:16]}"
    db.conn.execute(
        """INSERT OR REPLACE INTO papers (id, title, source, first_seen_at, updated_at, pdf_path, markdown_path)
           VALUES (?,?,?,datetime('now'),datetime('now'),?,?)""",
        (paper_id, file.filename.replace(".pdf", ""), "upload", str(pdf_path), str(md_path) if md_path else None),
    )
    db.conn.commit()

    if project_id:
        db.link_paper_to_project(project_id, paper_id)

    return {
        "success": True,
        "paper_id": paper_id,
        "title": file.filename,
        "pdf_path": str(pdf_path),
        "markdown_path": str(md_path) if md_path else None,
    }


# ═══════════════════════════════════════════════════════════════
# Knowledge Base
# ═══════════════════════════════════════════════════════════════

@router.post("/knowledge/ask")
async def knowledge_ask(req: KnowledgeQuestion):
    """知识库 RAG 问答."""
    kb = _get_kb()
    result = await kb.ask(
        question=req.question,
        top_k=req.top_k,
        use_fulltext=req.use_fulltext,
        project_id=req.project_id,
    )
    return {
        "question": result.question,
        "answer": result.answer,
        "confidence": result.confidence,
        "sources": result.sources,
        "follow_up_questions": result.follow_up_questions,
    }


@router.get("/knowledge/search")
async def knowledge_search(
    q: str = Query(..., description="搜索查询"),
    top_k: int = 5,
    project_id: Optional[str] = None,
):
    """知识库语义搜索."""
    kb = _get_kb()
    result = await kb.ask(question=q, top_k=top_k, project_id=project_id)
    return {"question": q, "sources": result.sources}


@router.post("/knowledge/extract/{paper_id}")
async def knowledge_extract(paper_id: str, deep: bool = False):
    """提取论文结构化知识."""
    kb = _get_kb()
    result = await kb.extract_knowledge(paper_id, deep=deep)
    return result


@router.get("/knowledge/discover")
async def knowledge_discover(
    domain: str = "",
    project_id: Optional[str] = None,
):
    """知识发现 — 研究空白、矛盾、趋势."""
    kb = _get_kb()
    result = await kb.discover_gaps(domain=domain, project_id=project_id)
    return result


@router.get("/knowledge/related/{paper_id}")
async def knowledge_related(paper_id: str, top_k: int = 10):
    """发现相关论文."""
    kb = _get_kb()
    result = await kb.find_related(paper_id, top_k=top_k)
    return {"paper_id": paper_id, "related": result}


# ═══════════════════════════════════════════════════════════════
# Agent Tasks
# ═══════════════════════════════════════════════════════════════

@router.post("/tasks")
async def create_task(query: str = Query(..., description="研究需求")):
    """创建 Agent 任务。返回 task_id，可通过 WS 或 REST 触发执行。"""
    import uuid as _uuid
    db = _get_db()
    task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{_uuid.uuid4().hex[:4]}"
    db.create_agent_task(task_id=task_id, user_query=query)
    return {"task_id": task_id, "status": "pending", "query": query}


@router.post("/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, req: PlanConfirmRequest):
    """确认任务方案 — 更新 plan 状态，触发执行。"""
    db = _get_db()
    task = db.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    if req.confirmed:
        db.update_agent_task(task_id, status="running",
                            plan_json=_json.dumps(req.modifications) if req.modifications else None)
        return {"task_id": task_id, "status": "running"}
    db.update_agent_task(task_id, status="cancelled")
    return {"task_id": task_id, "status": "cancelled", "reason": "user rejected"}


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停任务 — 当前阶段完成后暂停。"""
    db = _get_db()
    task = db.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    db.update_agent_task(task_id, status="paused")
    return {"task_id": task_id, "status": "paused"}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复已暂停的任务。"""
    db = _get_db()
    task = db.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    db.update_agent_task(task_id, status="running")
    return {"task_id": task_id, "status": "running"}


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    """取消任务。"""
    db = _get_db()
    task = db.get_agent_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    db.update_agent_task(task_id, status="cancelled")
    return {"task_id": task_id, "status": "cancelled"}


# ═══════════════════════════════════════════════════════════════
# IngestAgent — 论文入库
# ═══════════════════════════════════════════════════════════════


class IngestRequest(BaseModel):
    user_query: str
    sources: list[str] = ["arxiv", "semantic_scholar"]
    year_from: int = 2022
    max_results: int = 20
    project_id: Optional[str] = None


@router.post("/ingest/start")
async def start_ingest(req: IngestRequest):
    """触发 IngestAgent.ExecuteGraph() → 后台执行，返回 task_id 用于查询进度。"""
    import uuid as _uuid
    db = _get_db()

    pid = req.project_id or db.create_project(user_query=req.user_query)
    task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{_uuid.uuid4().hex[:4]}"

    db.create_agent_task(task_id=task_id, user_query=req.user_query, session_id=pid)

    # 异步启动 IngestAgent（不阻塞响应）
    import asyncio
    asyncio.create_task(_run_ingest(task_id, pid, req.user_query, req.sources, req.year_from, req.max_results))

    return {
        "task_id": task_id,
        "project_id": pid,
        "status": "started",
        "query": req.user_query,
    }


async def _run_ingest(task_id: str, project_id: str, user_query: str,
                       sources: list[str], year_from: int, max_results: int):
    """后台执行 IngestAgent。"""
    try:
        from ..agent.sub_agent import PipelineRunner
        from ..agent.graphs.ingest_graph import IngestAgent
        from ..agent.pdf_converter import PDFConverter
        from ..agent.journal_ranker import JournalRanker
        from ..agent.chroma_store import ChromaStoreV2
        from ..engine import PaperSearchEngine
        from ..config import Config
        from .app import get_db, get_llm

        db = get_db()
        llm = get_llm()
        engine = PaperSearchEngine(Config())

        runner = PipelineRunner(
            engine=engine, db=db, llm=llm,
            chroma=ChromaStoreV2(),
            converter=PDFConverter(max_concurrent=2),
            ranker=JournalRanker(),
        )
        ingest = IngestAgent(runner)
        graph = ingest.compile()

        config = {"configurable": {"thread_id": f"ingest-{task_id}"}}
        result = await graph.ainvoke(
            {
                "project_id": project_id,
                "user_query": user_query,
                "sources": sources,
                "year_from": year_from,
                "max_results": max_results,
                "is_single_tool": False,
                "single_tool_name": "",
            },
            config=config,
        )

        db.update_agent_task(task_id, status="completed",
                            plan_json=_json.dumps(result.get("result", {})))
        logger.info(f"IngestAgent task {task_id} completed")

    except Exception as e:
        logger.error(f"IngestAgent task {task_id} failed: {e}", exc_info=True)
        try:
            from .app import get_db
            get_db().update_agent_task(task_id, status="failed")
        except Exception:
            pass


@router.get("/ingest/progress/{task_id}")
async def ingest_progress(task_id: str):
    """查询入库进度（读取 task.jsonl 日志）。"""
    from pathlib import Path
    from ..agent.task_logger import TaskLogger

    log_dir = Path.home() / ".paper_search" / "logs" / "tasks"
    tlog = TaskLogger(log_dir, task_id)
    progress = tlog.get_progress()
    events = tlog.read_events()

    return {
        "task_id": task_id,
        "progress": progress,
        "event_count": len(events),
        "latest_events": events[-20:] if events else [],
    }


# ═══════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects(limit: int = 20):
    db = _get_db()
    projects = db.list_projects(limit=limit)
    return {"total": len(projects), "projects": projects}


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    db = _get_db()
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    papers = db.get_project_papers(project_id)
    return {"project": project, "papers_count": len(papers)}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, keep_pdfs: bool = True):
    db = _get_db()
    if keep_pdfs:
        db.conn.execute("DELETE FROM project_papers WHERE project_id = ?", (project_id,))
        db.conn.execute("DELETE FROM search_logs WHERE project_id = ?", (project_id,))
        db.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    else:
        # 也删除关联论文
        paper_ids = [r[0] for r in db.conn.execute(
            "SELECT paper_id FROM project_papers WHERE project_id = ?", (project_id,)
        ).fetchall()]
        db.conn.execute("DELETE FROM project_papers WHERE project_id = ?", (project_id,))
        db.conn.execute("DELETE FROM search_logs WHERE project_id = ?", (project_id,))
        db.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        for pid in paper_ids:
            db.conn.execute("DELETE FROM papers WHERE id = ?", (pid,))
    db.conn.commit()
    return {"success": True, "message": f"Project {project_id} deleted"}


@router.get("/projects/{project_id}/export")
async def export_project(project_id: str, format: str = "bibtex"):
    """导出项目论文."""
    db = _get_db()
    papers = db.get_project_papers(project_id)
    if not papers:
        raise HTTPException(status_code=404, detail=f"Project not found or has no papers: {project_id}")

    if format == "bibtex":
        entries = []
        for p in papers:
            authors = _json.loads(p.get("authors", "[]")) if isinstance(p.get("authors"), str) else (p.get("authors") or [])
            first_author = (authors[0].split()[-1] if authors else "Unknown").replace(",", "")
            key = f"{first_author}{p.get('year', '????')}{p['title'][:20].replace(' ', '').replace(':', '')}"
            author_str = " and ".join(a for a in authors[:8] if a)
            entry = (
                f"@article{{{key},\n"
                f"  title = {{{p['title']}}},\n"
                f"  author = {{{author_str}}},\n"
                f"  year = {{{p.get('year', '????')}}},\n"
                f"  journal = {{{p.get('venue', '')}}},\n"
                f"  doi = {{{p.get('doi', '')}}}\n"
                f"}}"
            )
            entries.append(entry)
        return {"format": "bibtex", "content": "\n\n".join(entries)}

    clean = [{
        "title": p.get("title"), "year": p.get("year"),
        "doi": p.get("doi"), "venue": p.get("venue"),
        "relevance_score": p.get("relevance_score"),
    } for p in papers]
    return {"format": "json", "papers": clean}


# ═══════════════════════════════════════════════════════════════
# Subscriptions
# ═══════════════════════════════════════════════════════════════

@router.get("/subscriptions")
async def list_subscriptions():
    db = _get_db()
    rows = db.conn.execute(
        "SELECT key, value FROM user_profile WHERE key LIKE 'subscription:%'"
    ).fetchall()
    subs = []
    for r in rows:
        config = _json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
        subs.append({
            "id": r["key"].replace("subscription:", ""),
            "config": config,
        })
    return {"total": len(subs), "subscriptions": subs}


@router.post("/subscriptions")
async def create_subscription(req: SubscriptionRequest):
    """创建研究方向订阅."""
    db = _get_db()
    import uuid as _uuid
    from datetime import datetime, timezone

    sub_id = str(_uuid.uuid4())[:8]
    config = {
        "name": req.name,
        "keywords": req.keywords,
        "sources": req.sources,
        "interval_hours": req.interval_hours,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_check": "",
    }
    db.conn.execute(
        "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?,?,?)",
        (f"subscription:{sub_id}", _json.dumps(config, ensure_ascii=False),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    db.conn.commit()
    return {"success": True, "subscription_id": sub_id, "config": config}


@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: str):
    db = _get_db()
    db.conn.execute(
        "DELETE FROM user_profile WHERE key = ?",
        (f"subscription:{subscription_id}",),
    )
    db.conn.commit()
    return {"success": True, "message": f"Subscription {subscription_id} deleted"}
