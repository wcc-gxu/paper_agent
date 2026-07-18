# Paper Agent v4.1 — 数据库架构设计

> 数据库方案：PostgreSQL (业务数据) + pgvector (向量存储) + Redis (缓存/队列/路由)
>
> 日期：2026-07-18 | 版本：4.1 | 表数：17 | 索引：28

---

## 目录

1. [技术选型](#1-技术选型)
2. [表关系 ER 图](#2-表关系-er-图)
3. [PostgreSQL 表结构（17 张）](#3-postgresql-表结构17-张)
4. [pgvector 向量索引设计](#4-pgvector-向量索引设计)
5. [索引策略](#5-索引策略)
6. [Redis 数据结构](#6-redis-数据结构)
7. [多用户数据隔离](#7-多用户数据隔离)
8. [迁移映射（30→17 表）](#8-迁移映射3017-表)
9. [架构审查](#9-架构审查)

---

## 1. 技术选型

| 组件 | 版本 | 用途 |
|------|------|------|
| PostgreSQL | 16+ | 所有业务数据存储 |
| pgvector | 0.8+ | 向量存储与检索（HNSW） |
| Redis | 7+ | 缓存 / 消息队列 / Pub/Sub / 限流 / Agent 状态 |

### 选型理由

| 对比维度 | SQLite | PostgreSQL + pgvector |
|----------|--------|----------------------|
| 并发写入 | 单写入锁 | MVCC，天然高并发 |
| 向量检索 | 依赖外部 ChromaDB | pgvector 与业务表同一事务 |
| 全文搜索 | 基础 FTS | tsvector + GIN 索引 |
| JSON 查询 | 有限 | jsonb + GIN 索引 |
| 运维 | 单文件 | 需要备份/复制方案 |

---

## 2. 表关系 ER 图

```
users ── 1:1 ── agents           (1 用户 1 agent)
  │
  ├── 1:N ── projects ── N:M ── papers
  │             (via project_papers)
  │
  ├── 1:N ── papers              (论文元数据)
  │             ├── 1:N ── paper_chunks (pgvector 切片)
  │             └── figures/archives 存为 JSONB 字段
  │
  ├── 1:N ── captures            (碎片知识 + embedding)
  ├── 1:N ── glossary_terms      (术语库 + embedding)
  ├── 1:N ── sessions            (聊天会话)
  │             ├── 1:N ── ws_messages   (消息持久化)
  │             └── summary 存为 JSONB 字段
  │
  ├── 1:N ── documents           (AI 写作文档 + versions JSONB)
  ├── 1:N ── subscriptions       (订阅 + results JSONB)
  ├── 1:N ── share_requests      (细粒度共享)
  ├── 1:N ── hallucination_events (反幻觉审计)
  └── 1:N ── event_logs          (通用事件日志)

独立表（不关联 user_id）：
  journal_ranks                  (CCF/SCI 分级缓存)
  _schema_meta                   (embedding 模型元数据)
```

---

## 3. PostgreSQL 表结构（17 张）

### 3.1 users — 用户

```sql
CREATE TABLE users (
    id              TEXT PRIMARY KEY,                    -- user-{uuid}
    username        TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'researcher',   -- researcher | super_admin
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_username ON users(username);
```

### 3.2 agents — Agent 配置（1 用户 : 1 Agent）

```sql
CREATE TABLE agents (
    id              TEXT PRIMARY KEY,                    -- agent-{uuid}
    user_id         TEXT UNIQUE NOT NULL REFERENCES users(id),
    system_prompt   TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'stopped',      -- Supervisor 维护
    llm_provider    TEXT NOT NULL DEFAULT 'deepseek',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_agents_user ON agents(user_id);
CREATE INDEX idx_agents_active ON agents(user_id, is_active);
```

**state 状态**: `starting` | `idle` | `busy` | `stopping` | `stopped` | `crashed` | `stalled`

> `state` 字段高频更新由 Supervisor 写 Redis Hash `agent:status`，DB 仅写大变更（启动/停止/崩溃）和每 5min 定时同步。

### 3.3 papers — 论文

```sql
CREATE TABLE papers (
    id              TEXT PRIMARY KEY,                    -- doi:{doi} | arxiv:{id} | sha256:{hash}
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    authors         JSONB NOT NULL DEFAULT '[]',
    year            INTEGER,
    venue           TEXT,
    venue_type      TEXT,                                -- conference | journal | preprint
    doi             TEXT,
    arxiv_id        TEXT,
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        JSONB NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL,                       -- arxiv | semanticscholar | upload | manual
    file_path       TEXT,
    md_path         TEXT,
    citation_count  INTEGER NOT NULL DEFAULT 0,
    tl_dr           TEXT,                                -- 一句话摘要
    status          TEXT NOT NULL DEFAULT 'ingested',
    reading_level   TEXT,                                -- unread | skimmed | read | intensive
    digest          TEXT,                                -- 用户笔记
    figures         JSONB DEFAULT '[]',                  -- [{caption, page, image_hash, oss_path}]
    archives        JSONB DEFAULT '{}',                  -- {original_pdf, oss_pdf, file_size, md5}
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_papers_user ON papers(user_id);
CREATE INDEX idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE INDEX idx_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX idx_papers_fts ON papers USING gin(to_tsvector('english', title || ' ' || abstract));
```

### 3.4 projects — 项目

```sql
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,                    -- prj-{uuid}
    user_id         TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    domain          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_projects_user ON projects(user_id);
```

### 3.5 project_papers — 项目-论文关联 (N:M)

```sql
CREATE TABLE project_papers (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, paper_id)
);
```

### 3.6 paper_chunks — 论文向量切片 (pgvector)

```sql
CREATE TABLE paper_chunks (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    chunk_text      TEXT NOT NULL,
    chunk_type      TEXT NOT NULL DEFAULT 'body',        -- body | abstract | figure_caption | table
    section_title   TEXT,
    chunk_order     INTEGER NOT NULL,
    embedding       vector(1024),
    token_count     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chunks_paper ON paper_chunks(paper_id);
CREATE INDEX idx_chunks_user ON paper_chunks(user_id);
```

### 3.7 captures — 碎片知识（含 embedding，参与统一 RAG 检索）

```sql
CREATE TABLE captures (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    capture_type    TEXT NOT NULL,                       -- web_clip | experiment_note | meeting_note | audio | video
    title           TEXT NOT NULL,
    content         TEXT,
    source_url      TEXT,
    tags            JSONB NOT NULL DEFAULT '[]',
    embedding       vector(1024),                       -- 统一 RAG 检索
    status          TEXT NOT NULL DEFAULT 'active',     -- active | archived | deleted
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_captures_user ON captures(user_id);
CREATE INDEX idx_captures_type ON captures(user_id, capture_type);
CREATE INDEX idx_captures_fts ON captures USING gin(to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')));
```

### 3.8 glossary_terms — 术语库（含 embedding）

```sql
CREATE TABLE glossary_terms (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    en_term         TEXT NOT NULL,
    zh_term         TEXT NOT NULL,
    variants        JSONB NOT NULL DEFAULT '[]',
    domain          TEXT NOT NULL DEFAULT '',
    df              INTEGER NOT NULL DEFAULT 0,          -- 文档频率
    user_verified   BOOLEAN NOT NULL DEFAULT false,
    embedding       vector(1024),                       -- 术语语义向量
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, en_term, zh_term)
);
CREATE INDEX idx_glossary_user ON glossary_terms(user_id);
CREATE INDEX idx_glossary_domain ON glossary_terms(user_id, domain);
CREATE INDEX idx_glossary_fts ON glossary_terms USING gin(to_tsvector('english', en_term));
```

### 3.9 journal_ranks — 期刊/会议分级缓存

```sql
CREATE TABLE journal_ranks (
    id              TEXT PRIMARY KEY,
    venue           TEXT NOT NULL UNIQUE,
    rank            TEXT NOT NULL,                       -- CCF-A | CCF-B | CCF-C | SCI-1 | SCI-2
    source          TEXT NOT NULL,                       -- ccf | sci | custom
    year            INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.10 sessions — 会话

```sql
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,                    -- sess-{uuid}
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,
    thread_id       TEXT NOT NULL,                       -- LangGraph thread_id
    title           TEXT NOT NULL DEFAULT '新会话',
    status          TEXT NOT NULL DEFAULT 'active',     -- active | archived
    document_id     TEXT REFERENCES documents(id),       -- 绑定的写作文档
    summary         JSONB DEFAULT '{}',                  -- {rolling_summary, topic_tags}
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_thread ON sessions(user_id, thread_id);
```

### 3.11 ws_messages — WebSocket 消息（Outbox 持久化）

```sql
CREATE TABLE ws_messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    seq             INTEGER NOT NULL,
    direction       TEXT NOT NULL,                       -- inbound | outbound
    msg_type        TEXT NOT NULL,
    subtype         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    priority        TEXT NOT NULL DEFAULT 'normal',      -- silent | normal | high | urgent
    msg_id          TEXT,
    correlation_id  TEXT,
    is_delivered    BOOLEAN NOT NULL DEFAULT false,
    delivered_at    TIMESTAMPTZ,
    apns_sent_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ws_session ON ws_messages(session_id, created_at);
CREATE INDEX idx_ws_user ON ws_messages(user_id, created_at DESC);

-- 去重：同一 session 的 tool_call_id 只保留最新状态
CREATE UNIQUE INDEX idx_ws_tool_dedup
    ON ws_messages (session_id, (payload->>'tool_call_id'))
    WHERE msg_type = 'tool' AND payload->>'tool_call_id' IS NOT NULL;

-- 去重：同一 session 的 plan_id 只保留最新 plan_todo_update
CREATE UNIQUE INDEX idx_ws_plan_dedup
    ON ws_messages (session_id, (payload->>'plan_id'))
    WHERE msg_type = 'plan_todo_update' AND payload->>'plan_id' IS NOT NULL;
```

### 3.12 documents — 文档（含版本历史 JSONB）

```sql
CREATE TABLE documents (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    is_auto_review  BOOLEAN DEFAULT FALSE,
    versions        JSONB DEFAULT '[]',                  -- [{version_number, content, trigger, session_id, created_at}]
    current_version INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_documents_user ON documents(user_id, updated_at DESC);
```

> 版本管理：`manual_commit` 立即持久化；`ai_turn`/`auto_save` 每 5s 最多写一次。版本数 > 200 时自动清理旧版本，保留最近 50 条。

### 3.13 user_preferences — 用户偏好

```sql
CREATE TABLE user_preferences (
    user_id         TEXT PRIMARY KEY REFERENCES users(id),
    research_domain TEXT DEFAULT '',
    writing_style   TEXT DEFAULT 'APA',
    language_pref   TEXT DEFAULT 'zh',
    mentor_quotes   TEXT DEFAULT '',
    other           JSONB DEFAULT '{}',                  -- {writing_templates, config, ...}
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.14 subscriptions — 订阅（含结果 JSONB）

```sql
CREATE TABLE subscriptions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    query           TEXT NOT NULL,
    sources         JSONB NOT NULL DEFAULT '["arxiv"]',
    frequency       TEXT NOT NULL DEFAULT 'daily',
    max_papers      INTEGER NOT NULL DEFAULT 20,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    results         JSONB DEFAULT '[]',                  -- [{paper_id, title, doi, is_new, created_at}]
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_subscriptions_user ON subscriptions(user_id, is_active);
```

> results 保留最近 50 条在 JSONB 内，历史结果写入 `event_logs`。

### 3.15 share_requests — 细粒度共享

```sql
CREATE TABLE share_requests (
    id              TEXT PRIMARY KEY,
    from_user_id    TEXT NOT NULL REFERENCES users(id),
    to_user_id      TEXT NOT NULL REFERENCES users(id),
    resource_type   TEXT NOT NULL,                       -- paper | document | knowledge_chunk
    resource_id     TEXT NOT NULL,
    message         TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',     -- pending | accepted | rejected
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_share_from ON share_requests(from_user_id);
CREATE INDEX idx_share_to ON share_requests(to_user_id);
```

### 3.16 hallucination_events — 反幻觉审计

```sql
CREATE TABLE hallucination_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT,
    event_type      TEXT NOT NULL,                       -- false_citation | claim_mismatch | fabricated_doi
    llm_output      TEXT NOT NULL,
    expected_output TEXT,
    verified_result JSONB NOT NULL DEFAULT '{}',
    action_taken    TEXT NOT NULL,                       -- keep | flag | delete | revise | reject
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_he_user ON hallucination_events(user_id, created_at DESC);
CREATE INDEX idx_he_type ON hallucination_events(event_type);
```

### 3.17 event_logs — 通用事件日志

```sql
-- 合并: search_logs + rag_traces + agent_events + external_validations
CREATE TABLE event_logs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT,
    event_type      TEXT NOT NULL,                       -- search | rag | agent_start | agent_end | tool_call | tool_result | summary_generated | external_validation
    payload         JSONB NOT NULL DEFAULT '{}',         -- 按 event_type 存储不同字段
    duration_ms     INTEGER,
    error_text      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_el_user ON event_logs(user_id, created_at DESC);
CREATE INDEX idx_el_type ON event_logs(event_type);
```

### 3.18 _schema_meta — Embedding 元数据

```sql
CREATE TABLE _schema_meta (
    key     TEXT PRIMARY KEY,
    value   JSONB NOT NULL
);

INSERT INTO _schema_meta (key, value) VALUES
    ('embedding', '{"model": "doubao-embedding", "dim": 1024, "provider": "volcano"}'),
    ('version', '{"schema": "4.1", "applied_at": "2026-07-18"}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

> 所有 `vector(1024)` 列必须在迁移时与 `_schema_meta.embedding.dim` 一致。换模型时先 CHECK 维度，不一致则 ALTER COLUMN + 重建索引。

---

## 4. pgvector 向量索引设计

### 4.1 HNSW 索引（3 个）

| 表 | 索引 | 参数 | 查询场景 |
|------|------|------|------|
| `paper_chunks` | `idx_chunks_embedding_hnsw` | m=16, ef_construction=200 | RAG 论文语义检索 |
| `captures` | `idx_captures_embedding_hnsw` | m=16, ef_construction=200 | 碎片知识语义检索 |
| `glossary_terms` | `idx_glossary_embedding_hnsw` | m=12, ef_construction=100 | 术语模糊匹配（数据量小） |

```sql
-- paper_chunks (最大数据量)
CREATE INDEX idx_chunks_embedding_hnsw ON paper_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- captures（中等数据量）
CREATE INDEX idx_captures_embedding_hnsw ON captures
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- glossary_terms（最小数据量）
CREATE INDEX idx_glossary_embedding_hnsw ON glossary_terms
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 12, ef_construction = 100);
```

**查询时动态设置 ef_search**：

```sql
-- 查询前设置探针深度（越大精度越高、越慢）
SET hnsw.ef_search = 50;   -- glossary
SET hnsw.ef_search = 100;  -- captures
SET hnsw.ef_search = 200;  -- paper_chunks
```

### 4.2 统一 RAG 检索函数（paper_chunks + captures）

```sql
CREATE OR REPLACE FUNCTION search_unified(
    query_embedding vector(1024),
    p_user_id TEXT,
    match_count INT DEFAULT 10,
    similarity_threshold REAL DEFAULT 0.5
) RETURNS TABLE (
    source      TEXT,
    chunk_id    TEXT,
    content     TEXT,
    title       TEXT,
    similarity  REAL
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
    SELECT 'capture'::TEXT, c.id, c.content, c.title,
           1 - (c.embedding <=> query_embedding) AS similarity
    FROM captures c
    WHERE c.user_id = p_user_id
      AND c.status = 'active'
      AND c.embedding <=> query_embedding < (1.0 - similarity_threshold)
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;
```

### 4.3 术语检索函数

```sql
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
```

---

## 5. 索引策略

### 5.1 向量索引（HNSW × 3）

| 表 | 索引名 | 参数 |
|------|------|------|
| paper_chunks | idx_chunks_embedding_hnsw | m=16, ef_construction=200 |
| captures | idx_captures_embedding_hnsw | m=16, ef_construction=200 |
| glossary_terms | idx_glossary_embedding_hnsw | m=12, ef_construction=100 |

### 5.2 单列索引（Single-key B-tree × 7）

| 表 | 索引 | 查询场景 |
|------|------|------|
| agents | idx_agents_user (user_id) | API `/agents/me` |
| agents | idx_agents_active (user_id, is_active) | Supervisor 扫描 |
| projects | idx_projects_user (user_id) | 用户项目列表 |
| sessions | idx_sessions_user (user_id) | 用户会话列表 |
| paper_chunks | idx_chunks_paper (paper_id) | 论文→切片 |
| paper_chunks | idx_chunks_user (user_id) | 用户隔离 |
| papers | idx_papers_user (user_id) | 用户论文列表 |

### 5.3 复合索引（Multi-key B-tree × 15）

| 表 | 索引 | 列 | 查询场景 |
|------|------|------|------|
| users | idx_users_username | username (UNIQUE) | 登录 |
| papers | idx_papers_doi | doi (partial) | DOI 查重 |
| papers | idx_papers_arxiv | arxiv_id (partial) | arXiv 查重 |
| project_papers | pk_project_paper | project_id, paper_id (UNIQUE) | 去重+关联 |
| sessions | idx_sessions_thread | user_id, thread_id | LangGraph 恢复 |
| ws_messages | idx_ws_session | session_id, created_at | 离线消息 |
| ws_messages | idx_ws_user | user_id, created_at DESC | 审计 |
| ws_messages | idx_ws_tool_dedup | session_id + payload tool_call_id (partial UNIQUE) | 去重 |
| ws_messages | idx_ws_plan_dedup | session_id + payload plan_id (partial UNIQUE) | 去重 |
| captures | idx_captures_type | user_id, capture_type | 按类型筛选 |
| glossary_terms | idx_glossary_domain | user_id, domain | 按领域查术语 |
| glossary_terms | idx_glossary_term | user_id, en_term, zh_term (UNIQUE) | 去重 |
| documents | idx_documents_user | user_id, updated_at DESC | 文档列表 |
| subscriptions | idx_subscriptions_user | user_id, is_active | Beat 订阅检查 |
| event_logs | idx_el_user | user_id, created_at DESC | 事件日志 |

### 5.4 全文索引（GIN × 3）

| 表 | 索引 | 列 | 查询场景 |
|------|------|------|------|
| papers | idx_papers_fts | title + abstract | 中英文论文搜索 |
| captures | idx_captures_fts | title + content | 碎片知识关键词搜索 |
| glossary_terms | idx_glossary_fts | en_term | 英文术语匹配 |

### 5.5 汇总

| 类型 | 数量 | 说明 |
|------|:---:|------|
| HNSW 向量 | 3 | 论文 / 碎片 / 术语 |
| Single-key B-tree | 7 | 高频单列 |
| Multi-key B-tree | 15 | 复合条件 |
| GIN 全文 | 3 | 文本搜索 |
| **总计** | **28** | |

---

## 6. Redis 数据结构

### 6.1 消息与路由（v4.1 Supervisor 架构）

| Key | 类型 | 写 | 读 | 用途 |
|-----|------|:---:|:---:|------|
| `agent:status` | Hash | Supervisor | API | 全量 Agent 状态 |
| `agent:ws:{uid}` | List | API | Supervisor | 入站消息队列 |
| `agent:outbox:{uid}` | List | Supervisor | API | 出站消息队列 |
| `agent:control` | Pub/Sub | API | Supervisor | 控制指令（仅启停） |
| `agent:ws:{uid}:parked` | List | Supervisor | Supervisor | 暂存未匹配消息 |

### 6.2 缓存

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `glossary:hot:{uid}:{domain}` | Sorted Set | 热点术语 | 1h |
| `rag:cache:{query_hash}` | String | LLM RAG 回答缓存 | 1h |
| `external:validation:{doi}` | String | 外部验证缓存 | 30d |

### 6.3 限流与锁

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `rate:{uid}:{endpoint}` | String | 请求计数 | 1min |
| `lock:ingest:{paper_id}` | String | 防重复入库 | 10min |

### 6.4 Celery 任务

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `celery:task:{celery_task_id}` | String | 任务状态缓存 | 1h |
| `celery:beat:last_run:{task_name}` | String | Beat 上次执行 | 永久 |

---

## 7. 多用户数据隔离

| 层级 | 机制 | 说明 |
|------|------|------|
| **应用层** | 所有查询强制 `WHERE user_id = $uid` | DAO 层统一处理 |
| **向量层** | `search_unified()` 函数内置 `p_user_id` 过滤 | 防止跨用户向量检索 |
| **缓存层** | Redis key 包含 `{user_id}` | 防止跨用户缓存读取 |

---

## 8. 迁移映射（30→17 表）

```
[保留，结构不变]
  users, projects, project_papers, journal_ranks, hallucination_events

[保留，结构微调]
  papers (+figures/archives JSONB → 合并 paper_figures + paper_archives)
  paper_chunks (不变)
  glossary_terms (添加 embedding 列 → 合并 glossary_embeddings)
  captures (+embedding → 合并 videos)
  documents (+versions JSONB → 合并 document_versions)
  agents (+state 列)
  sessions (+summary JSONB → 合并 conversation_archive)
  ws_messages (+priority → 去掉 priority_kind/role 冗余字段)

[合并 → event_logs]
  search_logs + rag_traces + agent_events + external_validations → event_logs

[合并 → JSONB 字段]
  subscription_results → subscriptions.results JSONB
  user_configs → user_preferences.other JSONB
  writing_templates → 系统配置 + preferences.other

[新增]
  share_requests, _schema_meta

[删除 / 替代]
  agent_tasks + task_steps → Celery 自身管理
  topic_embeddings → paper_chunks 聚类替代（后续实现）
  session_summaries → sessions.summary JSONB
  session_scan_markers → Redis key
  message_embeddings → 不需要（RAG 走 paper_chunks + captures）
  citations → 后续按需加回
  research_groups + group_members + shared_papers → share_requests
```

---

## 9. 架构审查

审查于 2026-07-18，9 条意见全部有解决方案：

| # | 审查意见 | 解决方案 | 状态 |
|:---:|------|------|:---:|
| 1 | ws_messages 去重索引缺失 | 2 个 partial UNIQUE 索引（tool_call_id + plan_id） | 已加入表设计 |
| 2 | paper_chunks 缺向量索引 | HNSW 3 个（paper_chunks, captures, glossary_terms） | 已加入索引方案 |
| 3 | documents.versions JSONB 写入膨胀 | jsonb_set 增量写 + 200 条上限 + ai_turn 5s 去重 | 已加入设计说明 |
| 4 | subscriptions.results 无限增长 | 50 条限制 + 溢出 → event_logs | 已加入设计说明 |
| 5 | vector 维度硬编码 | _schema_meta 表记录 embedding 模型 + 迁移 CHECK | 已加入表设计 |
| 6 | captures + glossary 缺全文索引 | 3 个 GIN FTS 索引（papers, captures, glossary_terms） | 已加入索引方案 |
| 7 | agents.state 高频写 DB | Redis Hash 为主 + DB 只写大变更 + 每 5min 批量同步 | 已加入设计说明 |
| 8 | 会话摘要分析能力 | sessions.summary JSONB + event_logs 记录摘要生成事件 | 已加入设计 |
| 9 | 迁移映射文档 | §8 迁移映射表 | 已加入文档 |

---

> 本文档定义了 v4.1 的数据库架构：17 张业务表 + 28 个索引 + 5 组 Redis Key。从 v3 的 30 张表精简到 17 张，覆盖用户/论文/项目/向量/碎片知识/术语/文档/偏好/订阅/共享/审计全部功能。
