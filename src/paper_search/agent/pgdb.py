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

    def get_user(self, user_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM users WHERE id = %s", (user_id,))

    def get_user_by_token(self, token: str) -> Optional[dict]:
        """Deprecated: v4.1 去掉 api_token，仅 JWT 认证。保留向后兼容。"""
        return None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """按用户名查找用户（用于登录）。"""
        return self._fetchone(
            "SELECT id, username, display_name, password_hash, role, is_active FROM users WHERE username = %s",
            (username,),
        )

    def create_user(self, username: str, display_name: str,
                    role: str = "researcher", password_hash: str = None) -> str:
        user_id = f"user-{_uuid('')[4:16]}"
        self._execute(
            """INSERT INTO users (id, username, display_name, role, password_hash)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_id, username, display_name, role, password_hash),
        )
        return user_id

    def set_user_role(self, user_id: str, role: str) -> None:
        self._execute(
            "UPDATE users SET role = %s, updated_at = now() WHERE id = %s",
            (role, user_id),
        )

    def count_user_papers(self, user_id: str) -> int:
        """获取用户的论文总数（用于冷启动检测）。"""
        cur = self._raw_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM papers WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]

    # ═══════════════════════════════════════════════════════════════
    # 智能体管理 (v3.2 多智能体)
    # ═══════════════════════════════════════════════════════════════

    def list_user_agents(self, user_id: str, include_inactive: bool = False) -> list[dict]:
        """列出用户的所有智能体。v4.1: is_active → state."""
        extra = "" if include_inactive else " AND state != 'stopped'"
        return self._fetchall(
            f"SELECT * FROM agents WHERE user_id = %s{extra} ORDER BY created_at",
            (user_id,),
        )

    def get_agent(self, agent_id: str) -> Optional[dict]:
        """获取单个智能体配置."""
        return self._fetchone("SELECT * FROM agents WHERE id = %s", (agent_id,))

    def get_default_agent(self, user_id: str) -> Optional[dict]:
        """获取用户的默认智能体（最早创建且非 stopped 状态）。v4.1: 去掉 agent_type/is_active。"""
        return self._fetchone(
            "SELECT * FROM agents WHERE user_id = %s AND state != 'stopped' ORDER BY created_at LIMIT 1",
            (user_id,),
        )

    def create_agent(
        self,
        user_id: str,
        system_prompt: str = "",
        llm_provider: str = "deepseek",
    ) -> str:
        """创建智能体，返回 agent_id。v4.1: 去掉 name/display_name/agent_type 冗余列。"""
        agent_id = _uuid("agent")
        self._execute(
            """INSERT INTO agents (id, user_id, system_prompt, llm_provider)
               VALUES (%s, %s, %s, %s)""",
            (agent_id, user_id, system_prompt, llm_provider),
        )
        return agent_id

    def update_agent(self, agent_id: str, user_id: str = None, **kwargs) -> bool:
        """更新智能体配置。user_id 可选用于权限校验."""
        if not kwargs:
            return False
        allowed = {"system_prompt", "llm_provider", "state"}
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
        """软删除智能体。v4.1: 改为设 state='stopped'。"""
        return self.update_agent(agent_id, user_id=user_id, state="stopped")

    def agent_belongs_to_user(self, agent_id: str, user_id: str) -> bool:
        """验证智能体是否属于该用户."""
        row = self._fetchone(
            "SELECT 1 FROM agents WHERE id = %s AND user_id = %s",
            (agent_id, user_id),
        )
        return row is not None

    # ═══════════════════════════════════════════════════════════════
    # v4.0 文档管理
    # ═══════════════════════════════════════════════════════════════

    def create_document(self, user_id: str, title: str, file_path: str = "",
                        is_auto_review: bool = False) -> str:
        doc_id = _uuid("doc")
        self._execute(
            """INSERT INTO documents (id, user_id, title, file_path, is_auto_review)
               VALUES (%s, %s, %s, %s, %s)""",
            (doc_id, user_id, title, file_path, is_auto_review),
        )
        return doc_id

    def get_document(self, doc_id: str, user_id: str = None) -> Optional[dict]:
        if user_id:
            return self._fetchone(
                "SELECT * FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id))
        return self._fetchone("SELECT * FROM documents WHERE id = %s", (doc_id,))

    def list_documents(self, user_id: str, search: str = "",
                       limit: int = 50) -> list[dict]:
        if search:
            return self._fetchall(
                "SELECT * FROM documents WHERE user_id = %s AND title ILIKE %s ORDER BY updated_at DESC LIMIT %s",
                (user_id, f"%{search}%", limit))
        return self._fetchall(
            "SELECT * FROM documents WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
            (user_id, limit))

    def update_document(self, doc_id: str, user_id: str = None, **kwargs) -> int:
        if not kwargs:
            return 0
        kwargs["updated_at"] = _now()
        sets = [f"{k} = %s" for k in kwargs]
        values = list(kwargs.values())
        where = "id = %s"
        params = values + [doc_id]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        self._execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE {where}", tuple(params))
        return 1

    def delete_document(self, doc_id: str, user_id: str = None) -> bool:
        if user_id:
            self._execute("DELETE FROM documents WHERE id = %s AND user_id = %s",
                         (doc_id, user_id))
        else:
            self._execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        return True

    def get_document_current_version(self, doc_id: str) -> Optional[int]:
        row = self._fetchone(
            "SELECT MAX(version_number) as v FROM document_versions WHERE document_id = %s",
            (doc_id,))
        return row["v"] if row else None

    def create_document_version(self, document_id: str, content: str,
                                 trigger: str = "manual_commit",
                                 session_id: str = None) -> str:
        current = self.get_document_current_version(document_id) or 0
        ver_num = current + 1
        ver_id = _uuid("ver")
        self._execute(
            """INSERT INTO document_versions (id, document_id, version_number, content, trigger, session_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (ver_id, document_id, ver_num, content, trigger, session_id),
        )
        return ver_id

    def get_document_version(self, ver_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM document_versions WHERE id = %s", (ver_id,))

    def get_document_version_by_number(self, doc_id: str, ver_num: int) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM document_versions WHERE document_id = %s AND version_number = %s",
            (doc_id, ver_num))

    def list_document_versions(self, doc_id: str, limit: int = 20) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM document_versions WHERE document_id = %s ORDER BY version_number DESC LIMIT %s",
            (doc_id, limit))

    def revert_document(self, doc_id: str, ver_id: str, user_id: str = None) -> str:
        ver = self.get_document_version(ver_id)
        if not ver:
            raise ValueError(f"Version not found: {ver_id}")
        return self.create_document_version(doc_id, ver["content"],
                                            trigger="rollback",
                                            session_id=str(ver_id))

    # ═══════════════════════════════════════════════════════════════
    # v4.0 用户偏好 (基于 user_preferences 表)
    # ═══════════════════════════════════════════════════════════════

    def get_v4_preferences(self, user_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM user_preferences WHERE user_id = %s", (user_id,))

    def upsert_v4_preferences(self, user_id: str, **kwargs) -> None:
        allowed = {"research_domain", "writing_style", "language_pref", "mentor_quotes", "other"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        row = self.get_v4_preferences(user_id)
        if row:
            updates["updated_at"] = _now()
            sets = [f"{k} = %s" for k in updates]
            self._execute(
                f"UPDATE user_preferences SET {', '.join(sets)} WHERE user_id = %s",
                tuple(list(updates.values()) + [user_id]))
        else:
            self._execute(
                """INSERT INTO user_preferences (user_id, research_domain, writing_style,
                   language_pref, mentor_quotes, other)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                (user_id,
                 kwargs.get("research_domain", ""),
                 kwargs.get("writing_style", "APA"),
                 kwargs.get("language_pref", "zh"),
                 kwargs.get("mentor_quotes", ""),
                 json.dumps(kwargs.get("other", {}))),
            )

    # ═══════════════════════════════════════════════════════════════
    # v4.0 知识共享
    # ═══════════════════════════════════════════════════════════════

    def create_share_request(self, from_user_id: str, to_user_id: str,
                             resource_type: str, resource_id: str,
                             message: str = "") -> str:
        share_id = _uuid("shr")
        self._execute(
            """INSERT INTO share_requests (id, from_user_id, to_user_id, resource_type,
               resource_id, message)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (share_id, from_user_id, to_user_id, resource_type, resource_id, message),
        )
        return share_id

    def get_share_request(self, share_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM share_requests WHERE id = %s", (share_id,))

    def list_share_requests(self, user_id: str, direction: str = "inbound") -> list[dict]:
        if direction == "outbound":
            return self._fetchall(
                "SELECT * FROM share_requests WHERE from_user_id = %s ORDER BY created_at DESC",
                (user_id,))
        return self._fetchall(
            "SELECT * FROM share_requests WHERE to_user_id = %s ORDER BY created_at DESC",
            (user_id,))

    def update_share_request(self, share_id: str, status: str) -> bool:
        self._execute(
            "UPDATE share_requests SET status = %s, updated_at = now() WHERE id = %s",
            (status, share_id))
        return True

    # ═══════════════════════════════════════════════════════════════
    # 用户偏好 (基于 user_configs 表 — 兼容旧版)
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
