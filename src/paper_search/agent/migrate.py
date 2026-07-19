"""数据库迁移工具 — API 启动时自动按版本增量迁移。

版本管理:
  每个 SQL 文件头包含 -- @version X.Y.Z 注释（必填）
  执行时比较当前 _schema_meta.version，只执行 > 当前版本的迁移
  执行完成后更新 _schema_meta.version

迁移文件名格式: NNN_description.sql（NNN 决定执行顺序，文件名不变不重跑）
版本仅由文件头 -- @version 注释决定

使用:
    from paper_search.agent.migrate import run_migrations, current_version
    run_migrations(db)
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "scripts" / "migrations"
# Resolves to: <project_root>/scripts/migrations/

VERSION_RE = re.compile(r'--\s*@version\s+(\d+)\.(\d+)\.(\d+)')


def _parse_version(v: str) -> tuple[int, int, int]:
    """解析 '4.1.0' → (4, 1, 0)。兼容 '4.1' → (4, 1, 0)。"""
    match = VERSION_RE.search(v)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    parts = v.strip().split(".")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1]), 0
    return int(parts[0]), int(parts[1]), int(parts[2])


def _extract_version(sql_content: str) -> tuple[int, int, int]:
    """从 SQL 文件内容中提取 @version 注解。"""
    match = VERSION_RE.search(sql_content)
    if not match:
        raise ValueError("Migration file missing -- @version X.Y.Z header")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def current_version(db) -> tuple[int, int, int]:
    """查询当前数据库 schema 版本。"""
    row = db._fetchone("SELECT value FROM _schema_meta WHERE key = 'version'")
    if row and row.get("value"):
        val = row["value"]
        if isinstance(val, dict):
            v = val.get("schema", "0.0.0")
        elif isinstance(val, str):
            v = json.loads(val).get("schema", "0.0.0")
        else:
            return (0, 0, 0)
        return _parse_version(v)
    return (0, 0, 0)


def run_migrations(db) -> list[str]:
    """按版本增量执行迁移 SQL 文件，返回已执行的文件名列表。"""
    if not MIGRATIONS_DIR.exists():
        return []

    # 确保 _schema_meta 存在
    db._execute(
        "CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL)"
    )

    # 当前版本
    ver = current_version(db)
    logger.info("Current DB schema version: %d.%d.%d", *ver)

    # 收集所有迁移文件，解析版本
    sql_files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    migrations = []
    for fname in sql_files:
        filepath = MIGRATIONS_DIR / fname
        content = filepath.read_text(encoding="utf-8")
        try:
            fver = _extract_version(content)
        except ValueError as e:
            logger.warning("Skipping %s: %s", fname, e)
            continue
        migrations.append((fname, fver, content))

    # 只执行版本 > 当前版本的迁移
    pending = [(n, v, c) for n, v, c in migrations if v > ver]
    if not pending:
        logger.info("DB up-to-date (v%d.%d.%d)", *ver)
        return []

    pending.sort(key=lambda x: x[1])  # 按版本排序
    logger.info("Running %d pending migrations (current v%d.%d.%d)",
                len(pending), *ver)

    max_version = ver
    for fname, fver, sql in pending:
        try:
            logger.info("Migration: %s → v%d.%d.%d", fname, *fver)
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                stmt = stmt.strip()
                if not stmt or stmt.startswith("--"):
                    continue
                db._execute(stmt + ";")
            max_version = fver
        except Exception as e:
            logger.error("Migration %s failed at v%d.%d.%d: %s",
                        fname, *fver, e, exc_info=True)
            raise

    # 更新版本标记
    new_ver = f"{max_version[0]}.{max_version[1]}.{max_version[2]}"
    db._execute(
        "INSERT INTO _schema_meta (key, value) VALUES ('version', %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps({"schema": new_ver}),),
    )
    logger.info("Migrations complete. DB version: %s", new_ver)
    return [n for n, _, _ in pending]
