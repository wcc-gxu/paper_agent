"""SQLite 持久化 — 搜索项目、论文库、日志."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import get_db_path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = get_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_query TEXT NOT NULL,
    parsed_intent TEXT,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    total_papers_found INTEGER DEFAULT 0,
    total_relevant INTEGER DEFAULT 0,
    total_downloaded INTEGER DEFAULT 0,
    report_path TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT,
    year INTEGER,
    abstract TEXT,
    doi TEXT UNIQUE,
    arxiv_id TEXT,
    pmid TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    pdf_url TEXT,
    citation_count INTEGER,
    venue TEXT,
    keywords TEXT,
    embedding_id TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_papers (
    project_id TEXT NOT NULL REFERENCES projects(id),
    paper_id TEXT NOT NULL REFERENCES papers(id),
    search_round INTEGER DEFAULT 1,
    relevance_score REAL DEFAULT 0.5,
    relevance_reason TEXT DEFAULT '',
    pdf_downloaded INTEGER DEFAULT 0,
    pdf_path TEXT,
    PRIMARY KEY (project_id, paper_id)
);

CREATE TABLE IF NOT EXISTS search_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    round INTEGER DEFAULT 1,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    results_count INTEGER DEFAULT 0,
    error TEXT,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_ranks (
    venue_name TEXT PRIMARY KEY,
    ccf_level TEXT,
    sci_zone TEXT,
    unified_level TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_paper_id TEXT NOT NULL,
    target_paper_id TEXT,
    target_title TEXT NOT NULL,
    target_doi TEXT,
    target_year INTEGER,
    relation_type TEXT NOT NULL DEFAULT 'references',
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_paper_id) REFERENCES papers(id)
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    user_query TEXT NOT NULL,
    plan_json TEXT,
    plan_markdown TEXT,
    status TEXT DEFAULT 'pending',
    current_step INTEGER DEFAULT 0,
    total_steps INTEGER DEFAULT 0,
    max_steps INTEGER DEFAULT 50,
    total_tokens_used INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES agent_tasks(id),
    step_index INTEGER NOT NULL,
    step_name TEXT,
    action TEXT NOT NULL,
    tool_name TEXT,
    tool_args TEXT,
    result_summary TEXT,
    status TEXT DEFAULT 'pending',
    metrics TEXT DEFAULT '{}',
    llm_assessment TEXT,
    retry_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(task_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);
CREATE INDEX IF NOT EXISTS idx_project_papers_project ON project_papers(project_id);
CREATE INDEX IF NOT EXISTS idx_search_logs_project ON search_logs(project_id);
CREATE INDEX IF NOT EXISTS idx_citations_source ON citations(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_target ON citations(target_paper_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);

CREATE TABLE IF NOT EXISTS sessions (
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT DEFAULT '新对话',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, session_id)
);

CREATE TABLE IF NOT EXISTS ws_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER DEFAULT 0,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    subtype TEXT DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    priority INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    is_replay INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ws_messages_lookup ON ws_messages(agent_id, session_id, seq);
"""

# 运行时迁移：为旧 papers 表添加新列
_MIGRATIONS = [
    "ALTER TABLE papers ADD COLUMN unified_level TEXT",
    "ALTER TABLE papers ADD COLUMN reading_level TEXT",
    "ALTER TABLE papers ADD COLUMN digest TEXT",
    "ALTER TABLE papers ADD COLUMN method_tags TEXT",
    "ALTER TABLE papers ADD COLUMN dataset_info TEXT",
    "ALTER TABLE papers ADD COLUMN code_url TEXT",
    "ALTER TABLE papers ADD COLUMN markdown_path TEXT",
    "ALTER TABLE papers ADD COLUMN pdf_path TEXT",
    "CREATE INDEX IF NOT EXISTS idx_papers_unified_level ON papers(unified_level)",
]


class AgentDB:
    """Agent 数据持久层。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            self._run_migrations()
            self._conn.commit()
        return self._conn

    def _run_migrations(self):
        """运行缺失列的迁移和索引创建。"""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(papers)")}
        for sql in _MIGRATIONS:
            if "ADD COLUMN" in sql:
                col = sql.split("ADD COLUMN ")[1].split(" ")[0]
                if col not in existing:
                    try:
                        self._conn.execute(sql)
                        logger.info(f"DB migration: {sql}")
                    except Exception as e:
                        logger.debug(f"Migration skipped: {e}")
            else:
                # 其他迁移语句（如 CREATE INDEX）——直接尝试执行
                try:
                    self._conn.execute(sql)
                    logger.info(f"DB migration (ddl): {sql}")
                except Exception as e:
                    logger.debug(f"Migration skipped: {e}")

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _paper_id(self, paper) -> str:
        """生成论文唯一 ID: DOI > arxiv_id > pmid > 标题 SHA256。"""
        import hashlib
        if paper.doi:
            return f"doi:{paper.doi.lower()}"
        if paper.arxiv_id:
            return f"arxiv:{paper.arxiv_id}"
        if paper.pmid:
            return f"pmid:{paper.pmid}"
        return f"sha256:{hashlib.sha256(paper.title.encode()).hexdigest()[:16]}"

    # ── Project CRUD ─────────────────────────────────────

    def create_project(self, user_query: str, parsed_intent: dict = None, project_id: str = None) -> str:
        pid = project_id or str(uuid.uuid4())[:8]
        self.conn.execute(
            "INSERT INTO projects (id, user_query, parsed_intent, created_at) VALUES (?,?,?,?)",
            (pid, user_query, json.dumps(parsed_intent, ensure_ascii=False) if parsed_intent else None, self._now()),
        )
        self.conn.commit()
        return pid

    def update_project(self, project_id: str, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [project_id]
        self.conn.execute(f"UPDATE projects SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def get_project(self, project_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, user_query, status, total_papers_found, total_relevant, total_downloaded, created_at "
            "FROM projects ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Paper CRUD ───────────────────────────────────────

    def upsert_paper(self, paper) -> str:
        """插入或更新论文，返回 paper_id。"""
        pid = self._paper_id(paper)
        now = self._now()
        authors_json = json.dumps(paper.authors, ensure_ascii=False) if paper.authors else "[]"
        keywords_json = json.dumps(paper.keywords, ensure_ascii=False) if paper.keywords else "[]"
        source_str = paper.source.value if hasattr(paper.source, 'value') else str(paper.source)

        self.conn.execute(
            """INSERT INTO papers (id, title, authors, year, abstract, doi, arxiv_id, pmid,
               source, source_url, pdf_url, citation_count, venue, keywords, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
               updated_at=excluded.updated_at,
               citation_count=COALESCE(excluded.citation_count, papers.citation_count),
               pdf_url=COALESCE(excluded.pdf_url, papers.pdf_url)""",
            (
                pid, paper.title, authors_json, paper.year, paper.abstract,
                paper.doi, paper.arxiv_id, paper.pmid,
                source_str, paper.source_url, paper.pdf_url, paper.citation_count,
                paper.venue, keywords_json, now, now,
            ),
        )
        self.conn.commit()
        return pid

    def update_paper_meta(self, paper_id: str, **kwargs):
        """更新论文的增强元数据字段。"""
        if not kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        self.conn.execute(
            f"UPDATE papers SET {sets}, updated_at=? WHERE id=?",
            vals + [self._now(), paper_id],
        )
        self.conn.commit()

    # ── 期刊分级缓存 ─────────────────────────────────────

    def upsert_journal_rank(self, venue: str, ccf: str = None, sci: str = None, unified: str = None):
        self.conn.execute(
            "INSERT OR REPLACE INTO journal_ranks (venue_name, ccf_level, sci_zone, unified_level, updated_at) "
            "VALUES (?,?,?,?,?)",
            (venue, ccf, sci, unified, self._now()),
        )
        self.conn.commit()

    def get_journal_rank(self, venue: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM journal_ranks WHERE venue_name=?", (venue,)).fetchone()
        return dict(row) if row else None

    def link_paper_to_project(
        self, project_id: str, paper_id: str, round_num: int = 1,
        relevance_score: float = 0.5, relevance_reason: str = "",
    ):
        self.conn.execute(
            """INSERT OR REPLACE INTO project_papers
               (project_id, paper_id, search_round, relevance_score, relevance_reason)
               VALUES (?,?,?,?,?)""",
            (project_id, paper_id, round_num, relevance_score, relevance_reason),
        )
        self.conn.commit()

    def mark_pdf_downloaded(self, project_id: str, paper_id: str, pdf_path: str):
        self.conn.execute(
            "UPDATE project_papers SET pdf_downloaded=1, pdf_path=? WHERE project_id=? AND paper_id=?",
            (pdf_path, project_id, paper_id),
        )
        self.conn.commit()

    def get_project_papers(self, project_id: str, relevant_only: bool = False) -> list[dict]:
        query = """
            SELECT p.*, pp.search_round, pp.relevance_score, pp.relevance_reason,
                   pp.pdf_downloaded, pp.pdf_path
            FROM papers p
            JOIN project_papers pp ON p.id = pp.paper_id
            WHERE pp.project_id = ?
        """
        if relevant_only:
            query += " AND pp.relevance_score >= 0.5"
        query += " ORDER BY pp.relevance_score DESC"
        rows = self.conn.execute(query, (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_relevant_papers(self, project_id: str) -> list[dict]:
        return self.get_project_papers(project_id, relevant_only=True)

    # ── 搜索日志 ─────────────────────────────────────────

    def log_search(
        self, project_id: str, round_num: int, source: str, query: str,
        results_count: int, duration_ms: int = 0, error: str = None,
    ):
        self.conn.execute(
            "INSERT INTO search_logs (project_id, round, source, query, results_count, error, duration_ms, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (project_id, round_num, source, query, results_count, error, duration_ms, self._now()),
        )
        self.conn.commit()

    # ── 引用关系管理 ─────────────────────────────────────

    def add_citation(self, source_paper_id: str, target_title: str,
                     target_paper_id: str = None, target_doi: str = None,
                     target_year: int = None, relation_type: str = "references",
                     confidence: float = 1.0):
        """添加引用关系."""
        self.conn.execute(
            """INSERT OR IGNORE INTO citations
               (source_paper_id, target_paper_id, target_title, target_doi,
                target_year, relation_type, confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (source_paper_id, target_paper_id, target_title, target_doi,
             target_year, relation_type, confidence, self._now()),
        )
        self.conn.commit()

    def get_citations(self, paper_id: str, relation_type: str = None,
                      direction: str = "outgoing") -> list[dict]:
        """获取论文的引用关系.

        Args:
            paper_id: 论文 ID
            relation_type: 过滤关系类型
            direction: "outgoing" (这篇引用了谁) | "incoming" (谁引用了这篇)
        """
        if direction == "outgoing":
            where = "source_paper_id = ?"
            params = [paper_id]
        else:
            where = "target_paper_id = ?"
            params = [paper_id]

        if relation_type:
            where += " AND relation_type = ?"
            params.append(relation_type)

        rows = self.conn.execute(
            f"SELECT * FROM citations WHERE {where} ORDER BY target_year DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_citation_count(self, paper_id: str) -> dict:
        """获取论文的引用统计."""
        outgoing = self.conn.execute(
            "SELECT COUNT(*) FROM citations WHERE source_paper_id = ?", (paper_id,)
        ).fetchone()[0]
        incoming = self.conn.execute(
            "SELECT COUNT(*) FROM citations WHERE target_paper_id = ?", (paper_id,)
        ).fetchone()[0]
        return {"outgoing": outgoing, "incoming": incoming}

    # ── Agent 任务管理 ────────────────────────────────────

    def create_agent_task(self, task_id: str, user_query: str,
                          session_id: str = None, max_steps: int = 50) -> str:
        """创建 Agent 任务."""
        now = self._now()
        self.conn.execute(
            """INSERT INTO agent_tasks
               (id, session_id, user_query, status, max_steps, created_at, updated_at)
               VALUES (?,?,?,'pending',?,?,?)""",
            (task_id, session_id, user_query, max_steps, now, now),
        )
        self.conn.commit()
        return task_id

    def update_agent_task(self, task_id: str, **kwargs):
        """更新 Agent 任务状态."""
        kwargs["updated_at"] = self._now()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        self.conn.execute(f"UPDATE agent_tasks SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def get_agent_task(self, task_id: str) -> Optional[dict]:
        """获取 Agent 任务."""
        row = self.conn.execute(
            "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_agent_tasks(self, session_id: str = None, limit: int = 20) -> list[dict]:
        """列出 Agent 任务."""
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 任务步骤管理 ──────────────────────────────────────

    def add_task_step(self, task_id: str, step_index: int, step_name: str,
                      action: str, tool_name: str = None, tool_args: dict = None) -> int:
        """添加任务步骤."""
        now = self._now()
        self.conn.execute(
            """INSERT OR REPLACE INTO task_steps
               (task_id, step_index, step_name, action, tool_name, tool_args, status, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task_id, step_index, step_name, action, tool_name,
             json.dumps(tool_args, ensure_ascii=False) if tool_args else None,
             "in_progress", now),
        )
        self.conn.commit()
        return step_index

    def update_task_step(self, task_id: str, step_index: int, **kwargs):
        """更新任务步骤."""
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        if "completed_at" not in kwargs and kwargs.get("status") in ("done", "failed", "skipped"):
            kwargs["completed_at"] = self._now()
            sets += ", completed_at=?"
            vals.append(kwargs["completed_at"])
        self.conn.execute(
            f"UPDATE task_steps SET {sets} WHERE task_id = ? AND step_index = ?",
            vals + [task_id, step_index],
        )
        self.conn.commit()

    def get_task_steps(self, task_id: str) -> list[dict]:
        """获取任务的所有步骤."""
        rows = self.conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task_step(self, task_id: str, step_index: int) -> Optional[dict]:
        """获取指定步骤."""
        row = self.conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND step_index = ?",
            (task_id, step_index),
        ).fetchone()
        return dict(row) if row else None

    # ── Session CRUD ───────────────────────────────────────

    def create_session(self, agent_id: str, session_id: str, title: str = "新对话") -> str:
        """创建新会话。幂等（INSERT OR IGNORE）."""
        now = self._now()
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (agent_id, session_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, session_id, title, now, now),
        )
        self.conn.commit()
        return session_id

    def get_session(self, agent_id: str, session_id: str) -> Optional[dict]:
        """获取会话详情."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE agent_id=? AND session_id=?",
            (agent_id, session_id),
        ).fetchone()
        return dict(row) if row else None

    def update_session_title(self, agent_id: str, session_id: str, title: str):
        """更新会话标题."""
        self.conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE agent_id=? AND session_id=?",
            (title, self._now(), agent_id, session_id),
        )
        self.conn.commit()

    def list_sessions(self, agent_id: str) -> list[dict]:
        """列出某 Agent 的所有会话."""
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE agent_id=? ORDER BY updated_at DESC",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── WS 消息持久化 ──────────────────────────────────────

    def save_ws_message(self, agent_id: str, session_id: str, seq: int,
                        role: str, type_: str, subtype: str = "",
                        payload: dict | str = None, priority: int = 0) -> int:
        """保存 WS 消息到持久化表."""
        import json as _json
        if payload is None:
            payload = {}
        payload_str = _json.dumps(payload, ensure_ascii=False, default=str) if not isinstance(payload, str) else payload
        cursor = self.conn.execute(
            """INSERT INTO ws_messages (agent_id, session_id, seq, role, type, subtype, payload, priority, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (agent_id, session_id, seq, role, type_, subtype, payload_str, priority, self._now()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_ws_messages_for_replay(self, agent_id: str, session_id: str,
                                    since_seq: int = 0, limit: int = 200) -> list[dict]:
        """获取重连回放消息。排除 priority=0 流式消息."""
        rows = self.conn.execute(
            """SELECT * FROM ws_messages
               WHERE agent_id=? AND session_id=? AND seq > ? AND priority > 0
               ORDER BY seq ASC LIMIT ?""",
            (agent_id, session_id, since_seq, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_user_seq(self, agent_id: str, session_id: str) -> int:
        """获取指定 session 中最后一条用户消息的 seq."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM ws_messages "
            "WHERE agent_id=? AND session_id=? AND role='user'",
            (agent_id, session_id),
        ).fetchone()
        return row[0] if row else 0

    def get_history_count(self, agent_id: str, session_id: str) -> int:
        """获取指定 session 的历史消息数."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM ws_messages WHERE agent_id=? AND session_id=?",
            (agent_id, session_id),
        ).fetchone()
        return row[0] if row else 0

    def get_active_tasks(self, agent_id: str, session_id: str) -> list[dict]:
        """获取指定 session 的活跃任务."""
        rows = self.conn.execute(
            "SELECT id, status, current_step, total_steps FROM agent_tasks "
            "WHERE session_id=? AND status IN ('pending','running','paused') "
            "ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
