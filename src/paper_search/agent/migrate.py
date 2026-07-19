"""数据库迁移工具 — API 启动时自动执行增量 SQL 迁移。

从 scripts/migrations/ 目录读取 .sql 文件，按文件名排序执行。
已执行的迁移记录在 _schema_meta 表中，不会重复执行。

使用:
    from paper_search.agent.migrate import run_migrations
    await run_migrations(db)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "scripts" / "migrations"
# Resolves to: <project_root>/scripts/migrations/


def run_migrations(db) -> list[str]:
    """自动检测并执行待迁移 SQL 文件，返回已执行的文件名列表。

    同步方法 — 在 FastAPI lifespan 中调用。
    """
    if not MIGRATIONS_DIR.exists():
        return []

    # 确保 _schema_meta 存在
    db._execute(
        "CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL)"
    )

    # 读取已执行的迁移
    row = db._fetchone("SELECT value FROM _schema_meta WHERE key = 'migrations'")
    if row and row.get("value"):
        applied = set(json.loads(row["value"]) if isinstance(row["value"], str) else row["value"])
    else:
        applied = set()
        db._execute(
            "INSERT INTO _schema_meta (key, value) VALUES ('migrations', '[]'::jsonb)"
        )

    # 收集待执行的 SQL 文件
    sql_files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    pending = [f for f in sql_files if f not in applied]
    if not pending:
        return []

    logger.info("Running %d pending migrations: %s", len(pending), pending)

    for fname in pending:
        filepath = MIGRATIONS_DIR / fname
        try:
            sql = filepath.read_text(encoding="utf-8")
            logger.info("Migration: %s", fname)
            # Split by semicolons; execute each statement separately (psycopg2)
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                if stmt.startswith("--"):
                    continue
                db._execute(stmt + ";")
            applied.add(fname)
        except Exception as e:
            logger.error("Migration %s failed: %s", fname, e)
            raise

    # 更新已执行标记
    db._execute(
        "UPDATE _schema_meta SET value = %s WHERE key = 'migrations'",
        (json.dumps(list(applied)),),
    )
    logger.info("Migrations complete")
    return pending
