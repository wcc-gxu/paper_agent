#!/usr/bin/env python3
"""SQLite + ChromaDB → PostgreSQL + pgvector 数据迁移脚本.

v3 Phase 1: 单用户 → 多用户迁移。
所有存量数据统一分配 user_id="user-default"。

用法:
    python scripts/migrate_to_postgres.py --phase analyze
    python scripts/migrate_to_postgres.py --phase sqlite
    python scripts/migrate_to_postgres.py --phase chroma
    python scripts/migrate_to_postgres.py --phase store
    python scripts/migrate_to_postgres.py --phase verify
    python scripts/migrate_to_postgres.py --phase all
    python scripts/migrate_to_postgres.py --phase all --dry-run

环境变量:
    DATABASE_URL: PostgreSQL 连接字符串
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")

DEFAULT_USER_ID = "user-default"
DEFAULT_USER_TOKEN = "tok-migrated-default"
BATCH_SIZE = 500


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _json_to_pg_array(val) -> str:
    """Convert a JSON array string (e.g. '[\"a\",\"b\"]') to PG array literal (e.g. '{a,b}')."""
    if val is None:
        return "{}"
    if isinstance(val, list):
        items = [str(x).replace(",", "\\,").replace("{", "\\{").replace("}", "\\}") for x in val]
        return "{" + ",".join(items) + "}"
    if isinstance(val, str):
        val = val.strip()
        if val == "" or val == "[]":
            return "{}"
        try:
            arr = json.loads(val)
            if isinstance(arr, list):
                items = [str(x).replace(",", "\\,").replace("{", "\\{").replace("}", "\\}") for x in arr]
                return "{" + ",".join(items) + "}"
        except (json.JSONDecodeError, TypeError):
            pass
    return "{}"


def _safe_json(v):
    """Ensure value is a JSON string suitable for jsonb columns."""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        try:
            json.loads(v)
            return v
        except (json.JSONDecodeError, TypeError):
            return json.dumps([v], ensure_ascii=False)
    if v is None:
        return "[]"
    return json.dumps(v, ensure_ascii=False)


def _fmt_vector(embedding) -> str:
    if embedding is None:
        return None
    if isinstance(embedding, list):
        return "[" + ",".join(str(x) for x in embedding) + "]"
    if hasattr(embedding, "tolist"):
        return "[" + ",".join(str(x) for x in embedding.tolist()) + "]"
    raise ValueError(f"Unknown embedding type: {type(embedding)}")


def _infer_venue_type(row: dict) -> str:
    if row.get("journal") or row.get("venue"):
        return "journal"
    if row.get("conference"):
        return "conference"
    if row.get("arxiv_id"):
        return "preprint"
    return "other"


def _source_priority(row: dict) -> int:
    s = (row.get("source") or "").lower()
    if s == "arxiv":
        return 20
    if s in ("ieee", "acm", "springer", "elsevier"):
        return 40
    return 30


# ═══════════════════════════════════════════════════════════════
# SQLite access
# ═══════════════════════════════════════════════════════════════

def _sqlite_rows(db_path: Path, table: str) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    except sqlite3.OperationalError as e:
        logger.warning(f"  Skip {table}: {e}")
        return []
    finally:
        conn.close()


def _sqlite_count(db_path: Path, table: str) -> int:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# PostgreSQL access
# ═══════════════════════════════════════════════════════════════

def _pg_connect(dsn: str):
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def _pg_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _pg_insert(conn, table: str, rows: list[dict], dry_run: bool = False) -> int:
    """Insert rows one at a time with ON CONFLICT DO NOTHING.

    Each row is its own statement so one bad row doesn't block the rest.
    We use conn.rollback() after errors to keep the transaction alive.
    """
    if not rows:
        return 0

    if dry_run:
        logger.info(f"  [DRY-RUN] Would insert {len(rows)} rows into {table}")
        return len(rows)

    columns = list(rows[0].keys())
    col_list = ", ".join(columns)
    ph_list = ", ".join(["%s"] * len(columns))

    pk_map = {
        "projects": "id", "papers": "id", "project_papers": "id",
        "search_logs": "id", "journal_ranks": "id",
        "citations": "id", "agent_tasks": "id", "task_steps": "id",
        "sessions": "id", "ws_messages": "id", "agent_events": "id",
        "videos": "id", "subscriptions": "id",
        "users": "id", "user_configs": "id",
        "paper_chunks": "id", "glossary_embeddings": "id",
        "subscription_results": "id",
        "store_data": "id", "checkpoints": "checkpoint_id",
        "checkpoint_blobs": "id", "checkpoint_writes": "id",
        "conversation_archive": "id",
    }
    pk = pk_map.get(table, "id")
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({ph_list}) ON CONFLICT ({pk}) DO NOTHING"

    cur = conn.cursor()
    inserted = 0
    for row in rows:
        vals = tuple(row.get(c) for c in columns)
        try:
            cur.execute(sql, vals)
            inserted += cur.rowcount
        except Exception as e:
            conn.rollback()
            logger.debug(f"  Row insert skip: {e}")

    conn.commit()
    return inserted


def _pg_insert_vectors(conn, table: str, rows: list[dict], dry_run: bool = False) -> int:
    """Insert vector rows — embedding column needs ::vector cast."""
    if not rows:
        return 0
    if dry_run:
        logger.info(f"  [DRY-RUN] Would insert {len(rows)} vectors into {table}")
        return len(rows)

    cur = conn.cursor()
    inserted = 0

    for row in rows:
        emb_str = row.pop("_embedding_raw", None)
        if emb_str is None:
            continue

        cols = list(row.keys())
        # Build VALUES with explicit ::vector cast for the embedding column
        val_parts = []
        params = []
        for c in cols:
            if c == "embedding" and emb_str:
                val_parts.append(f"'{emb_str}'::vector")
            else:
                val_parts.append("%s")
                params.append(row.get(c))

        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(val_parts)}) ON CONFLICT (id) DO NOTHING"
        try:
            cur.execute(sql, params)
            inserted += cur.rowcount
        except Exception as e:
            conn.rollback()
            logger.debug(f"  Vector insert skip: {e}")

    conn.commit()
    return inserted


# ═══════════════════════════════════════════════════════════════
# Row mappers — SQLite old schema → PostgreSQL v3 schema
# ═══════════════════════════════════════════════════════════════

def _map_project(row: dict) -> dict:
    pid = str(row.get("id", ""))
    return {
        "id": pid,
        "user_id": DEFAULT_USER_ID,
        "name": str(row.get("user_query", "")[:200]),
        "description": str(row.get("parsed_intent", "")),
        "domain": "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("created_at"),  # SQLite 无 updated_at
    }


def _map_paper(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "user_id": DEFAULT_USER_ID,
        "title": str(row.get("title", "")),
        "authors": _safe_json(row.get("authors", "[]")),
        "year": row.get("year"),
        "venue": row.get("venue") or row.get("journal") or row.get("conference"),
        "venue_type": _infer_venue_type(row),
        "doi": row.get("doi"),
        "arxiv_id": row.get("arxiv_id"),
        "abstract": str(row.get("abstract") or ""),
        "keywords": _safe_json(row.get("keywords", "[]")),
        "source": str(row.get("source", "manual")),
        "source_priority": _source_priority(row),
        "file_path": row.get("pdf_path"),
        "md_path": row.get("markdown_path") or row.get("md_path"),
        "figures_dir": row.get("figures_dir"),
        "metadata": _safe_json(row.get("metadata", "{}")),
        "citation_count": int(row.get("citation_count") or 0),
        "tl_dr": row.get("digest") or row.get("tl_dr"),
        "status": str(row.get("status") or "ingested"),
        "duplicate_of": row.get("duplicate_of"),
        "alternate_versions": _safe_json(row.get("alternate_versions", "[]")),
        "method_tags": _safe_json(row.get("method_tags", "[]")),
        "dataset_info": _safe_json(row.get("dataset_info", "{}")),
        "code_url": row.get("code_url"),
        "reading_level": row.get("reading_level"),
        "digest": row.get("digest"),
        "created_at": row.get("created_at") or row.get("first_seen_at"),
        "updated_at": row.get("updated_at"),
    }


def _map_project_paper(row: dict) -> dict:
    # SQLite project_papers has NO id column — generate one
    pid = str(row.get("project_id", ""))
    pap_id = str(row.get("paper_id", ""))
    return {
        "id": f"pp-{pid}-{pap_id}",
        "project_id": pid,
        "paper_id": pap_id,
        "added_at": row.get("created_at") or "now()",
    }


def _map_search_log(row: dict) -> dict:
    import uuid
    return {
        "id": f"slog-{uuid.uuid4().hex[:12]}",
        "user_id": DEFAULT_USER_ID,
        "query": str(row.get("query", "")),
        "source": str(row.get("source", "")),
        "result_count": int(row.get("results_count") or 0),
        "duration_ms": row.get("duration_ms"),
        "created_at": row.get("created_at"),
    }


def _map_journal_rank(row: dict) -> dict:
    # SQLite journal_ranks: venue_name, ccf_level, sci_zone, unified_level, updated_at (NO id)
    venue = str(row.get("venue_name", ""))
    rank_val = row.get("unified_level") or row.get("ccf_level") or row.get("sci_zone") or ""
    return {
        "id": f"jrank-{venue.replace(' ', '_').lower()}",
        "venue": venue,
        "rank": str(rank_val),
        "source": "migration",
        "year": None,
        "created_at": row.get("updated_at"),
    }


def _map_citation(row: dict) -> dict:
    import uuid
    return {
        "id": f"cit-{uuid.uuid4().hex[:12]}",
        "paper_id": str(row.get("source_paper_id") or row.get("paper_id", "")),
        "cited_paper_id": row.get("target_paper_id") or row.get("cited_paper_id"),
        "cited_doi": row.get("target_doi") or row.get("cited_doi") or row.get("doi"),
        "cited_title": str(row.get("target_title") or row.get("cited_title") or row.get("title") or ""),
        "cited_authors": _safe_json(row.get("target_authors") or row.get("cited_authors", "[]")),
        "cited_year": row.get("cited_year") or row.get("year"),
        "citation_context": row.get("context_text") or row.get("citation_context") or row.get("context"),
        "classification": str(row.get("relation_type") or row.get("classification") or "unknown"),
        "confidence": float(row.get("confidence") or 0.5),
        "verified": bool(row.get("is_verified") or row.get("verified", False)),
        "created_at": row.get("created_at"),
    }


def _map_agent_task(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "user_id": DEFAULT_USER_ID,
        "mode": str(row.get("mode") or "full_ingest"),
        "name": str(row.get("name") or ""),
        "agent_name": "main",
        "task_kind": "general",
        "celery_task_id": None,
        "status": str(row.get("status", "pending")),
        "progress": "{}",
        "arguments": _safe_json(row.get("plan_json", "{}")),
        "result": "{}",
        "error_message": None,
        "started_at": None,
        "completed_at": row.get("completed_at"),
        "created_at": row.get("created_at"),
    }


def _map_task_step(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "task_id": str(row.get("task_id", "")),
        "step_order": int(row.get("step_order") or row.get("step_index", 0)),
        "step_name": str(row.get("step_name", "")),
        "status": str(row.get("status", "pending")),
        "detail": _safe_json(row.get("detail", "{}")),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "created_at": row.get("created_at"),
    }


def _map_session(row: dict) -> dict:
    # SQLite sessions: agent_id, session_id, title, created_at, updated_at (NO id)
    agent = str(row.get("agent_id", "agent-001"))
    sess = str(row.get("session_id", ""))
    return {
        "id": f"sess-{agent}-{sess}",
        "user_id": DEFAULT_USER_ID,
        "agent_id": agent,
        "thread_id": sess,
        "title": str(row.get("title") or "新对话"),
        "status": "active",
        "metadata": "{}",
        "message_count": 0,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _map_ws_message(row: dict) -> dict:
    # SQLite ws_messages: id (int), agent_id, session_id, seq, role, type, subtype,
    #   payload, priority (int), created_at, is_replay (int), msg_id, correlation_id,
    #   priority_kind, delivered_at, delivered_sessions, apns_sent_at
    role = str(row.get("role", "assistant"))
    direction = "inbound" if role == "user" else "outbound"
    payload = row.get("payload", "{}")
    if isinstance(payload, str):
        try:
            json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = json.dumps({"text": str(payload)}, ensure_ascii=False)
    elif isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)

    agent = str(row.get("agent_id", "agent-001"))
    sess = str(row.get("session_id", ""))
    # Build a session_id FK that matches what sessions mapper generates
    pg_session_id = f"sess-{agent}-{sess}"

    return {
        "id": f"msg-{row.get('id', '')}",
        "session_id": pg_session_id,
        "user_id": DEFAULT_USER_ID,
        "seq": int(row.get("seq") or 0),
        "direction": direction,
        "msg_type": str(row.get("type", "text")),
        "subtype": row.get("subtype"),
        "payload": payload,
        "priority_kind": str(row.get("priority_kind") or "normal"),
        "is_delivered": bool(row.get("is_delivered", False)),
        "is_replay": bool(int(row.get("is_replay") or 0)),
        "msg_id": row.get("msg_id"),
        "correlation_id": row.get("correlation_id"),
        "delivered_at": row.get("delivered_at") or None,
        "delivered_sessions": _json_to_pg_array(row.get("delivered_sessions")),
        "apns_sent_at": row.get("apns_sent_at") or None,
        "created_at": row.get("created_at"),
    }


def _map_agent_event(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "user_id": DEFAULT_USER_ID,
        "agent_id": str(row.get("agent_id", "agent-001")),
        "event_type": str(row.get("event_type", "")),
        "payload": _safe_json(row.get("payload") or row.get("data", "{}")),
        "created_at": row.get("created_at"),
    }


def _map_video(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "user_id": DEFAULT_USER_ID,
        "url": str(row.get("url", "")),
        "platform": str(row.get("platform") or ""),
        "title": str(row.get("title") or ""),
        "duration_sec": row.get("duration_sec"),
        "transcript": row.get("transcript") or row.get("transcript_path"),
        "summary": row.get("summary") or row.get("summary_path"),
        "keywords": _safe_json(row.get("keywords", "[]")),
        "status": str(row.get("status", "pending")),
        "metadata": _safe_json(row.get("metadata", "{}")),
        "created_at": row.get("created_at"),
    }


def _map_subscription(row: dict) -> dict:
    return {
        "id": str(row.get("id", "")),
        "user_id": DEFAULT_USER_ID,
        "query": str(row.get("keywords") or row.get("query", "")),
        "source": str(row.get("sources") or row.get("source", "arxiv")),
        "frequency": str(row.get("frequency") or "daily"),
        "max_papers_per_check": int(row.get("max_papers_per_check") or 20),
        "is_active": bool(row.get("is_active", True)),
        "last_checked_at": row.get("last_checked_at"),
        "created_at": row.get("created_at"),
    }


def _map_device_config(row: dict) -> dict:
    return {
        "id": f"cfg-apns-{row.get('id', '')}",
        "user_id": DEFAULT_USER_ID,
        "config_key": "apns_device_token",
        "config_value": json.dumps({
            "agent_id": row.get("agent_id", "agent-001"),
            "device_token": row.get("device_token", ""),
            "platform": row.get("platform", "ios"),
            "bundle_id": row.get("bundle_id", ""),
        }, ensure_ascii=False),
    }


def _map_subscription_result(row: dict) -> dict:
    r = dict(row)
    r.setdefault("user_id", DEFAULT_USER_ID)
    return r


# Table mapping: (sqlite_table, pg_table, mapper_fn)
SQLITE_TABLES: list[tuple[str, str, callable]] = [
    ("projects",           "projects",           _map_project),
    ("papers",             "papers",             _map_paper),
    ("project_papers",     "project_papers",     _map_project_paper),
    ("search_logs",        "search_logs",        _map_search_log),
    ("journal_ranks",      "journal_ranks",      _map_journal_rank),
    ("citations",          "citations",          _map_citation),
    ("agent_tasks",        "agent_tasks",        _map_agent_task),
    ("task_steps",         "task_steps",         _map_task_step),
    ("sessions",           "sessions",           _map_session),
    ("ws_messages",        "ws_messages",        _map_ws_message),
    ("agent_events",       "agent_events",       _map_agent_event),
    ("videos",             "videos",             _map_video),
    ("subscriptions",      "subscriptions",      _map_subscription),
    ("device_tokens",      "user_configs",       _map_device_config),
    ("subscription_results","subscription_results", _map_subscription_result),
]

STORE_TABLES = [
    "store_data", "checkpoints", "checkpoint_blobs",
    "checkpoint_writes", "conversation_archive",
]

CHROMA_MAP: list[tuple[str, str, str]] = [
    ("papers_abstract",  "paper_chunks",       "abstract"),
    ("papers_fulltext",  "paper_chunks",       "body"),
    ("glossary_terms",   "glossary_embeddings", None),
]


# ═══════════════════════════════════════════════════════════════
# Migration Runner
# ═══════════════════════════════════════════════════════════════

class MigrationRunner:
    def __init__(self, sqlite_path: Path, pg_dsn: str, chroma_path: Path,
                 dry_run: bool = False):
        self.sqlite_path = sqlite_path
        self.pg_dsn = pg_dsn
        self.chroma_path = chroma_path
        self.dry_run = dry_run
        self._conn = None

    @property
    def pg(self):
        if self._conn is None and not self.dry_run:
            self._conn = _pg_connect(self.pg_dsn)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── analyze ──────────────────────────────────

    def run_analyze(self):
        logger.info("=" * 60)
        logger.info("Phase: ANALYZE")
        logger.info("=" * 60)

        if not self.sqlite_path.exists():
            logger.error(f"SQLite not found: {self.sqlite_path}")
            return

        total = 0
        logger.info("\nSQLite tables:")
        for src, dst, _ in SQLITE_TABLES:
            n = _sqlite_count(self.sqlite_path, src)
            total += n
            logger.info(f"  {src:25s} → {dst:25s}  {n:>8d} rows")

        logger.info(f"  {'':25s}   {'':25s}  {'─' * 10}")
        logger.info(f"  {'':25s}   {'':25s}  {total:>8d} total")

        # ChromaDB
        total_vec = 0
        logger.info("\nChromaDB collections:")
        try:
            import chromadb
            c = chromadb.PersistentClient(path=str(self.chroma_path))
            for col_name, dst, _ in CHROMA_MAP:
                try:
                    n = c.get_collection(col_name).count()
                    total_vec += n
                    logger.info(f"  {col_name:30s} → {dst:25s}  {n:>8d} vectors")
                except Exception as e:
                    logger.warning(f"  {col_name:30s}   SKIP: {e}")
        except ImportError:
            logger.warning("  chromadb not installed")
        except Exception as e:
            logger.warning(f"  ChromaDB error: {e}")

        est = (total / 50000) * 5 + (total_vec / 10000) * 10
        logger.info(f"\nEstimated: ~{est:.0f} min  ({total} rows + {total_vec} vectors)")

    # ── sqlite ───────────────────────────────────

    def run_sqlite(self, resume_from: str = None):
        logger.info("=" * 60)
        logger.info("Phase: SQLITE → PostgreSQL")
        logger.info("=" * 60)

        if not self.sqlite_path.exists():
            logger.error(f"SQLite not found: {self.sqlite_path}")
            return

        # Ensure default user exists
        self._ensure_default_user()

        skipped = resume_from is not None
        for src, dst, mapper in SQLITE_TABLES:
            if skipped:
                if src == resume_from:
                    skipped = False
                else:
                    logger.info(f"  Skip {src} (resume_from={resume_from})")
                    continue

            rows = _sqlite_rows(self.sqlite_path, src)
            if not rows:
                logger.info(f"  [{src} → {dst}] empty, skip")
                continue

            mapped = [mapper(r) for r in rows]
            n = _pg_insert(self.pg, dst, mapped, dry_run=self.dry_run)
            logger.info(f"  [{src} → {dst}] {len(rows)} source → {n} inserted")

    def _ensure_default_user(self):
        if self.dry_run:
            return
        cur = self.pg.cursor()
        cur.execute(
            """INSERT INTO users (id, username, display_name, api_token, role)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (DEFAULT_USER_ID, DEFAULT_USER_ID, "默认用户", DEFAULT_USER_TOKEN, "researcher"),
        )
        self.pg.commit()
        logger.info(f"  Default user: {DEFAULT_USER_ID}")

    # ── chroma ───────────────────────────────────

    def run_chroma(self):
        logger.info("=" * 60)
        logger.info("Phase: ChromaDB → pgvector")
        logger.info("=" * 60)

        try:
            import chromadb
        except ImportError:
            logger.error("chromadb not installed")
            return

        if not self.chroma_path.exists():
            logger.warning(f"ChromaDB path not found: {self.chroma_path}")
            return

        client = chromadb.PersistentClient(path=str(self.chroma_path))

        for col_name, dst_table, chunk_type in CHROMA_MAP:
            logger.info(f"  [{col_name} → {dst_table}]")
            try:
                col = client.get_collection(col_name)
            except Exception as e:
                logger.warning(f"    Not found: {e}")
                continue

            total = col.count()
            if total == 0:
                logger.info(f"    empty, skip")
                continue

            migrated = 0
            for offset in range(0, total, BATCH_SIZE):
                batch = col.get(
                    limit=BATCH_SIZE, offset=offset,
                    include=["embeddings", "metadatas", "documents"],
                )
                ids = batch.get("ids", [])
                embs = batch.get("embeddings", [])
                metas = batch.get("metadatas", [])
                docs = batch.get("documents", [])

                rows = []
                for i in range(len(ids)):
                    emb = embs[i] if i < len(embs) else None
                    meta = metas[i] if i < len(metas) else {}
                    doc = docs[i] if i < len(docs) else ""
                    if emb is None:
                        continue

                    rows.append({
                        "id": str(ids[i]),
                        "paper_id": str(meta.get("paper_id", "unknown")),
                        "user_id": DEFAULT_USER_ID,
                        "chunk_text": doc or "",
                        "chunk_type": chunk_type or meta.get("type", "body"),
                        "section_title": meta.get("section"),
                        "section_level": meta.get("section_level"),
                        "chunk_order": meta.get("chunk_order", offset + i),
                        "token_count": len((doc or "").split()),
                        "metadata": json.dumps(meta, ensure_ascii=False),
                        "created_at": "now()",
                        "_embedding_raw": _fmt_vector(emb),
                    })

                n = _pg_insert_vectors(self.pg, dst_table, rows, dry_run=self.dry_run)
                migrated += n
                logger.info(f"    {min(offset + BATCH_SIZE, total)}/{total}")

            logger.info(f"    {total} source → {migrated} inserted")

        # Vector indexes
        if not self.dry_run:
            self._create_indexes()

    def _create_indexes(self):
        logger.info("  Creating vector indexes...")
        cur = self.pg.cursor()
        for s in [
            "CREATE INDEX IF NOT EXISTS idx_paper_chunks_emb ON paper_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
            "CREATE INDEX IF NOT EXISTS idx_glossary_embs_emb ON glossary_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50)",
        ]:
            try:
                cur.execute(s)
                logger.info(f"    {s[:60]}...")
            except Exception as e:
                logger.warning(f"    Index skip: {e}")
        self.pg.commit()

    # ── store ────────────────────────────────────

    def run_store(self):
        logger.info("=" * 60)
        logger.info("Phase: LangGraph Store")
        logger.info("=" * 60)

        if not self.sqlite_path.exists():
            logger.error(f"SQLite not found: {self.sqlite_path}")
            return

        for t in STORE_TABLES:
            rows = _sqlite_rows(self.sqlite_path, t)
            if not rows:
                logger.info(f"  [{t}] empty, skip")
                continue
            mapped = [dict(r) for r in rows]
            n = _pg_insert(self.pg, t, mapped, dry_run=self.dry_run)
            logger.info(f"  [{t}] {len(rows)} source → {n} inserted")

    # ── verify ───────────────────────────────────

    def run_verify(self):
        logger.info("=" * 60)
        logger.info("Phase: VERIFY")
        logger.info("=" * 60)

        errors = []

        logger.info("\nRow count comparison:")
        for src, dst, _ in SQLITE_TABLES:
            sc = _sqlite_count(self.sqlite_path, src)
            dc = _pg_count(self.pg, dst) if not self.dry_run else sc
            diff = sc - dc
            mark = "✅" if diff == 0 else "❌"
            logger.info(f"  {mark} {src:25s}  SQLite: {sc:>8d}  PG: {dc:>8d}  diff: {diff:+d}")
            if diff != 0:
                errors.append(f"{src}: diff={diff}")

        # Users check
        if not self.dry_run:
            cur = self.pg.cursor()
            cur.execute("SELECT id, username FROM users")
            for u in cur.fetchall():
                logger.info(f"  User: {u[0]:30s} {u[1]}")

        logger.info(f"\n{'=' * 60}")
        if errors:
            logger.error(f"❌ {len(errors)} ERROR(S):")
            for e in errors:
                logger.error(f"   - {e}")
        else:
            logger.info("✅ All counts match!")

        return errors


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def _load_dotenv():
    """Load .env file from project root."""
    for root in [Path.cwd(), Path(__file__).resolve().parent.parent]:
        env_file = root / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            logger.info(f"Loaded env from {env_file}")
            return


def main():
    _load_dotenv()
    p = argparse.ArgumentParser(description="SQLite+ChromaDB → PostgreSQL+pgvector migration")
    p.add_argument("--phase", required=True,
                   choices=["analyze", "sqlite", "chroma", "store", "verify", "all"])
    p.add_argument("--resume-from", help="Resume from table (sqlite phase)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sqlite-path")
    p.add_argument("--pg-dsn")
    p.add_argument("--chroma-path")
    args = p.parse_args()

    sqlite_path = Path(args.sqlite_path) if args.sqlite_path else (
        Path.home() / ".paper_search" / "agent.db")
    pg_dsn = args.pg_dsn or os.environ.get("DATABASE_URL", "")
    chroma_path = Path(args.chroma_path) if args.chroma_path else (
        Path.home() / ".paper_search" / "chroma")

    if args.phase in ("sqlite", "chroma", "store", "verify", "all"):
        if not pg_dsn and not args.dry_run:
            logger.error("DATABASE_URL not set")
            sys.exit(1)

    runner = MigrationRunner(sqlite_path, pg_dsn, chroma_path, dry_run=args.dry_run)

    t0 = time.time()
    try:
        phases = ["analyze", "sqlite", "chroma", "store", "verify"] if args.phase == "all" else [args.phase]

        if "analyze" in phases:
            runner.run_analyze()
        if "sqlite" in phases:
            runner.run_sqlite(resume_from=args.resume_from)
        if "chroma" in phases:
            runner.run_chroma()
        if "store" in phases:
            runner.run_store()
        if "verify" in phases:
            errs = runner.run_verify()
        else:
            errs = []
    finally:
        runner.close()

    logger.info(f"\nElapsed: {time.time() - t0:.1f}s")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
