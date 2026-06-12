"""SQLite 持久化 — 搜索项目、论文库、日志."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("~/.paper_search/agent.db").expanduser()

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

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);
CREATE INDEX IF NOT EXISTS idx_project_papers_project ON project_papers(project_id);
CREATE INDEX IF NOT EXISTS idx_search_logs_project ON search_logs(project_id);
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
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
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

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
