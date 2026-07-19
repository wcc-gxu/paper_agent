-- ============================================================
-- Paper Agent v4.1 — 增量迁移脚本 (保留现有数据)
-- 适用于已有旧数据库，不执行 down -v 清库
-- 执行: docker exec -i $(docker compose ps -q postgres) psql -U paper_admin -d paper_search < scripts/migrate_v41.sql
-- ============================================================

-- 添加 _schema_meta 元数据表
CREATE TABLE IF NOT EXISTS _schema_meta (
    key     TEXT PRIMARY KEY,
    value   JSONB NOT NULL
);
INSERT INTO _schema_meta (key, value) VALUES
    ('embedding', '{"model": "doubao-embedding", "dim": 1024, "provider": "volcano"}'),
    ('version', '{"schema": "4.1", "applied_at": "2026-07-18"}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- agents 表治理
ALTER TABLE agents ALTER COLUMN state SET DEFAULT 'stopped';

-- users 表去掉 api_token (保留列避免旧代码崩溃，置 NULL)
UPDATE users SET password_hash = '' WHERE password_hash IS NULL;

-- sessions 表加字段
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS document_id TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary JSONB DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- documents 表加版本字段
ALTER TABLE documents ADD COLUMN IF NOT EXISTS versions JSONB DEFAULT '[]';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS current_version INTEGER DEFAULT 0;

-- 新建 event_logs (不删旧表，数据由应用层后续迁移)
CREATE TABLE IF NOT EXISTS event_logs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    duration_ms     INTEGER,
    error_text      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_el_user ON event_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_el_type ON event_logs(event_type);

-- 新建 share_requests
CREATE TABLE IF NOT EXISTS share_requests (
    id              TEXT PRIMARY KEY,
    from_user_id    TEXT NOT NULL REFERENCES users(id),
    to_user_id      TEXT NOT NULL REFERENCES users(id),
    resource_type   TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    message         TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_share_from ON share_requests(from_user_id);
CREATE INDEX IF NOT EXISTS idx_share_to ON share_requests(to_user_id);

-- captures 表补 embedding 列
ALTER TABLE captures ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- glossary_terms 表补 embedding 列
ALTER TABLE glossary_terms ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- 更新 _schema_meta 版本标记
UPDATE _schema_meta SET value = jsonb_set(value, '{schema}', '"4.1"') WHERE key = 'version';
