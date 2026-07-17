"""PostgreSQL 数据库访问层 — 替代 SQLite AgentDB。

API 完全兼容 AgentDB（db.py），调用方只需更换导入路径即可切换数据库后端。
所有方法均包含 user_id 参数用于多用户数据隔离。

连接:
    DATABASE_URL 环境变量 → PostgreSQL 连接字符串
    DATABASE_URL 必须设置

使用示例:
    db = PostgresAgentDB()
    db.create_project("文献调研", user_id="user-001")
    papers = db.get_project_papers("prj-001", user_id="user-001")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}-{uid}" if prefix else uid


class _PgCompatCursor:
    """psycopg2 cursor 的兼容包装 — 模拟 sqlite3 Cursor 的 .fetchone()/.fetchall()。

    额外提供 __iter__ 支持 ``for row in cursor`` 和 ``dict(row)``。
    """

    def __init__(self, pg_cursor, conn_wrapper):
        self._cur = pg_cursor
        self._conn_wrapper = conn_wrapper
        self._lastrowid = None

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        # 如果 psycopg2 没有 RealDictCursor，手动转换为 dict
        desc = [d[0] for d in self._cur.description] if self._cur.description else []
        if desc:
            return dict(zip(desc, row))
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        desc = [d[0] for d in self._cur.description] if self._cur.description else []
        if desc and rows and not isinstance(rows[0], dict):
            return [dict(zip(desc, row)) for row in rows]
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    @property
    def lastrowid(self):
        return self._lastrowid


class _PgCompatConnection:
    """psycopg2 connection 的兼容包装 — 模拟 sqlite3.Connection。

    核心能力:
      - .execute(sql, params) → 返回 _PgCompatCursor（自动转换 ? → %s）
      - .commit() → 委托给原生 conn.commit()
      - .close() → 委托给原生 conn.close()
    """

    def __init__(self, pg_conn):
        self._pg_conn = pg_conn

    def execute(self, sql: str, params=()):
        """执行 SQL 查询，兼容 sqlite3 调用风格。

        自动将 SQLite 风格的 ? 占位符替换为 PostgreSQL 的 %s；
        对已包含 %s 的 SQL 不做重复替换。
        """
        if params is None:
            params = ()
        # 仅在 SQL 不包含 %s 时做 ? → %s 转换
        if "%s" not in sql and "?" in sql:
            sql = sql.replace("?", "%s")
        cur = self._pg_conn.cursor()
        try:
            cur.execute(sql, params)
        except Exception:
            self._pg_conn.rollback()
            cur.close()
            raise
        return _PgCompatCursor(cur, self)

    def commit(self):
        self._pg_conn.commit()

    def close(self):
        try:
            self._pg_conn.close()
        except Exception:
            pass

    @property
    def closed(self):
        return self._pg_conn.closed


class PostgresAgentDB:
    """PostgreSQL 数据访问对象，API 兼容 AgentDB (SQLite)。

    所有查询自动附带 user_id 过滤，实现多用户数据隔离。
    使用 psycopg2 同步连接，conn 属性返回 sqlite3 兼容包装。
    """

    def __init__(self, dsn: str = None):
        import psycopg2
        import psycopg2.extras

        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self.dsn = dsn or os.environ.get("DATABASE_URL", "")
        self._conn = None
        self._compat_conn = None

    @property
    def _raw_conn(self):
        """返回原生 psycopg2 connection（内部方法使用）。"""
        if self._conn is None or self._conn.closed:
            if not self.dsn:
                raise RuntimeError("DATABASE_URL 未设置，无法连接 PostgreSQL")
            self._conn = self._psycopg2.connect(self.dsn)
            self._conn.autocommit = False
        return self._conn

    @property
    def conn(self):
        """返回 sqlite3 兼容的 connection 包装（.execute / .commit / .close）。"""
        if self._conn is None or self._conn.closed:
            if not self.dsn:
                raise RuntimeError("DATABASE_URL 未设置，无法连接 PostgreSQL")
            self._conn = self._psycopg2.connect(self.dsn)
            self._conn.autocommit = False
            self._compat_conn = _PgCompatConnection(self._conn)
        return self._compat_conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        cur = self._raw_conn.cursor(cursor_factory=self._extras.RealDictCursor)
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._raw_conn.cursor(cursor_factory=self._extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _execute(self, sql: str, params: tuple = ()):
        cur = self._raw_conn.cursor()
        try:
            cur.execute(sql, params)
            self._raw_conn.commit()
        except Exception:
            self._raw_conn.rollback()
            raise

    # ═══════════════════════════════════════════════════════════════
    # 用户管理（新增）
    # ═══════════════════════════════════════════════════════════════

    def get_user_by_token(self, token: str) -> Optional[dict]:
        """按 API token 查找用户。"""
        return self._fetchone(
            "SELECT id, username, display_name, role FROM users WHERE api_token = %s AND is_active = true",
            (token,),
        )

    def get_user(self, user_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM users WHERE id = %s", (user_id,))

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """按用户名查找用户（用于登录）。"""
        return self._fetchone(
            "SELECT id, username, display_name, password_hash, role, is_active FROM users WHERE username = %s",
            (username,),
        )

    def create_user(self, username: str, display_name: str, api_token: str = None,
                    role: str = "researcher", password_hash: str = None) -> str:
        user_id = f"user-{_uuid('')[4:16]}"
        token = api_token or f"tok-{_uuid('')[4:24]}"
        self._execute(
            """INSERT INTO users (id, username, display_name, api_token, role, password_hash)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, username, display_name, token, role, password_hash),
        )
        return user_id

    def count_user_papers(self, user_id: str) -> int:
        """获取用户的论文总数（用于冷启动检测）。"""
        cur = self._raw_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM papers WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]

    # ═══════════════════════════════════════════════════════════════
    # 智能体管理 (v3.2 多智能体)
    # ═══════════════════════════════════════════════════════════════

    def list_user_agents(self, user_id: str, include_inactive: bool = False) -> list[dict]:
        """列出用户的所有智能体."""
        extra = "" if include_inactive else " AND is_active = true"
        return self._fetchall(
            f"SELECT * FROM agents WHERE user_id = %s{extra} ORDER BY created_at",
            (user_id,),
        )

    def get_agent(self, agent_id: str) -> Optional[dict]:
        """获取单个智能体配置."""
        return self._fetchone("SELECT * FROM agents WHERE id = %s", (agent_id,))

    def get_default_agent(self, user_id: str) -> Optional[dict]:
        """获取用户的默认智能体（最早创建的活跃 main 类型）。"""
        return self._fetchone(
            "SELECT * FROM agents WHERE user_id = %s AND agent_type = 'main' AND is_active = true ORDER BY created_at LIMIT 1",
            (user_id,),
        )

    def create_agent(
        self,
        user_id: str,
        name: str = "My Agent",
        display_name: str = "Paper Agent",
        system_prompt: str = "",
        llm_provider: str = "deepseek",
        agent_type: str = "main",
    ) -> str:
        """创建智能体，返回 agent_id."""
        agent_id = _uuid("agent")
        self._execute(
            """INSERT INTO agents (id, user_id, name, display_name, agent_type, system_prompt, llm_provider)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (agent_id, user_id, name, display_name, agent_type, system_prompt, llm_provider),
        )
        return agent_id

    def update_agent(self, agent_id: str, user_id: str = None, **kwargs) -> bool:
        """更新智能体配置。user_id 可选用于权限校验."""
        if not kwargs:
            return False
        allowed = {"name", "display_name", "system_prompt", "llm_provider", "config", "is_active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = "now()"
        sets = [f"{k} = %s" for k in updates]
        values = list(updates.values())
        where = "id = %s"
        params = values + [agent_id]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        # Handle now() specially — pass as literal
        self._execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE {where}".replace("'now()'", "now()"),
            tuple(params),
        )
        return True

    def deactivate_agent(self, agent_id: str, user_id: str = None) -> bool:
        """软删除智能体."""
        return self.update_agent(agent_id, user_id=user_id, is_active=False)

    def agent_belongs_to_user(self, agent_id: str, user_id: str) -> bool:
        """验证智能体是否属于该用户."""
        row = self._fetchone(
            "SELECT 1 FROM agents WHERE id = %s AND user_id = %s",
            (agent_id, user_id),
        )
        return row is not None

    # ═══════════════════════════════════════════════════════════════
    # 用户偏好 (基于 user_configs 表)
    # ═══════════════════════════════════════════════════════════════

    def get_user_preferences(self, user_id: str) -> dict:
        """获取用户所有偏好（返回 key→value dict）."""
        rows = self._fetchall(
            "SELECT config_key, config_value FROM user_configs WHERE user_id = %s",
            (user_id,),
        )
        return {r["config_key"]: r["config_value"] for r in rows}

    def get_user_preference(self, user_id: str, key: str) -> Optional[Any]:
        """获取单个偏好值."""
        row = self._fetchone(
            "SELECT config_value FROM user_configs WHERE user_id = %s AND config_key = %s",
            (user_id, key),
        )
        return row["config_value"] if row else None

    def set_user_preference(self, user_id: str, key: str, value: Any) -> None:
        """设置用户偏好（upsert）."""
        import json as _json
        val = _json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        self._execute(
            """INSERT INTO user_configs (id, user_id, config_key, config_value)
               VALUES (%s, %s, %s, %s::jsonb)
               ON CONFLICT (user_id, config_key) DO UPDATE SET config_value = EXCLUDED.config_value""",
            (_uuid("cfg"), user_id, key, val),
        )

    def delete_user_preference(self, user_id: str, key: str) -> bool:
        """删除用户偏好."""
        cur = self._raw_conn.cursor()
        cur.execute(
            "DELETE FROM user_configs WHERE user_id = %s AND config_key = %s",
            (user_id, key),
        )
        return cur.rowcount > 0

    # ═══════════════════════════════════════════════════════════════
    # 项目管理
    # ═══════════════════════════════════════════════════════════════

    def create_project(self, user_query: str, parsed_intent: dict = None,
                       project_id: str = None, user_id: str = "anonymous") -> str:
        pid = project_id or _uuid("prj")
        self._execute(
            """INSERT INTO projects (id, user_id, name, description, domain)
               VALUES (%s, %s, %s, %s, %s)""",
            (pid, user_id, user_query[:200], json.dumps(parsed_intent or {}, ensure_ascii=False), ""),
        )
        return pid

    def update_project(self, project_id: str, user_id: str = None, **kwargs):
        if not kwargs:
            return
        sets = [f"{k} = %s" for k in kwargs]
        values = list(kwargs.values())
        where = "id = %s"
        params = values + [project_id]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        self._execute(f"UPDATE projects SET {', '.join(sets)} WHERE {where}", tuple(params))

    def get_project(self, project_id: str, user_id: str = None) -> Optional[dict]:
        if user_id:
            return self._fetchone("SELECT * FROM projects WHERE id = %s AND user_id = %s", (project_id, user_id))
        return self._fetchone("SELECT * FROM projects WHERE id = %s", (project_id,))

    def list_projects(self, limit: int = 20, user_id: str = None) -> list[dict]:
        if user_id:
            return self._fetchall("SELECT * FROM projects WHERE user_id = %s ORDER BY created_at DESC LIMIT %s", (user_id, limit))
        return self._fetchall("SELECT * FROM projects ORDER BY created_at DESC LIMIT %s", (limit,))

    # ═══════════════════════════════════════════════════════════════
    # 论文管理
    # ═══════════════════════════════════════════════════════════════

    def _make_paper_id(self, paper: dict) -> str:
        doi = paper.get("doi", "")
        if doi:
            return f"doi:{doi}"
        arxiv = paper.get("arxiv_id", "")
        if arxiv:
            return f"arxiv:{arxiv}"
        pmid = paper.get("pmid", "")
        if pmid:
            return f"pmid:{pmid}"
        title = paper.get("title", "")
        h = hashlib.sha256(title.encode()).hexdigest()[:16]
        return f"sha256:{h}"

    def upsert_paper(self, paper: dict, user_id: str = "anonymous") -> str:
        pid = paper.get("id") or self._make_paper_id(paper)
        title = paper.get("title", "")
        authors = paper.get("authors", "[]")
        if isinstance(authors, str):
            try:
                authors = json.dumps(json.loads(authors), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                authors = json.dumps([authors], ensure_ascii=False)
        elif isinstance(authors, (list, dict)):
            authors = json.dumps(authors, ensure_ascii=False)

        keywords = paper.get("keywords", "[]")
        if isinstance(keywords, str):
            try:
                keywords = json.dumps(json.loads(keywords), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                keywords = json.dumps([keywords], ensure_ascii=False)
        elif isinstance(keywords, (list, dict)):
            keywords = json.dumps(keywords, ensure_ascii=False)

        venue = paper.get("journal") or paper.get("conference") or paper.get("venue", "")

        self._execute(
            """INSERT INTO papers (id, user_id, title, authors, year, venue, doi, arxiv_id,
               abstract, keywords, source, citation_count, file_path, md_path, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'ingested', %s, %s)
               ON CONFLICT (id) DO UPDATE SET
               title=EXCLUDED.title, authors=EXCLUDED.authors, year=EXCLUDED.year,
               venue=EXCLUDED.venue, abstract=EXCLUDED.abstract, keywords=EXCLUDED.keywords,
               citation_count=EXCLUDED.citation_count, updated_at=EXCLUDED.updated_at""",
            (
                pid, user_id, title, authors,
                paper.get("year"), venue,
                paper.get("doi"), paper.get("arxiv_id"),
                paper.get("abstract", ""),
                keywords,
                paper.get("source", "manual"),
                paper.get("citation_count", 0),
                paper.get("pdf_path") or paper.get("file_path"),
                paper.get("markdown_path") or paper.get("md_path"),
                _now(), _now(),
            ),
        )
        return pid

    def update_paper_meta(self, paper_id: str, user_id: str = None, **kwargs):
        if not kwargs:
            return
        sets = [f"{k} = %s" for k in kwargs]
        values = list(kwargs.values())
        where = "id = %s"
        params = values + [paper_id]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        self._execute(f"UPDATE papers SET {', '.join(sets)}, updated_at = %s WHERE {where}",
                      tuple(params + [_now()]))

    def _paper_from_row(self, row: dict) -> dict:
        """将 DB 行转换为与 SQLite AgentDB 兼容的格式。"""
        if not row:
            return None
        paper = dict(row)
        # 添加兼容字段
        if "authors" in paper and isinstance(paper["authors"], str):
            paper["authors"] = paper["authors"]  # keep as JSON string for compat
        if "md_path" in paper:
            paper["markdown_path"] = paper["md_path"]
        if "file_path" in paper:
            paper["pdf_path"] = paper["file_path"]
        return paper

    def get_paper(self, paper_id: str, user_id: str = None) -> Optional[dict]:
        if user_id:
            row = self._fetchone("SELECT * FROM papers WHERE id = %s AND user_id = %s", (paper_id, user_id))
        else:
            row = self._fetchone("SELECT * FROM papers WHERE id = %s", (paper_id,))
        return self._paper_from_row(row)

    def list_user_papers(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM papers WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [self._paper_from_row(r) for r in rows]

    def search_papers(self, query: str, user_id: str = None, limit: int = 20) -> list[dict]:
        """全文搜索论文标题和摘要。"""
        where = "to_tsvector('english', title || ' ' || abstract) @@ plainto_tsquery('english', %s)"
        params = [query]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        params.append(limit)
        rows = self._fetchall(
            f"SELECT * FROM papers WHERE {where} ORDER BY year DESC LIMIT %s",
            tuple(params),
        )
        return [self._paper_from_row(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # 精确元数据查找 (KnowledgeAgent local PDF ingest)
    # ═══════════════════════════════════════════════════════════════

    def find_paper_by_title_exact(self, title: str, user_id: str = None) -> Optional[dict]:
        """标题精确匹配 — 小写去标点后比较。

        用于快速去重：<1ms/篇。
        """
        # 标准化：小写 + 去标点 + 合并空白
        import re
        normalized = re.sub(r'[^\w\s]', '', title.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        if len(normalized) < 5:
            return None

        params = [f"%{normalized}%"]  # LIKE fallback: 标题中可能含有多余前缀/后缀
        if user_id:
            sql = """SELECT * FROM papers
                     WHERE user_id = %s
                       AND LOWER(REGEXP_REPLACE(title, '[^\\w\\s]', '', 'g')) = %s
                     LIMIT 1"""
            params = [user_id, normalized]
        else:
            sql = """SELECT * FROM papers
                     WHERE LOWER(REGEXP_REPLACE(title, '[^\\w\\s]', '', 'g')) = %s
                     LIMIT 1"""

        row = self._fetchone(sql, tuple(params))
        return self._paper_from_row(row) if row else None

    def find_papers_by_metadata(self, filters: dict, user_id: str = None,
                                 limit: int = 50) -> list[dict]:
        """多字段组合 AND 精确查询。

        filters 支持: title (LIKE), author, year, doi, source, venue
        """
        conditions = []
        params = []

        if user_id:
            conditions.append("user_id = %s")
            params.append(user_id)

        if "title" in filters:
            conditions.append("LOWER(title) LIKE %s")
            params.append(f"%{filters['title'].lower()}%")
        if "author" in filters:
            conditions.append("authors::text ILIKE %s")
            params.append(f"%{filters['author']}%")
        if "year" in filters:
            conditions.append("year = %s")
            params.append(int(filters["year"]))
        if "doi" in filters:
            conditions.append("LOWER(doi) = %s")
            params.append(filters["doi"].lower())
        if "source" in filters:
            conditions.append("source = %s")
            params.append(filters["source"])
        if "venue" in filters:
            conditions.append("venue ILIKE %s")
            params.append(f"%{filters['venue']}%")

        if not conditions:
            return []

        where = " AND ".join(conditions)
        rows = self._fetchall(
            f"SELECT * FROM papers WHERE {where} ORDER BY year DESC LIMIT %s",
            tuple(params + [limit]),
        )
        return [self._paper_from_row(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # 论文图表 + 归档 (KnowledgeAgent local PDF ingest)
    # ═══════════════════════════════════════════════════════════════

    def save_paper_figure(self, figure: dict) -> str:
        """写入 paper_figures 表。figure 含 id, paper_id, caption, figure_type,
        local_path, oss_path, page_number, image_hash."""
        self._execute(
            """INSERT INTO paper_figures (id, paper_id, caption, figure_type,
               local_path, oss_path, page_number, image_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
               caption = EXCLUDED.caption, oss_path = EXCLUDED.oss_path""",
            (figure["id"], figure["paper_id"], figure.get("caption", ""),
             figure.get("figure_type", "figure"), figure.get("local_path", ""),
             figure.get("oss_path", ""), figure.get("page_number"), figure.get("image_hash", "")),
        )
        return figure["id"]

    def get_paper_figures(self, paper_id: str) -> list[dict]:
        """获取某篇论文的所有图表元数据."""
        rows = self._fetchall(
            "SELECT * FROM paper_figures WHERE paper_id = %s ORDER BY page_number",
            (paper_id,),
        )
        return [dict(r) for r in rows]

    def archive_pdf(self, paper_id: str, original_path: str, oss_path: str,
                    file_size: int = 0, md5_hash: str = "") -> str:
        """记录 PDF 归档信息."""
        archive_id = _uuid("arc")
        self._execute(
            """INSERT INTO paper_archives (id, paper_id, original_pdf_path,
               oss_pdf_path, file_size_bytes, md5_hash)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (archive_id, paper_id, original_path, oss_path, file_size, md5_hash),
        )
        # 更新 papers 表的 file_path 指向归档位置
        self._execute(
            "UPDATE papers SET file_path = %s, updated_at = %s WHERE id = %s",
            (oss_path, _now(), paper_id),
        )
        return archive_id

    # ═══════════════════════════════════════════════════════════════
    # 项目-论文关联
    # ═══════════════════════════════════════════════════════════════

    def link_paper_to_project(self, project_id: str, paper_id: str,
                               round_num: int = 1, relevance_score: float = 0.5,
                               relevance_reason: str = ""):
        pp_id = _uuid("pp")
        self._execute(
            """INSERT INTO project_papers (id, project_id, paper_id)
               VALUES (%s, %s, %s) ON CONFLICT (project_id, paper_id) DO NOTHING""",
            (pp_id, project_id, paper_id),
        )

    def mark_pdf_downloaded(self, project_id: str, paper_id: str, pdf_path: str):
        self._execute(
            "UPDATE papers SET file_path = %s, updated_at = %s WHERE id = %s",
            (pdf_path, _now(), paper_id),
        )

    def get_project_papers(self, project_id: str, relevant_only: bool = False,
                           user_id: str = None) -> list[dict]:
        sql = """SELECT p.* FROM papers p
                 JOIN project_papers pp ON p.id = pp.paper_id
                 WHERE pp.project_id = %s"""
        params = [project_id]
        if user_id:
            sql += " AND p.user_id = %s"
            params.append(user_id)
        sql += " ORDER BY p.created_at DESC"
        rows = self._fetchall(sql, tuple(params))
        return [self._paper_from_row(r) for r in rows]

    def get_relevant_papers(self, project_id: str, user_id: str = None) -> list[dict]:
        return self.get_project_papers(project_id, relevant_only=True, user_id=user_id)

    # ═══════════════════════════════════════════════════════════════
    # 期刊分级
    # ═══════════════════════════════════════════════════════════════

    def upsert_journal_rank(self, venue: str, ccf: str = None, sci: str = None, unified: str = None):
        jr_id = _uuid("jr")
        rank = ccf or sci or unified or ""
        self._execute(
            """INSERT INTO journal_ranks (id, venue, rank, source)
               VALUES (%s, %s, %s, 'custom')
               ON CONFLICT (venue) DO UPDATE SET rank = EXCLUDED.rank""",
            (jr_id, venue, rank),
        )

    def get_journal_rank(self, venue: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM journal_ranks WHERE venue = %s", (venue,))

    # ═══════════════════════════════════════════════════════════════
    # 搜索日志
    # ═══════════════════════════════════════════════════════════════

    def log_search(self, project_id: str, round_num: int, source: str, query: str,
                   results_count: int, duration_ms: int = 0, error: str = None,
                   user_id: str = "anonymous"):
        slog_id = _uuid("slog")
        self._execute(
            """INSERT INTO search_logs (id, user_id, query, source, result_count, duration_ms)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (slog_id, user_id, query, source, results_count, duration_ms),
        )

    # ═══════════════════════════════════════════════════════════════
    # 引用关系
    # ═══════════════════════════════════════════════════════════════

    def add_citation(self, source_paper_id: str, target_title: str,
                     target_paper_id: str = None, target_doi: str = None,
                     target_year: int = None, relation_type: str = "references",
                     confidence: float = 1.0):
        cit_id = _uuid("cit")
        self._execute(
            """INSERT INTO citations (id, paper_id, cited_paper_id, cited_doi, cited_title, cited_year, classification, confidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (cit_id, source_paper_id, target_paper_id, target_doi, target_title, target_year, relation_type, confidence),
        )

    def get_citations(self, paper_id: str, relation_type: str = None, direction: str = "outgoing") -> list[dict]:
        if direction == "outgoing":
            sql = "SELECT * FROM citations WHERE paper_id = %s"
            params = [paper_id]
        else:
            sql = "SELECT * FROM citations WHERE cited_paper_id = %s"
            params = [paper_id]
        if relation_type:
            sql += " AND classification = %s"
            params.append(relation_type)
        return self._fetchall(sql, tuple(params))

    def get_citation_count(self, paper_id: str) -> dict:
        outgoing = self._fetchone("SELECT COUNT(*) as cnt FROM citations WHERE paper_id = %s", (paper_id,))
        incoming = self._fetchone("SELECT COUNT(*) as cnt FROM citations WHERE cited_paper_id = %s", (paper_id,))
        return {
            "outgoing": outgoing["cnt"] if outgoing else 0,
            "incoming": incoming["cnt"] if incoming else 0,
        }

    # ═══════════════════════════════════════════════════════════════
    # Agent 任务
    # ═══════════════════════════════════════════════════════════════

    def create_agent_task(self, task_id: str, user_query: str, session_id: str = None,
                          max_steps: int = 50, mode: str = "foreground", name: str = "",
                          user_id: str = "anonymous") -> str:
        tid = task_id or _uuid("task")
        self._execute(
            """INSERT INTO agent_tasks (id, user_id, mode, name, agent_name, task_kind, status, created_at)
               VALUES (%s, %s, %s, %s, 'MainAgent', 'orchestrate', 'pending', %s)""",
            (tid, user_id, mode, name or user_query[:100], _now()),
        )
        return tid

    def update_agent_task(self, task_id: str, user_id: str = None, **kwargs):
        if not kwargs:
            return
        sets = [f"{k} = %s" for k in kwargs]
        values = list(kwargs.values())
        where = "id = %s"
        params = values + [task_id]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        self._execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE {where}", tuple(params))

    def get_agent_task(self, task_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM agent_tasks WHERE id = %s", (task_id,))

    def list_agent_tasks(self, session_id: str = None, limit: int = 20, user_id: str = None) -> list[dict]:
        if user_id:
            return self._fetchall(
                "SELECT * FROM agent_tasks WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
        return self._fetchall("SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT %s", (limit,))

    def set_task_mode(self, task_id: str, mode: str):
        self._execute("UPDATE agent_tasks SET mode = %s WHERE id = %s", (mode, task_id))

    def get_foreground_task(self, session_id: str, user_id: str = None) -> Optional[dict]:
        if user_id:
            return self._fetchone(
                "SELECT * FROM agent_tasks WHERE user_id = %s AND mode = 'foreground' AND status = 'running' LIMIT 1",
                (user_id,),
            )
        return self._fetchone(
            "SELECT * FROM agent_tasks WHERE mode = 'foreground' AND status = 'running' LIMIT 1",
        )

    def get_active_tasks(self, agent_id: str, session_id: str, user_id: str = None) -> list[dict]:
        params = [session_id]
        sql = """SELECT id AS "taskId", name, mode, status,
                 progress->>'stage' AS stage, progress->>'current' AS current, progress->>'total' AS total
                 FROM agent_tasks WHERE status IN ('pending','running')"""
        if user_id:
            sql += " AND user_id = %s"
            params.insert(0, user_id)
        return self._fetchall(sql, tuple(params))

    # ═══════════════════════════════════════════════════════════════
    # 任务步骤
    # ═══════════════════════════════════════════════════════════════

    def add_task_step(self, task_id: str, step_index: int, step_name: str,
                      action: str, tool_name: str = None, tool_args: dict = None) -> int:
        step_id = _uuid("step")
        self._execute(
            """INSERT INTO task_steps (id, task_id, step_order, step_name, status, detail)
               VALUES (%s, %s, %s, %s, 'pending', %s)
               ON CONFLICT DO NOTHING""",
            (step_id, task_id, step_index, step_name, json.dumps({"action": action, "tool_name": tool_name, "tool_args": tool_args or {}}, ensure_ascii=False) if action else "{}"),
        )
        return step_index

    def update_task_step(self, task_id: str, step_index: int, **kwargs):
        if not kwargs:
            return
        sets = [f"{k} = %s" for k in kwargs]
        values = list(kwargs.values()) + [task_id, step_index]
        self._execute(
            f"UPDATE task_steps SET {', '.join(sets)} WHERE task_id = %s AND step_order = %s",
            tuple(values),
        )

    def get_task_steps(self, task_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM task_steps WHERE task_id = %s ORDER BY step_order",
            (task_id,),
        )

    def get_task_step(self, task_id: str, step_index: int) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM task_steps WHERE task_id = %s AND step_order = %s",
            (task_id, step_index),
        )

    # ═══════════════════════════════════════════════════════════════
    # 会话管理
    # ═══════════════════════════════════════════════════════════════

    def create_session(self, agent_id: str, session_id: str, title: str = "新对话",
                       user_id: str = "anonymous") -> str:
        sess_id = session_id or _uuid("sess")
        thread_id = session_id
        self._execute(
            """INSERT INTO sessions (id, user_id, agent_id, thread_id, title, status)
               VALUES (%s, %s, %s, %s, %s, 'active')
               ON CONFLICT (id) DO NOTHING""",
            (sess_id, user_id, agent_id, thread_id, title),
        )
        return sess_id

    def get_session(self, agent_id: str, session_id: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM sessions WHERE agent_id = %s AND id = %s",
            (agent_id, session_id),
        )

    def update_session_title(self, agent_id: str, session_id: str, title: str):
        self._execute(
            "UPDATE sessions SET title = %s, updated_at = %s WHERE agent_id = %s AND id = %s",
            (title, _now(), agent_id, session_id),
        )

    def list_sessions(self, agent_id: str, user_id: str = None) -> list[dict]:
        if user_id:
            return self._fetchall(
                "SELECT * FROM sessions WHERE user_id = %s AND agent_id = %s ORDER BY updated_at DESC",
                (user_id, agent_id),
            )
        return self._fetchall(
            "SELECT * FROM sessions WHERE agent_id = %s ORDER BY updated_at DESC",
            (agent_id,),
        )

    # ═══════════════════════════════════════════════════════════════
    # WebSocket 消息 (Outbox)
    # ═══════════════════════════════════════════════════════════════

    def save_outbox_envelope(self, envelope: dict, correlation_id: str = "",
                             user_id: str = "anonymous") -> str:
        msg_id = envelope.get("msg_id", _uuid("msg"))
        session_id = envelope.get("sessionId", "")
        direction = "inbound" if envelope.get("role") == "user" else "outbound"
        msg_type = envelope.get("type", "message")
        subtype = envelope.get("subType", "")
        payload_raw = envelope.get("payload", {})
        if isinstance(payload_raw, dict):
            payload_str = json.dumps(payload_raw, ensure_ascii=False)
        else:
            payload_str = str(payload_raw)
            payload_raw = {}
        priority = envelope.get("priorityKind") or envelope.get("priority", "normal")
        seq = envelope.get("seq", 0)

        # ── Upsert dedup: keep only the latest state per entity ──
        # Tool messages: same session + tool_call_id → replace old state
        tool_call_id = payload_raw.get("tool_call_id", "") if isinstance(payload_raw, dict) else ""
        if msg_type == "tool" and tool_call_id:
            self._execute(
                "DELETE FROM ws_messages WHERE session_id = %s AND msg_type = 'tool' AND payload->>'tool_call_id' = %s",
                (session_id, tool_call_id),
            )
        # plan_todo_update: same session + plan_id → replace old state
        plan_id = payload_raw.get("plan_id", "") if isinstance(payload_raw, dict) else ""
        if msg_type == "plan_todo_update" and plan_id:
            self._execute(
                "DELETE FROM ws_messages WHERE session_id = %s AND msg_type = 'plan_todo_update' AND payload->>'plan_id' = %s",
                (session_id, plan_id),
            )

        self._execute(
            """INSERT INTO ws_messages (id, session_id, user_id, seq, direction, msg_type, subtype, payload, priority_kind, correlation_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (msg_id, session_id, user_id, seq, direction, msg_type, subtype, payload_str, priority, correlation_id),
        )
        return msg_id

    def get_final_state_messages(self, session_id: str, since_ts: str = "",
                                  limit: int = 200) -> list[dict]:
        """获取 session 的最终状态消息（去重，REST API 用）。

        去重规则:
          - tool/* 消息: 同一 tool_call_id 只保留最新一条
          - plan_todo_update: 同一 plan_id 只保留最新一条
          - 其他: 按 msg_id 保留（不重复）
          - priority_kind='silent' 的消息始终排除
        """
        sql = """
            SELECT * FROM (
                SELECT DISTINCT ON (dedup_key) *,
                    CASE
                        WHEN msg_type = 'tool'
                             AND payload->>'tool_call_id' IS NOT NULL
                            THEN 'tool:' || (payload->>'tool_call_id')
                        WHEN msg_type = 'plan_todo_update'
                             AND payload->>'plan_id' IS NOT NULL
                            THEN 'plan:' || (payload->>'plan_id')
                        ELSE 'msg:' || COALESCE(id, '')
                    END AS dedup_key
                FROM ws_messages
                WHERE session_id = %s
                  AND priority_kind != 'silent'
            """
        params: list = [session_id]
        if since_ts:
            sql += " AND created_at > %s"
            params.append(since_ts)
        sql += """
                ORDER BY dedup_key, created_at DESC
            ) sub
            ORDER BY created_at ASC
            LIMIT %s
        """
        params.append(limit)
        rows = self._fetchall(sql, tuple(params))
        # Convert to envelope format
        out = []
        for r in rows:
            try:
                payload = json.loads(r.get("payload", "{}")) if isinstance(r.get("payload"), str) else (r.get("payload") or {})
            except (ValueError, TypeError):
                payload = {}
            out.append({
                "msg_id": r.get("id", ""),
                "type": r.get("msg_type", ""),
                "subType": r.get("subtype", ""),
                "sessionId": r.get("session_id", ""),
                "timestamp": str(r.get("created_at", "")),
                "payload": payload,
                "priority": r.get("priority_kind", "normal"),
                "priorityKind": r.get("priority_kind", "normal"),
            })
        return out

    def mark_message_delivered(self, msg_id: str, session_id: str):
        self._execute(
            "UPDATE ws_messages SET is_delivered = true, delivered_at = %s WHERE id = %s",
            (_now(), msg_id),
        )

    def mark_message_apns_sent(self, msg_id: str):
        self._execute("UPDATE ws_messages SET apns_sent_at = %s WHERE id = %s", (_now(), msg_id))

    def get_undelivered_messages(self, agent_id: str, session_id: str,
                                  since_msg_id: str = "", hours: int = 24,
                                  limit: int = 500, user_id: str = None) -> list[dict]:
        params = [session_id]
        sql = """SELECT * FROM ws_messages
                 WHERE session_id = %s AND is_delivered = false
                 AND created_at > NOW() - INTERVAL '%s hours'"""
        params.append(str(hours))
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        if since_msg_id:
            sql += " AND id > %s"
            params.append(since_msg_id)
        sql += " ORDER BY created_at LIMIT %s"
        params.append(limit)
        return self._fetchall(sql, tuple(params))

    def save_ws_message(self, agent_id: str, session_id: str, seq: int, role: str,
                        type_: str, subtype: str = "", payload: dict = None,
                        priority: int = 0) -> int:
        """旧版 WS 消息保存（向后兼容）。"""
        msg_id = _uuid("msg")
        self._execute(
            """INSERT INTO ws_messages (id, session_id, user_id, seq, direction, msg_type, subtype, payload, priority_kind)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'normal')""",
            (msg_id, session_id, "anonymous", seq, role if role == "user" else "outbound", type_, subtype,
             json.dumps(payload or {}, ensure_ascii=False)),
        )
        return msg_id

    def get_ws_messages_for_replay(self, agent_id: str, session_id: str,
                                    since_seq: int = 0, limit: int = 200) -> list[dict]:
        return self._fetchall(
            """SELECT * FROM ws_messages
               WHERE session_id = %s AND seq > %s ORDER BY seq LIMIT %s""",
            (session_id, since_seq, limit),
        )

    def get_last_user_seq(self, agent_id: str, session_id: str) -> int:
        row = self._fetchone(
            "SELECT MAX(seq) as mx FROM ws_messages WHERE session_id = %s AND direction = 'inbound'",
            (session_id,),
        )
        return row["mx"] if row and row["mx"] else 0

    def get_history_count(self, agent_id: str, session_id: str) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) as cnt FROM ws_messages WHERE session_id = %s",
            (session_id,),
        )
        return row["cnt"] if row else 0

    # ═══════════════════════════════════════════════════════════════
    # 设备令牌 (APNs)
    # ═══════════════════════════════════════════════════════════════

    def register_device_token(self, agent_id: str, device_token: str,
                               platform: str = "ios", bundle_id: str = "",
                               user_id: str = "anonymous") -> int:
        # 保留在 ws_messages 同一逻辑中，使用 agent_id 映射到 user
        self._execute(
            """INSERT INTO ws_messages (id, session_id, user_id, seq, direction, msg_type, subtype, payload, priority_kind)
               VALUES (%s, %s, %s, 0, 'system', 'device_register', %s, %s::jsonb, 'normal')""",
            (_uuid("msg"), "system", user_id, platform,
             json.dumps({"device_token": device_token, "bundle_id": bundle_id, "platform": platform}, ensure_ascii=False)),
        )
        return 1

    def get_active_device_tokens(self, agent_id: str) -> list[dict]:
        return self._fetchall(
            """SELECT payload->>'device_token' AS device_token, payload->>'platform' AS platform,
               payload->>'bundle_id' AS bundle_id
               FROM ws_messages WHERE msg_type = 'device_register' AND created_at > NOW() - INTERVAL '90 days'""",
        )

    def deactivate_device_token(self, agent_id: str, device_token: str):
        self._execute(
            "DELETE FROM ws_messages WHERE msg_type = 'device_register' AND payload->>'device_token' = %s",
            (device_token,),
        )

    # ═══════════════════════════════════════════════════════════════
    # Agent 事件
    # ═══════════════════════════════════════════════════════════════

    def record_agent_event(self, agent_id: str, session_id: str, correlation_id: str,
                           event_type: str, payload: dict, user_id: str = "anonymous") -> int:
        ev_id = _uuid("ev")
        self._execute(
            """INSERT INTO agent_events (id, user_id, agent_id, event_type, payload)
               VALUES (%s, %s, %s, %s, %s::jsonb)""",
            (ev_id, user_id, agent_id, event_type, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return 1

    def get_events_by_correlation(self, correlation_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM agent_events WHERE payload->>'correlation_id' = %s ORDER BY created_at",
            (correlation_id,),
        )

    def get_pending_correlations(self, agent_id: str) -> list[str]:
        rows = self._fetchall(
            "SELECT DISTINCT payload->>'correlation_id' AS cid FROM agent_events WHERE agent_id = %s AND event_type = 'pending'",
            (agent_id,),
        )
        return [r["cid"] for r in rows if r["cid"]]

    # ═══════════════════════════════════════════════════════════════
    # 视频
    # ═══════════════════════════════════════════════════════════════

    def save_video_result(self, project_id: str, video_id: str, url: str,
                           platform: str = "", title: str = "", duration_seconds: int = 0,
                           uploader: str = "", summary: str = None, analysis: str = None,
                           local_path: str = "", transcript_text: str = None,
                           user_id: str = "anonymous") -> str:
        vid = video_id or _uuid("vid")
        self._execute(
            """INSERT INTO videos (id, user_id, url, platform, title, duration_sec, transcript, summary, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'done')""",
            (vid, user_id, url, platform, title, duration_seconds, transcript_text, summary),
        )
        return vid

    def get_video_result(self, video_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM videos WHERE id = %s", (video_id,))

    def list_video_results(self, project_id: str, limit: int = 20, user_id: str = None) -> list[dict]:
        if user_id:
            return self._fetchall(
                "SELECT * FROM videos WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
        return self._fetchall("SELECT * FROM videos ORDER BY created_at DESC LIMIT %s", (limit,))

    # ═══════════════════════════════════════════════════════════════
    # 订阅管理
    # ═══════════════════════════════════════════════════════════════

    def create_subscription(self, name: str, keywords: str, sources: list = None,
                            interval_hours: int = 24, max_papers_per_check: int = 5,
                            user_id: str = "anonymous") -> str:
        sub_id = _uuid("sub")
        sources_json = json.dumps(sources or ["arxiv", "semantic_scholar"])
        self._execute(
            """INSERT INTO subscriptions (id, user_id, query, source, frequency, max_papers_per_check)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (sub_id, user_id, keywords, sources_json,
             "daily" if interval_hours <= 24 else "weekly", max_papers_per_check),
        )
        return sub_id

    def get_subscription(self, subscription_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM subscriptions WHERE id = %s", (subscription_id,))

    def list_subscriptions(self, enabled_only: bool = False, user_id: str = None) -> list[dict]:
        sql = "SELECT * FROM subscriptions WHERE 1=1"
        params = []
        if enabled_only:
            sql += " AND is_active = true"
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        sql += " ORDER BY created_at DESC"
        return self._fetchall(sql, tuple(params) if params else None)

    def update_subscription(self, subscription_id: str, **kwargs) -> bool:
        if not kwargs:
            return False
        sets = [f"{k} = %s" for k in kwargs]
        self._execute(
            f"UPDATE subscriptions SET {', '.join(sets)} WHERE id = %s",
            tuple(list(kwargs.values()) + [subscription_id]),
        )
        return True

    def delete_subscription(self, subscription_id: str) -> bool:
        self._execute("DELETE FROM subscriptions WHERE id = %s", (subscription_id,))
        return True

    def set_subscription_active(self, subscription_id: str, active: bool) -> bool:
        self._execute("UPDATE subscriptions SET is_active = %s WHERE id = %s", (active, subscription_id))
        return True

    def mark_subscription_checked(self, subscription_id: str, last_paper_ids: list = None) -> bool:
        self._execute(
            "UPDATE subscriptions SET last_checked_at = %s WHERE id = %s",
            (_now(), subscription_id),
        )
        return True

    def save_subscription_result(self, subscription_id: str, paper: dict) -> int:
        sr_id = _uuid("sr")
        self._execute(
            """INSERT INTO subscription_results (id, subscription_id, paper_id, paper_title, paper_doi)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (subscription_id, paper_id) DO NOTHING""",
            (sr_id, subscription_id, paper.get("id", ""), paper.get("title", ""), paper.get("doi")),
        )
        return 1

    def get_subscription_results(self, subscription_id: str, since: str = None,
                                  limit: int = 50) -> list[dict]:
        if since:
            return self._fetchall(
                "SELECT * FROM subscription_results WHERE subscription_id = %s AND created_at > %s ORDER BY created_at DESC LIMIT %s",
                (subscription_id, since, limit),
            )
        return self._fetchall(
            "SELECT * FROM subscription_results WHERE subscription_id = %s ORDER BY created_at DESC LIMIT %s",
            (subscription_id, limit),
        )

    async def write_hallucination_event(self, event: dict):
        """Write a hallucination audit event."""
        try:
            import json as _json
            await self._execute("""
                INSERT INTO hallucination_events (session_id, event_type, severity, details, created_at)
                VALUES ($1, $2, $3, $4, NOW())
            """, event.get("session_id"), event.get("event_type"),
               event.get("severity", "info"),
               event.get("details", "{}"))
        except Exception:
            pass  # Best-effort audit
