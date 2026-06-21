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
    is_replay INTEGER DEFAULT 0,
    -- Phase 1 新增字段：消息送达 / APNs / 关联追踪
    msg_id TEXT DEFAULT '',                   -- UUID，发送端生成
    correlation_id TEXT DEFAULT '',           -- 关联一轮对话（事件源 Checkpoint 用）
    priority_kind TEXT DEFAULT 'normal',      -- silent | normal | high | urgent
    delivered_at TEXT DEFAULT '',             -- 首次成功送达时间
    delivered_sessions TEXT DEFAULT '[]',     -- JSON 数组：已收到此消息的 session_id 列表
    apns_sent_at TEXT DEFAULT ''              -- APNs 推送时间
);
CREATE INDEX IF NOT EXISTS idx_ws_messages_lookup ON ws_messages(agent_id, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_ws_messages_msg_id ON ws_messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_ws_messages_undelivered ON ws_messages(agent_id, session_id, delivered_at, priority_kind);
CREATE INDEX IF NOT EXISTS idx_ws_messages_correlation ON ws_messages(correlation_id);

-- Phase 1 新增：iOS 设备 token 表（APNs 推送目标）
CREATE TABLE IF NOT EXISTS device_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    device_token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'ios',     -- ios | android (future)
    bundle_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    UNIQUE(agent_id, device_token)
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_agent ON device_tokens(agent_id, active);

-- Phase 1 新增：Agent 事件源（用于 Phase 4 的 Checkpoint）
CREATE TABLE IF NOT EXISTS agent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(agent_id, session_id, event_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_correlation ON agent_events(correlation_id, event_id);

CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES agent_tasks(id),
    url TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    title TEXT NOT NULL DEFAULT '',
    duration_seconds REAL DEFAULT 0,
    uploader TEXT DEFAULT '',
    summary TEXT,
    analysis TEXT,
    transcript_text TEXT,
    local_path TEXT,
    thumbnail_url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_project ON videos(project_id);
CREATE INDEX IF NOT EXISTS idx_videos_platform ON videos(platform);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '["arxiv","semantic_scholar"]',
    interval_hours INTEGER NOT NULL DEFAULT 24,
    last_checked_at TEXT,
    last_paper_ids TEXT DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
    paper_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    authors TEXT DEFAULT '[]',
    year INTEGER,
    abstract TEXT DEFAULT '',
    venue TEXT DEFAULT '',
    source TEXT DEFAULT '',
    doi TEXT DEFAULT '',
    pushed_at TEXT NOT NULL,
    delivered_ws INTEGER DEFAULT 0,
    UNIQUE(subscription_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_results_sub ON subscription_results(subscription_id);
CREATE INDEX IF NOT EXISTS idx_sub_results_pushed ON subscription_results(pushed_at);
"""

# 运行时迁移：为旧表添加新列（多表支持）
_MIGRATIONS = [
    # papers 表迁移
    ("papers", "ALTER TABLE papers ADD COLUMN unified_level TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN reading_level TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN digest TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN method_tags TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN dataset_info TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN code_url TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN markdown_path TEXT"),
    ("papers", "ALTER TABLE papers ADD COLUMN pdf_path TEXT"),
    # agent_tasks 表迁移（v7.0: task 类型 + 后台任务支持）
    ("agent_tasks", "ALTER TABLE agent_tasks ADD COLUMN mode TEXT DEFAULT 'foreground'"),
    ("agent_tasks", "ALTER TABLE agent_tasks ADD COLUMN name TEXT"),
    # Phase 1 ws_messages 扩展（消息送达 + APNs + 关联追踪）
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN msg_id TEXT DEFAULT ''"),
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN correlation_id TEXT DEFAULT ''"),
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN priority_kind TEXT DEFAULT 'normal'"),
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN delivered_at TEXT DEFAULT ''"),
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN delivered_sessions TEXT DEFAULT '[]'"),
    ("ws_messages", "ALTER TABLE ws_messages ADD COLUMN apns_sent_at TEXT DEFAULT ''"),
    # 索引
    ("papers", "CREATE INDEX IF NOT EXISTS idx_papers_unified_level ON papers(unified_level)"),
    ("ws_messages", "CREATE INDEX IF NOT EXISTS idx_ws_messages_msg_id ON ws_messages(msg_id)"),
    ("ws_messages", "CREATE INDEX IF NOT EXISTS idx_ws_messages_undelivered ON ws_messages(agent_id, session_id, delivered_at, priority_kind)"),
    ("ws_messages", "CREATE INDEX IF NOT EXISTS idx_ws_messages_correlation ON ws_messages(correlation_id)"),
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
        """运行缺失列的迁移和索引创建（多表支持）。"""
        for item in _MIGRATIONS:
            table, sql = item[0], item[1]
            if "ADD COLUMN" in sql:
                col = sql.split("ADD COLUMN ")[1].split(" ")[0]
                # 检查目标表的现有列
                existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
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

    def write_lock(self):
        """文件锁 contextmanager — 多进程写互斥。

        用法:
            with db.write_lock():
                db.conn.execute("INSERT ...")
                db.conn.commit()
        """
        from contextlib import contextmanager
        import fcntl

        @contextmanager
        def _lock():
            lock_path = self.db_path.with_suffix(".db.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(str(lock_path), 'w')
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
        return _lock()

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
                          session_id: str = None, max_steps: int = 50,
                          mode: str = "foreground", name: str = "") -> str:
        """创建 Agent 任务。

        Args:
            task_id: 任务唯一 ID
            user_query: 用户原始查询
            session_id: 关联的 session ID
            max_steps: 最大执行步数
            mode: 任务模式 — "foreground" | "background"
            name: 任务显示名称（用于 iOS 任务卡片）
        """
        now = self._now()
        self.conn.execute(
            """INSERT INTO agent_tasks
               (id, session_id, user_query, status, max_steps, mode, name, created_at, updated_at)
               VALUES (?,?,?,'pending',?,?,?,?,?)""",
            (task_id, session_id, user_query, max_steps, mode, name, now, now),
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

    def save_outbox_envelope(self, envelope: dict, correlation_id: str = "") -> str:
        """Phase 1: 保存出站信封到 ws_messages 表（含 msg_id / priority_kind 等扩展字段）。

        envelope 必须包含 type, role, agentId, sessionId, timestamp。
        如缺 msg_id 会自动生成 UUID 并写回 envelope（in-place 修改）。
        如缺 priorityKind 默认 'normal'（流式 thinking 应显式设 'silent'）。

        Returns:
            msg_id（UUID 字符串）
        """
        import json as _json
        if not envelope.get("msg_id"):
            envelope["msg_id"] = str(uuid.uuid4())
        msg_id = envelope["msg_id"]

        agent_id = envelope.get("agentId", "")
        session_id = envelope.get("sessionId", "")
        role = envelope.get("role", "assistant")
        msg_type = envelope.get("type", "")
        subtype = envelope.get("subType", "") or ""
        payload = envelope.get("payload", {})
        priority_kind = envelope.get("priorityKind", "normal")
        created_at = envelope.get("timestamp", self._now())

        payload_str = (_json.dumps(payload, ensure_ascii=False, default=str)
                       if not isinstance(payload, str) else payload)

        self.conn.execute(
            """INSERT INTO ws_messages
               (agent_id, session_id, role, type, subtype, payload, priority,
                created_at, msg_id, correlation_id, priority_kind,
                delivered_at, delivered_sessions, apns_sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (agent_id, session_id, role, msg_type, subtype, payload_str,
             0, created_at, msg_id, correlation_id, priority_kind,
             "", "[]", ""),
        )
        self.conn.commit()
        return msg_id

    def mark_message_delivered(self, msg_id: str, session_id: str):
        """Phase 1: 标记消息已送达某 session（用于离线同步排重）。

        delivered_at 首次送达时设置；delivered_sessions JSON 数组累加。
        """
        import json as _json
        row = self.conn.execute(
            "SELECT delivered_at, delivered_sessions FROM ws_messages WHERE msg_id=?",
            (msg_id,),
        ).fetchone()
        if not row:
            return
        try:
            sessions = _json.loads(row["delivered_sessions"] or "[]")
        except (ValueError, TypeError):
            sessions = []
        if session_id not in sessions:
            sessions.append(session_id)
        new_delivered_at = row["delivered_at"] or self._now()
        self.conn.execute(
            "UPDATE ws_messages SET delivered_at=?, delivered_sessions=? WHERE msg_id=?",
            (new_delivered_at, _json.dumps(sessions), msg_id),
        )
        self.conn.commit()

    def mark_message_apns_sent(self, msg_id: str):
        """Phase 1: 标记消息已经触发过 APNs 推送（不重复推）。"""
        self.conn.execute(
            "UPDATE ws_messages SET apns_sent_at=? WHERE msg_id=? AND apns_sent_at=''",
            (self._now(), msg_id),
        )
        self.conn.commit()

    def get_undelivered_messages(self, agent_id: str, session_id: str,
                                  since_msg_id: str = "",
                                  hours: int = 24,
                                  limit: int = 500) -> list[dict]:
        """Phase 1: 获取未送达到指定 session 的消息（按重要性过滤）。

        策略:
          - priority_kind='silent' 不参与同步（流式 thinking 不回放）
          - 默认 24h 内的消息
          - delivered_sessions 不含本 session_id 即视为未送达
          - 同 taskId 的 normal 进度消息只保留最后一条（按 payload.taskId 去重）

        Args:
            since_msg_id: 如果非空，只返回此 msg_id 之后的消息（基于 id 自增）
        """
        import json as _json
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        params = [agent_id, session_id, cutoff]
        since_clause = ""
        if since_msg_id:
            row = self.conn.execute(
                "SELECT id FROM ws_messages WHERE msg_id=?", (since_msg_id,),
            ).fetchone()
            if row:
                since_clause = "AND id > ?"
                params.append(row["id"])

        rows = self.conn.execute(
            f"""SELECT * FROM ws_messages
               WHERE agent_id=?
                 AND (session_id=? OR session_id='main')
                 AND created_at >= ?
                 AND priority_kind != 'silent'
                 {since_clause}
               ORDER BY id ASC
               LIMIT ?""",
            (*params, limit),
        ).fetchall()

        # 反序列化 + 过滤：本 session 已送达的跳过
        out = []
        progress_by_task = {}  # taskId → idx in out (用于去重)
        for r in rows:
            try:
                delivered = _json.loads(r["delivered_sessions"] or "[]")
            except (ValueError, TypeError):
                delivered = []
            if session_id in delivered:
                continue
            try:
                payload = _json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}

            entry = {
                "msg_id": r["msg_id"],
                "type": r["type"],
                "subType": r["subtype"],
                "role": r["role"],
                "agentId": r["agent_id"],
                "sessionId": r["session_id"],
                "timestamp": r["created_at"],
                "priorityKind": r["priority_kind"] or "normal",
                "correlation_id": r["correlation_id"] or "",
                "payload": payload,
            }

            # normal sub_progress 同 taskId 去重保最后一条
            if (r["priority_kind"] == "normal"
                    and r["type"] == "tool"
                    and r["subtype"] == "sub_progress"):
                task_id = payload.get("taskId", "")
                if task_id:
                    if task_id in progress_by_task:
                        out[progress_by_task[task_id]] = entry
                        continue
                    progress_by_task[task_id] = len(out)

            out.append(entry)
        return out

    # ── Device Tokens (Phase 1, APNs) ──────────────────

    def register_device_token(self, agent_id: str, device_token: str,
                              platform: str = "ios", bundle_id: str = "") -> int:
        """Phase 1: 注册 / 更新 iOS 设备 token。"""
        now = self._now()
        cursor = self.conn.execute(
            """INSERT INTO device_tokens (agent_id, device_token, platform, bundle_id, created_at, last_seen_at, active)
               VALUES (?,?,?,?,?,?,1)
               ON CONFLICT(agent_id, device_token) DO UPDATE SET
                 last_seen_at=excluded.last_seen_at,
                 active=1,
                 platform=excluded.platform,
                 bundle_id=excluded.bundle_id""",
            (agent_id, device_token, platform, bundle_id, now, now),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_active_device_tokens(self, agent_id: str) -> list[dict]:
        """Phase 1: 获取某 agent 的所有活跃设备 token。"""
        rows = self.conn.execute(
            "SELECT * FROM device_tokens WHERE agent_id=? AND active=1",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def deactivate_device_token(self, agent_id: str, device_token: str):
        """Phase 1: 标记设备 token 失效（如 APNs Unregistered 错误）。"""
        self.conn.execute(
            "UPDATE device_tokens SET active=0 WHERE agent_id=? AND device_token=?",
            (agent_id, device_token),
        )
        self.conn.commit()

    # ── Agent Events (Phase 4 Checkpoint 准备) ───────────

    def record_agent_event(self, agent_id: str, session_id: str,
                           correlation_id: str, event_type: str,
                           payload: dict) -> int:
        """Phase 4 准备: 写入主 Agent 内部状态变更事件。"""
        import json as _json
        cursor = self.conn.execute(
            """INSERT INTO agent_events
               (agent_id, session_id, correlation_id, event_type, payload, created_at)
               VALUES (?,?,?,?,?,?)""",
            (agent_id, session_id, correlation_id, event_type,
             _json.dumps(payload, ensure_ascii=False, default=str), self._now()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_events_by_correlation(self, correlation_id: str) -> list[dict]:
        """Phase 4 准备: 获取一轮对话的全部事件（按时间顺序）。"""
        import json as _json
        rows = self.conn.execute(
            "SELECT * FROM agent_events WHERE correlation_id=? ORDER BY event_id ASC",
            (correlation_id,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = _json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            out.append({
                "event_id": r["event_id"],
                "agent_id": r["agent_id"],
                "session_id": r["session_id"],
                "correlation_id": r["correlation_id"],
                "event_type": r["event_type"],
                "payload": payload,
                "created_at": r["created_at"],
            })
        return out

    def get_pending_correlations(self, agent_id: str) -> list[str]:
        """Phase 4 准备: 列出未完成（有 turn_started 但无 turn_completed）的 correlation_id。"""
        rows = self.conn.execute(
            """SELECT DISTINCT correlation_id FROM agent_events
               WHERE agent_id=? AND event_type='turn_started'
                 AND correlation_id NOT IN (
                   SELECT correlation_id FROM agent_events
                   WHERE event_type='turn_completed'
                 )""",
            (agent_id,),
        ).fetchall()
        return [r["correlation_id"] for r in rows]

    def save_ws_message(self, agent_id: str, session_id: str, seq: int,
                        role: str, type_: str, subtype: str = "",
                        payload: dict | str = None, priority: int = 0) -> int:
        """[Legacy] 保存 WS 消息到持久化表 — 旧路径兼容。

        新代码应使用 save_outbox_envelope。
        """
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

    def set_task_mode(self, task_id: str, mode: str):
        """设置任务模式（foreground ↔ background，不可逆）。"""
        self.conn.execute(
            "UPDATE agent_tasks SET mode=?, updated_at=? WHERE id=?",
            (mode, self._now(), task_id),
        )
        self.conn.commit()

    def get_foreground_task(self, session_id: str) -> Optional[dict]:
        """获取指定 session 中当前的前台任务（最多 1 个）。"""
        row = self.conn.execute(
            "SELECT * FROM agent_tasks "
            "WHERE session_id=? AND mode='foreground' AND status IN ('pending','running','paused') "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_tasks(self, agent_id: str, session_id: str) -> list[dict]:
        """获取指定 session 的活跃任务（含 mode, name 等字段用于 phase/connected）。

        Returns:
            列表每项: {"id", "name", "mode", "status", "stage", "current_step", "total_steps"}
        """
        rows = self.conn.execute(
            "SELECT id, name, mode, status, "
            "CASE status WHEN 'running' THEN COALESCE((SELECT step_name FROM task_steps "
            "  WHERE task_steps.task_id = agent_tasks.id AND task_steps.status='in_progress' "
            "  ORDER BY step_index LIMIT 1), '执行中') ELSE status END as stage, "
            "current_step, total_steps "
            "FROM agent_tasks "
            "WHERE session_id=? AND status IN ('pending','running','paused') "
            "ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        return [
            {
                "taskId": r["id"],
                "name": r["name"] or r["id"],
                "mode": r["mode"] or "foreground",
                "status": r["status"],
                "stage": r["stage"],
                "current": r["current_step"] or 0,
                "total": r["total_steps"] or 0,
            }
            for r in rows
        ]

    # ── Video CRUD ────────────────────────────────────────

    def save_video_result(self, project_id: str, video_id: str, url: str,
                          platform: str = "", title: str = "",
                          duration_seconds: float = 0,
                          uploader: str = "",
                          summary: dict = None,
                          analysis: dict = None,
                          local_path: str = "",
                          transcript_text: str = None) -> str:
        """保存或更新视频分析结果。

        Args:
            project_id: 主任务 ID
            video_id: 平台视频 ID
            url: 视频 URL
            platform: 平台名 (douyin/tiktok/...)
            title: 视频标题
            duration_seconds: 时长 (秒)
            uploader: 上传者
            summary: LLM 结构化摘要 (dict → JSON)
            analysis: LLM 深度分析 (dict → JSON)
            local_path: 本地视频文件路径
            transcript_text: 转录全文

        Returns:
            video_id (用于后续查询)
        """
        import uuid as _uuid
        now = self._now()
        vid = video_id or f"vid:{_uuid.uuid4().hex[:12]}"
        summary_json = json.dumps(summary, ensure_ascii=False, default=str) if summary else None
        analysis_json = json.dumps(analysis, ensure_ascii=False, default=str) if analysis else None

        self.conn.execute(
            """INSERT OR REPLACE INTO videos
               (id, project_id, url, platform, title, duration_seconds,
                uploader, summary, analysis, transcript_text, local_path, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, project_id, url, platform, title, duration_seconds,
             uploader, summary_json, analysis_json, transcript_text,
             local_path, now, now),
        )
        self.conn.commit()
        return vid

    def get_video_result(self, video_id: str) -> Optional[dict]:
        """获取视频分析结果。

        Args:
            video_id: 视频 ID

        Returns:
            dict 含 deserialized summary/analysis JSON 字段，或 None
        """
        row = self.conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for field in ("summary", "analysis"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def list_video_results(self, project_id: str, limit: int = 20) -> list[dict]:
        """列出项目的所有视频结果。

        Args:
            project_id: 项目 ID
            limit: 最大返回数

        Returns:
            list[dict] 包含 id, url, platform, title, duration_seconds, uploader, created_at
        """
        rows = self.conn.execute(
            """SELECT id, project_id, url, platform, title, duration_seconds,
                      uploader, created_at
               FROM videos WHERE project_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Subscription CRUD ───────────────────────────────

    def create_subscription(self, name: str, keywords: str,
                            sources: list[str] = None,
                            interval_hours: int = 24) -> str:
        """创建订阅，返回 subscription_id。"""
        import uuid as _uuid
        sub_id = str(_uuid.uuid4())[:8]
        now = self._now()
        sources_json = json.dumps(sources or ["arxiv", "semantic_scholar"])
        self.conn.execute(
            """INSERT INTO subscriptions
               (id, name, keywords, sources, interval_hours,
                enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (sub_id, name, keywords, sources_json, interval_hours, now, now),
        )
        self.conn.commit()
        return sub_id

    def get_subscription(self, subscription_id: str) -> Optional[dict]:
        """获取订阅详情。"""
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for field in ("sources", "last_paper_ids"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        return d

    def list_subscriptions(self, enabled_only: bool = False) -> list[dict]:
        """列出所有订阅。"""
        query = "SELECT * FROM subscriptions"
        params: list = []
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            for field in ("sources", "last_paper_ids"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
            results.append(d)
        return results

    def update_subscription(self, subscription_id: str, **kwargs) -> bool:
        """更新订阅字段。"""
        kwargs["updated_at"] = self._now()
        for field in ("sources", "last_paper_ids"):
            if field in kwargs and isinstance(kwargs[field], list):
                kwargs[field] = json.dumps(kwargs[field])
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [subscription_id]
        self.conn.execute(
            f"UPDATE subscriptions SET {sets} WHERE id=?", vals
        )
        self.conn.commit()
        return True

    def delete_subscription(self, subscription_id: str) -> bool:
        """删除订阅及其结果。"""
        self.conn.execute(
            "DELETE FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        self.conn.execute(
            "DELETE FROM subscription_results WHERE subscription_id = ?",
            (subscription_id,),
        )
        self.conn.commit()
        return True

    def save_subscription_result(self, subscription_id: str,
                                  paper: dict) -> int:
        """保存单篇论文推送结果（INSERT OR IGNORE 去重）。"""
        now = self._now()
        authors_json = json.dumps(paper.get("authors", []), ensure_ascii=False)
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO subscription_results
               (subscription_id, paper_id, title, authors, year, abstract,
                venue, source, doi, pushed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (subscription_id, paper.get("paper_id", ""),
             paper.get("title", ""), authors_json,
             paper.get("year"), paper.get("abstract", ""),
             paper.get("venue", ""), paper.get("source", ""),
             paper.get("doi", ""), now),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_subscription_results(self, subscription_id: str,
                                  since: str = None,
                                  limit: int = 50) -> list[dict]:
        """获取订阅的推送历史。"""
        query = "SELECT * FROM subscription_results WHERE subscription_id = ?"
        params: list = [subscription_id]
        if since:
            query += " AND pushed_at > ?"
            params.append(since)
        query += " ORDER BY pushed_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["authors"] = json.loads(d.get("authors", "[]"))
            except Exception:
                d["authors"] = []
            results.append(d)
        return results

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
