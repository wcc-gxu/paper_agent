-- v4.1 增量迁移：旧 schema → 新 schema（保留现有数据）
-- 适用: 已部署旧数据库，不执行 down -v 清库

-- _schema_meta 元数据
CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL);

-- agents: 补充新列
ALTER TABLE agents ADD COLUMN IF NOT EXISTS state TEXT DEFAULT 'stopped';

-- users: 补充必要字段
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT '';

-- sessions: 补充新列
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS document_id TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary JSONB DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- documents: 版本管理
ALTER TABLE documents ADD COLUMN IF NOT EXISTS versions JSONB DEFAULT '[]';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS current_version INTEGER DEFAULT 0;

-- ws_messages: 新字段
ALTER TABLE ws_messages ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal';

-- captures: 语义检索
ALTER TABLE captures ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- glossary_terms: 语义检索
ALTER TABLE glossary_terms ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- papers: 图表+归档 JSONB
ALTER TABLE papers ADD COLUMN IF NOT EXISTS figures JSONB DEFAULT '[]';
ALTER TABLE papers ADD COLUMN IF NOT EXISTS archives JSONB DEFAULT '{}';

-- 新建 event_logs
CREATE TABLE IF NOT EXISTS event_logs (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT, event_type TEXT NOT NULL, payload JSONB DEFAULT '{}',
    duration_ms INTEGER, error_text TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_el_user ON event_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_el_type ON event_logs(event_type);

-- 新建 share_requests
CREATE TABLE IF NOT EXISTS share_requests (
    id TEXT PRIMARY KEY, from_user_id TEXT NOT NULL REFERENCES users(id),
    to_user_id TEXT NOT NULL REFERENCES users(id), resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL, message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_share_from ON share_requests(from_user_id);
CREATE INDEX IF NOT EXISTS idx_share_to ON share_requests(to_user_id);
