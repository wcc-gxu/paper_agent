-- ============================================================
-- 智驭·研 v3 数据库初始化脚本
-- PostgreSQL 16+ + pgvector 0.8+
-- 对应文档: docs/development/database-architecture.md
-- 日期: 2026-07-10
-- ============================================================

-- 启用扩展
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- Section 2: 业务表 (22 张)
-- ============================================================

-- 2.2 用户与认证
CREATE TABLE users (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    api_token   TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    role        TEXT NOT NULL DEFAULT 'researcher',
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_configs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    config_key  TEXT NOT NULL,
    config_value JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, config_key)
);

CREATE INDEX idx_users_api_token ON users(api_token);

-- 2.3 项目与论文
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_user ON projects(user_id);

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
    source_priority INTEGER NOT NULL DEFAULT 30,
    file_path       TEXT,
    md_path         TEXT,
    figures_dir     TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    citation_count  INTEGER NOT NULL DEFAULT 0,
    tl_dr           TEXT,
    status          TEXT NOT NULL DEFAULT 'ingested',
    duplicate_of    TEXT,
    alternate_versions JSONB DEFAULT '[]',
    method_tags     JSONB DEFAULT '[]',
    dataset_info    JSONB DEFAULT '{}',
    code_url        TEXT,
    reading_level   TEXT,
    digest          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_papers_title_fts ON papers USING gin(to_tsvector('english', title));
CREATE INDEX idx_papers_abstract_fts ON papers USING gin(to_tsvector('english', abstract));
CREATE INDEX idx_papers_user ON papers(user_id);
CREATE INDEX idx_papers_venue ON papers(venue);
CREATE INDEX idx_papers_year ON papers(year);
CREATE INDEX idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE INDEX idx_papers_arxiv_id ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;

CREATE TABLE project_papers (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id    TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, paper_id)
);

CREATE INDEX idx_pp_project ON project_papers(project_id);
CREATE INDEX idx_pp_paper ON project_papers(paper_id);

-- 2.4 引用关系
CREATE TABLE citations (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES papers(id),
    cited_paper_id  TEXT REFERENCES papers(id) ON DELETE SET NULL,
    cited_doi       TEXT,
    cited_title     TEXT,
    cited_authors   JSONB,
    cited_year      INTEGER,
    citation_context TEXT,
    classification  TEXT DEFAULT 'unknown',
    confidence      REAL NOT NULL DEFAULT 0.0,
    verified        BOOLEAN NOT NULL DEFAULT false,
    verified_by     TEXT,
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_citations_paper ON citations(paper_id);
CREATE INDEX idx_citations_cited ON citations(cited_paper_id) WHERE cited_paper_id IS NOT NULL;

-- 2.5 会话与消息
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '新会话',
    status          TEXT NOT NULL DEFAULT 'active',
    metadata        JSONB NOT NULL DEFAULT '{}',
    message_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sessions_user_thread ON sessions(user_id, thread_id);

CREATE TABLE ws_messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    seq             INTEGER NOT NULL,
    direction       TEXT NOT NULL,
    msg_type        TEXT NOT NULL,
    subtype         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    priority_kind   TEXT NOT NULL DEFAULT 'normal',
    is_delivered    BOOLEAN NOT NULL DEFAULT false,
    is_replay       BOOLEAN NOT NULL DEFAULT false,
    msg_id          TEXT,
    correlation_id  TEXT,
    delivered_at    TIMESTAMPTZ,
    delivered_sessions TEXT[],
    apns_sent_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ws_session ON ws_messages(session_id);
CREATE INDEX idx_ws_user ON ws_messages(user_id);
CREATE INDEX idx_ws_created ON ws_messages(created_at);
CREATE INDEX idx_ws_session_seq ON ws_messages(session_id, seq);

-- 消息去重: 同一 session + tool_call_id 只保留最新状态
CREATE UNIQUE INDEX IF NOT EXISTS idx_ws_tool_dedup
    ON ws_messages (session_id, (payload->>'tool_call_id'))
    WHERE msg_type = 'tool' AND payload->>'tool_call_id' IS NOT NULL;

-- 消息去重: 同一 session + plan_id 只保留最新 plan_todo_update
CREATE UNIQUE INDEX IF NOT EXISTS idx_ws_plan_dedup
    ON ws_messages (session_id, (payload->>'plan_id'))
    WHERE msg_type = 'plan_todo_update' AND payload->>'plan_id' IS NOT NULL;

-- 消息向量嵌入 (Phase 4)
CREATE TABLE message_embeddings (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    msg_id       TEXT NOT NULL,
    user_id      TEXT NOT NULL REFERENCES users(id),
    content_text TEXT NOT NULL,
    embedding    vector(1024),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_msg_emb_session ON message_embeddings(session_id);
CREATE INDEX idx_msg_emb_user ON message_embeddings(user_id);
-- [DISABLED] IVFFlat 近似索引在数据量 < 1000 条时会因 probes 不足返回 0 结果。
-- 待 paper_chunks 超过 5000 条后再启用，并设置 ivfflat.probes = 20。
-- CREATE INDEX idx_msg_emb_ivf ON message_embeddings
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

CREATE TABLE conversation_archive (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    summary_text    TEXT NOT NULL,
    summary_type    TEXT NOT NULL DEFAULT 'rolling',
    start_msg_id    TEXT,
    end_msg_id      TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_archive_session ON conversation_archive(session_id);

-- 2.6 Agent 任务
CREATE TABLE agent_tasks (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    mode            TEXT NOT NULL DEFAULT 'full_ingest',
    name            TEXT NOT NULL DEFAULT '',
    agent_name      TEXT NOT NULL,
    task_kind       TEXT NOT NULL,
    celery_task_id  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    progress        JSONB DEFAULT '{}',
    arguments       JSONB NOT NULL DEFAULT '{}',
    result          JSONB,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_user ON agent_tasks(user_id);
CREATE INDEX idx_tasks_status ON agent_tasks(status);
CREATE INDEX idx_tasks_agent ON agent_tasks(agent_name);

CREATE TABLE task_steps (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id),
    step_order      INTEGER NOT NULL,
    step_name       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    detail          JSONB DEFAULT '{}',
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_steps_task ON task_steps(task_id);

-- 2.7 术语词表
CREATE TABLE glossary_terms (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    en_term         TEXT NOT NULL,
    zh_term         TEXT NOT NULL,
    variants        JSONB NOT NULL DEFAULT '[]',
    domain          TEXT NOT NULL DEFAULT '',
    source_paper_ids JSONB NOT NULL DEFAULT '[]',
    tf              REAL NOT NULL DEFAULT 0.0,
    df              INTEGER NOT NULL DEFAULT 0,
    cross_doc_score REAL NOT NULL DEFAULT 0.0,
    llm_confidence  REAL NOT NULL DEFAULT 0.0,
    user_verified   BOOLEAN NOT NULL DEFAULT false,
    last_seen_at    TIMESTAMPTZ,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, en_term, zh_term)
);

CREATE INDEX idx_glossary_user ON glossary_terms(user_id);
CREATE INDEX idx_glossary_domain ON glossary_terms(user_id, domain);
CREATE INDEX idx_glossary_en ON glossary_terms(en_term);

-- 2.8 写作模板
CREATE TABLE writing_templates (
    id              TEXT PRIMARY KEY,
    user_id         TEXT,
    name            TEXT NOT NULL,
    venue           TEXT,
    domain          TEXT NOT NULL DEFAULT '',
    section_type    TEXT NOT NULL,
    structure_json  JSONB NOT NULL,
    sample_text     TEXT,
    source_paper_ids JSONB DEFAULT '[]',
    is_system       BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_templates_venue ON writing_templates(venue);
CREATE INDEX idx_templates_user ON writing_templates(user_id) WHERE user_id IS NOT NULL;

-- 2.9 外部验证缓存
CREATE TABLE external_validations (
    id              TEXT PRIMARY KEY,
    identifier_type TEXT NOT NULL,
    identifier      TEXT NOT NULL,
    validation_result JSONB NOT NULL,
    validated_by    TEXT NOT NULL,
    validated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (identifier_type, identifier)
);

CREATE INDEX idx_ev_expires ON external_validations(expires_at);

-- 2.10 反幻觉事件
CREATE TABLE hallucination_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    citation_id     TEXT,
    event_type      TEXT NOT NULL,
    llm_output      TEXT NOT NULL,
    expected_output TEXT,
    verified_result JSONB NOT NULL,
    action_taken    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_he_user ON hallucination_events(user_id);
CREATE INDEX idx_he_type ON hallucination_events(event_type);
CREATE INDEX idx_he_created ON hallucination_events(created_at);

-- 2.11 基础设施
CREATE TABLE search_logs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    query           TEXT NOT NULL,
    source          TEXT NOT NULL,
    result_count    INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_search_user ON search_logs(user_id);
CREATE INDEX idx_search_created ON search_logs(created_at);

CREATE TABLE journal_ranks (
    id              TEXT PRIMARY KEY,
    venue           TEXT NOT NULL UNIQUE,
    rank            TEXT NOT NULL,
    source          TEXT NOT NULL,
    year            INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE videos (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    url             TEXT NOT NULL,
    platform        TEXT,
    title           TEXT,
    duration_sec    INTEGER,
    transcript      TEXT,
    summary         TEXT,
    keywords        JSONB DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_videos_user ON videos(user_id);

-- 2.12 碎片知识采集 (Capture Agent)
CREATE TABLE captures (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    capture_type    TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT,
    source_url      TEXT,
    tags            JSONB NOT NULL DEFAULT '[]',
    transcript      TEXT,
    summary         TEXT,
    keywords        JSONB DEFAULT '[]',
    embedding       vector(1024),
    status          TEXT NOT NULL DEFAULT 'active',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_captures_user ON captures(user_id);
CREATE INDEX idx_captures_type ON captures(user_id, capture_type);
CREATE INDEX idx_captures_created ON captures(created_at);

-- 2.13 订阅
CREATE TABLE subscriptions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    query           TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'arxiv',
    frequency       TEXT NOT NULL DEFAULT 'daily',
    max_papers_per_check INTEGER NOT NULL DEFAULT 20,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription_results (
    id              TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    paper_id        TEXT,
    paper_title     TEXT,
    paper_doi       TEXT,
    is_new          BOOLEAN NOT NULL DEFAULT true,
    notified        BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sub_results_sub ON subscription_results(subscription_id);

-- 2.14 Agent 事件
CREATE TABLE agent_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ae_agent ON agent_events(agent_id);
CREATE INDEX idx_ae_created ON agent_events(created_at);

-- 课题组共享 (预留 Phase 2)
CREATE TABLE research_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE group_members (
    group_id    TEXT NOT NULL REFERENCES research_groups(id),
    user_id     TEXT NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL DEFAULT 'member',
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE shared_papers (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES papers(id),
    shared_by       TEXT NOT NULL REFERENCES users(id),
    shared_with     TEXT,
    shared_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Section 3: pgvector 向量存储 (4 张)
-- ============================================================

-- 3.2 论文切片向量表
CREATE TABLE paper_chunks (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    chunk_text      TEXT NOT NULL,
    chunk_type      TEXT NOT NULL DEFAULT 'body',
    section_title   TEXT,
    section_level   INTEGER,
    chunk_order     INTEGER NOT NULL,
    embedding       vector(1024),
    token_count     INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 业务索引
CREATE INDEX idx_chunks_paper ON paper_chunks(paper_id);
CREATE INDEX idx_chunks_user ON paper_chunks(user_id);
CREATE INDEX idx_chunks_type ON paper_chunks(chunk_type);

-- 3.3 术语词表向量表
CREATE TABLE glossary_embeddings (
    id              TEXT PRIMARY KEY,
    glossary_term_id TEXT NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
    term_text       TEXT NOT NULL,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3.4 会话与主题摘要向量表
CREATE TABLE session_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    summary_text    TEXT NOT NULL,
    summary_type    TEXT NOT NULL,
    embedding       vector(1024),
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ssum_user ON session_summaries(user_id);
CREATE INDEX idx_ssum_session ON session_summaries(session_id);

CREATE TABLE topic_embeddings (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    topic_name      TEXT NOT NULL,
    description     TEXT NOT NULL,
    embedding       vector(1024),
    paper_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_topic_user ON topic_embeddings(user_id);

-- ============================================================
-- pgvector 索引 (迁移后统一创建，先迁数据再建索引)
-- ============================================================

-- [DISABLED] IVFFlat 近似索引在数据量 < 1000 条时会因 probes 不足返回 0 结果。
-- 待 paper_chunks 超过 5000 条后再启用，并在每次搜索前执行:
--   SET ivfflat.probes = 20;
-- CREATE INDEX idx_chunks_embedding_ivf
--     ON paper_chunks
--     USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);

-- [DISABLED] 同上，数据量不足时禁用。
-- CREATE INDEX idx_gle_embedding_ivf
--     ON glossary_embeddings
--     USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 50);

-- ============================================================
-- pgvector 检索函数
-- ============================================================

-- 论文切片检索函数
CREATE OR REPLACE FUNCTION search_paper_chunks(
    query_embedding vector(1024),
    p_user_id TEXT,
    match_count INT DEFAULT 10,
    similarity_threshold REAL DEFAULT 0.5
) RETURNS TABLE (
    chunk_id TEXT,
    paper_id TEXT,
    chunk_text TEXT,
    section_title TEXT,
    similarity REAL
) AS $$
DECLARE
    max_distance REAL := 1.0 - similarity_threshold;
BEGIN
    RETURN QUERY
    SELECT
        pc.id,
        pc.paper_id,
        pc.chunk_text,
        pc.section_title,
        1 - (pc.embedding <=> query_embedding) AS similarity
    FROM paper_chunks pc
    WHERE pc.user_id = p_user_id
        AND pc.embedding <=> query_embedding < max_distance
    ORDER BY pc.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;

-- 术语模糊匹配函数
CREATE OR REPLACE FUNCTION search_glossary(
    query_text TEXT,
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
    SELECT
        gt.id,
        gt.en_term,
        gt.zh_term,
        1 - (ge.embedding <=> query_embedding) AS similarity
    FROM glossary_embeddings ge
    JOIN glossary_terms gt ON ge.glossary_term_id = gt.id
    WHERE gt.user_id = p_user_id
    ORDER BY ge.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 默认数据：创建默认用户（存量数据归属）
-- ============================================================
INSERT INTO users (id, username, display_name, api_token, role)
VALUES ('user-default', 'default', '默认用户', 'tok-migrated-default', 'researcher')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 完成
-- ============================================================
-- 验证:
--   SELECT count(*) FROM information_schema.tables WHERE table_schema='public';
--   SELECT extname, extversion FROM pg_extension WHERE extname='vector';
