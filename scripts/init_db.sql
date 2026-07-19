-- ============================================================
-- Paper Agent v4.1 — 数据库初始化脚本 (17 张表, 28 索引)
-- PostgreSQL 16+ + pgvector 0.8+
-- 日期: 2026-07-18
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- ID 生成函数
-- ============================================================

CREATE OR REPLACE FUNCTION gen_agent_id() RETURNS TEXT AS $$
BEGIN RETURN 'agent-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gen_doc_id() RETURNS TEXT AS $$
BEGIN RETURN 'doc-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gen_ver_id() RETURNS TEXT AS $$
BEGIN RETURN 'ver-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gen_share_id() RETURNS TEXT AS $$
BEGIN RETURN 'shr-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

-- ============================================================
-- 1. users — 用户
-- ============================================================

CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'researcher',
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. agents — Agent 配置 (1:1 user)
-- ============================================================

CREATE TABLE agents (
    id              TEXT PRIMARY KEY,
    user_id         TEXT UNIQUE NOT NULL REFERENCES users(id),
    system_prompt   TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'stopped',
    llm_provider    TEXT NOT NULL DEFAULT 'deepseek',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_agents_user ON agents(user_id);
CREATE INDEX idx_agents_active ON agents(user_id, state);

-- ============================================================
-- 3. papers — 论文 (figures/archives 存为 JSONB)
-- ============================================================

CREATE TABLE papers (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    authors         JSONB NOT NULL DEFAULT '[]',
    year            INTEGER,
    venue           TEXT,
    venue_type      TEXT,
    doi             TEXT,
    arxiv_id        TEXT,
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        JSONB NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL,
    file_path       TEXT,
    md_path         TEXT,
    citation_count  INTEGER NOT NULL DEFAULT 0,
    tl_dr           TEXT,
    status          TEXT NOT NULL DEFAULT 'ingested',
    reading_level   TEXT,
    digest          TEXT,
    figures         JSONB DEFAULT '[]',
    archives        JSONB DEFAULT '{}',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_papers_user ON papers(user_id);
CREATE INDEX idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE INDEX idx_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX idx_papers_fts ON papers USING gin(to_tsvector('english', title || ' ' || abstract));

-- ============================================================
-- 4. projects — 项目
-- ============================================================

CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    domain          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_projects_user ON projects(user_id);

-- ============================================================
-- 5. project_papers — 项目-论文关联 (N:M)
-- ============================================================

CREATE TABLE project_papers (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, paper_id)
);

-- ============================================================
-- 6. paper_chunks — 论文向量切片 (pgvector)
-- ============================================================

CREATE TABLE paper_chunks (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    chunk_text      TEXT NOT NULL,
    chunk_type      TEXT NOT NULL DEFAULT 'body',
    section_title   TEXT,
    chunk_order     INTEGER NOT NULL,
    embedding       vector(1024),
    token_count     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chunks_paper ON paper_chunks(paper_id);
CREATE INDEX idx_chunks_user ON paper_chunks(user_id);

-- ============================================================
-- 7. captures — 碎片知识 (含 embedding，参与统一 RAG)
-- ============================================================

CREATE TABLE captures (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    capture_type    TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT,
    source_url      TEXT,
    tags            JSONB NOT NULL DEFAULT '[]',
    embedding       vector(1024),
    status          TEXT NOT NULL DEFAULT 'active',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_captures_user ON captures(user_id);
CREATE INDEX idx_captures_type ON captures(user_id, capture_type);
CREATE INDEX idx_captures_fts ON captures USING gin(to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')));

-- ============================================================
-- 8. glossary_terms — 术语库 (含 embedding)
-- ============================================================

CREATE TABLE glossary_terms (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    en_term         TEXT NOT NULL,
    zh_term         TEXT NOT NULL,
    variants        JSONB NOT NULL DEFAULT '[]',
    domain          TEXT NOT NULL DEFAULT '',
    df              INTEGER NOT NULL DEFAULT 0,
    user_verified   BOOLEAN NOT NULL DEFAULT false,
    embedding       vector(1024),
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, en_term, zh_term)
);
CREATE INDEX idx_glossary_user ON glossary_terms(user_id);
CREATE INDEX idx_glossary_domain ON glossary_terms(user_id, domain);
CREATE INDEX idx_glossary_fts ON glossary_terms USING gin(to_tsvector('english', en_term));

-- ============================================================
-- 9. journal_ranks — 期刊/会议分级
-- ============================================================

CREATE TABLE journal_ranks (
    id              TEXT PRIMARY KEY,
    venue           TEXT NOT NULL UNIQUE,
    rank            TEXT NOT NULL,
    source          TEXT NOT NULL,
    year            INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 10. documents — 文档 (含版本历史 JSONB)
-- ============================================================

CREATE TABLE documents (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    is_auto_review  BOOLEAN DEFAULT FALSE,
    versions        JSONB DEFAULT '[]',
    current_version INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_documents_user ON documents(user_id, updated_at DESC);

-- ============================================================
-- 11. sessions — 会话 (summary / document_id 存为 JSONB)
-- ============================================================

CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '新会话',
    status          TEXT NOT NULL DEFAULT 'active',
    document_id     TEXT REFERENCES documents(id),
    summary         JSONB DEFAULT '{}',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_thread ON sessions(user_id, thread_id);

-- ============================================================
-- 12. ws_messages — WebSocket 消息 (Outbox 持久化)
-- ============================================================

-- ============================================================
-- 13. user_preferences — 用户偏好
-- ============================================================

CREATE TABLE user_preferences (
    user_id         TEXT PRIMARY KEY REFERENCES users(id),
    research_domain TEXT DEFAULT '',
    writing_style   TEXT DEFAULT 'APA',
    language_pref   TEXT DEFAULT 'zh',
    mentor_quotes   TEXT DEFAULT '',
    other           JSONB DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 14. subscriptions — 订阅 (含结果 JSONB)
-- ============================================================

CREATE TABLE subscriptions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    query           TEXT NOT NULL,
    sources         JSONB NOT NULL DEFAULT '["arxiv"]',
    frequency       TEXT NOT NULL DEFAULT 'daily',
    max_papers      INTEGER NOT NULL DEFAULT 20,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    results         JSONB DEFAULT '[]',
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_subscriptions_user ON subscriptions(user_id, is_active);

-- ============================================================
-- 15. share_requests — 细粒度共享
-- ============================================================

CREATE TABLE share_requests (
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
CREATE INDEX idx_share_from ON share_requests(from_user_id);
CREATE INDEX idx_share_to ON share_requests(to_user_id);

-- ============================================================
-- 16. hallucination_events — 反幻觉审计
-- ============================================================

CREATE TABLE hallucination_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT,
    event_type      TEXT NOT NULL,
    llm_output      TEXT NOT NULL,
    expected_output TEXT,
    verified_result JSONB NOT NULL DEFAULT '{}',
    action_taken    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_he_user ON hallucination_events(user_id, created_at DESC);
CREATE INDEX idx_he_type ON hallucination_events(event_type);

-- ============================================================
-- 17. event_logs — 通用事件日志
--     合并: search_logs + rag_traces + agent_events + external_validations
-- ============================================================

CREATE TABLE event_logs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    duration_ms     INTEGER,
    error_text      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_el_user ON event_logs(user_id, created_at DESC);
CREATE INDEX idx_el_type ON event_logs(event_type);

-- ============================================================
-- _schema_meta — Embedding 元数据
-- ============================================================

CREATE TABLE _schema_meta (
    key     TEXT PRIMARY KEY,
    value   JSONB NOT NULL
);
INSERT INTO _schema_meta (key, value) VALUES
    ('embedding', '{"model": "doubao-embedding", "dim": 1024, "provider": "volcano"}'),
    ('version', '{"schema": "4.1", "applied_at": "2026-07-18"}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- ============================================================
-- pgvector HNSW 索引 (数据量 > 1000 后启用)
-- ============================================================

-- CREATE INDEX idx_chunks_embedding_hnsw ON paper_chunks
--     USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 16, ef_construction = 200);
-- CREATE INDEX idx_captures_embedding_hnsw ON captures
--     USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 16, ef_construction = 200);
-- CREATE INDEX idx_glossary_embedding_hnsw ON glossary_terms
--     USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 12, ef_construction = 100);

-- ============================================================
-- 统一 RAG 检索函数
-- ============================================================

CREATE OR REPLACE FUNCTION search_unified(
    query_embedding vector(1024),
    p_user_id TEXT,
    match_count INT DEFAULT 10,
    similarity_threshold REAL DEFAULT 0.5
) RETURNS TABLE (
    source TEXT,
    chunk_id TEXT,
    content TEXT,
    title TEXT,
    similarity REAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT 'paper'::TEXT, pc.id, pc.chunk_text, p.title,
           1 - (pc.embedding <=> query_embedding) AS similarity
    FROM paper_chunks pc
    JOIN papers p ON pc.paper_id = p.id
    WHERE pc.user_id = p_user_id
      AND pc.embedding <=> query_embedding < (1.0 - similarity_threshold)
    UNION ALL
    SELECT 'capture'::TEXT, c.id, coalesce(c.content,''), c.title,
           1 - (c.embedding <=> query_embedding) AS similarity
    FROM captures c
    WHERE c.user_id = p_user_id
      AND c.status = 'active'
      AND c.embedding <=> query_embedding < (1.0 - similarity_threshold)
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION search_terms(
    query_embedding vector(1024),
    p_user_id TEXT,
    match_count INT DEFAULT 5
) RETURNS TABLE (
    glossary_term_id TEXT,
    en_term TEXT,
    zh_term TEXT,
    similarity REAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT gt.id, gt.en_term, gt.zh_term,
           1 - (gt.embedding <=> query_embedding) AS similarity
    FROM glossary_terms gt
    WHERE gt.user_id = p_user_id
    ORDER BY gt.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 默认数据：创建默认用户
-- ============================================================

INSERT INTO users (id, username, display_name, password_hash, role)
VALUES ('user-default', 'default', '默认用户', 'legacy_no_password', 'researcher')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 完成
-- ============================================================
