# 智驭·研 v3 数据库架构设计

> 数据库方案：PostgreSQL (业务数据) + pgvector (向量存储) + Redis (缓存/队列)
>
> 日期：2026-07-10

---

## 目录

1. [技术选型](#1-技术选型)
2. [PostgreSQL 表结构](#2-postgresql-表结构)
   - 2.1 表关系 ER 图
   - 2.2 用户与认证
   - 2.3 项目与论文
   - 2.4 引用关系
   - 2.5 会话与消息
   - 2.6 Agent 任务
   - 2.7 术语词表
   - 2.8 写作模板
   - 2.9 外部验证缓存
   - 2.10 反幻觉事件
   - 2.11 基础设施（搜索日志 / 期刊分级 / 视频）
   - 2.12 碎片知识采集 [新增]
   - 2.13 订阅
   - 2.14 Agent 事件
3. [pgvector 向量存储设计](#3-pgvector-向量存储设计)
4. [Redis 数据结构](#4-redis-数据结构)
5. [索引策略](#5-索引策略)
6. [迁移方案（SQLite → PostgreSQL）](#6-迁移方案)
7. [多用户数据隔离策略](#7-多用户数据隔离策略)

---

## 1. 技术选型

| 组件 | 版本 | 用途 | 替代旧方案 |
|------|------|------|-----------|
| PostgreSQL | 16+ | 所有业务数据存储 | SQLite (AgentDB) |
| pgvector | 0.8+ | 向量存储与检索 | ChromaDB |
| Redis | 7+ | 缓存 / 消息队列 / Pub/Sub / 限流 | 保持不变，增强 |

### 选型理由

| 对比维度 | SQLite | PostgreSQL + pgvector |
|----------|--------|----------------------|
| 并发写入 | 单写入锁，多用户场景瓶颈 | MVCC，天然高并发 |
| 向量检索 | 依赖外部 ChromaDB，数据一致性问题 | pgvector 与业务表在同一事务中 |
| 全文搜索 | 基础 FTS | `tsvector` + `tsquery`，中英文支持 |
| JSON 查询 | 有限 | `jsonb` + GIN 索引 |
| 运维 | 单文件，无运维 | 需要独立部署，但支持备份/复制 |
| 多用户隔离 | 应用层限制 | Schema 级隔离 + RLS（可选） |

---

## 2. PostgreSQL 表结构

### 2.1 表关系 ER 图

```
users
  │
  ├── 1:N ── projects ── N:M ── papers
  │   (via project_papers)
  │
  ├── 1:N ── papers                   # 用户上传/入库的论文
  ├── 1:N ── sessions
  ├── 1:N ── glossary_terms
  ├── 1:N ── writing_templates
  ├── 1:N ── agent_tasks ── 1:N ── task_steps
  ├── 1:N ── search_logs
  ├── 1:N ── captures                # 碎片知识（网页/笔记/实验/音视频）
  ├── 1:N ── subscriptions ── 1:N ── subscription_results
  └── 1:N ── hallucination_events

papers
  │
  ├── 1:N ── paper_chunks (pgvector)
  ├── 1:N ── citations              (引用关系；cited_paper_id 可选 FK→papers)
  └── N:M ── projects               (via project_papers)

sessions
  │
  ├── 1:N ── ws_messages
  └── 1:N ── conversation_archive
```

### 2.2 用户与认证

```sql
-- 用户表
CREATE TABLE users (
    id          TEXT PRIMARY KEY,           -- user-xxx (UUID)
    username    TEXT NOT NULL UNIQUE,        -- 登录名
    display_name TEXT NOT NULL,              -- 显示名称
    api_token   TEXT NOT NULL UNIQUE,        -- Bearer Token (tok-xxx)
    role        TEXT NOT NULL DEFAULT 'researcher',  -- researcher | admin
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 用户配置（JSON 灵活扩展）
CREATE TABLE user_configs (
    id          TEXT PRIMARY KEY,           -- cfg-xxx
    user_id     TEXT NOT NULL REFERENCES users(id),
    config_key  TEXT NOT NULL,              -- e.g. 'llm.model_preference'
    config_value JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, config_key)
);

CREATE INDEX idx_users_api_token ON users(api_token);
```

### 2.3 项目与论文

```sql
-- 项目表
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,           -- prj-xxx
    user_id     TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT '',    -- computer_vision / nlp / system / ...
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_user ON projects(user_id);

-- 论文表
CREATE TABLE papers (
    id              TEXT PRIMARY KEY,           -- pap-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    authors         JSONB NOT NULL DEFAULT '[]', -- [{name, affiliation, email}]
    year            INTEGER,
    venue           TEXT,                        -- 会议/期刊名
    venue_type      TEXT,                        -- conference / journal / workshop / preprint
    doi             TEXT,
    arxiv_id        TEXT,
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        JSONB NOT NULL DEFAULT '[]', -- [keyword1, keyword2, ...]
    source          TEXT NOT NULL,               -- arxiv / semanticscholar / cnki / manual
    source_priority INTEGER NOT NULL DEFAULT 30, -- 保留优先级（§6 去重用）
    file_path       TEXT,                        -- PDF 文件路径
    md_path         TEXT,                        -- Markdown 文件路径
    figures_dir     TEXT,                        -- 图片目录路径
    metadata        JSONB NOT NULL DEFAULT '{}', -- 灵活扩展元数据
    citation_count  INTEGER NOT NULL DEFAULT 0,  -- 引用数（从 S2 API）
    tl_dr           TEXT,                        -- 一句话摘要（LLM 生成）
    status          TEXT NOT NULL DEFAULT 'ingested', -- searching / downloaded / converting / processing / ingested / active / archived / deleted
    duplicate_of    TEXT,                        -- 如果是重复版本，指向主记录
    alternate_versions JSONB DEFAULT '[]',       -- 其他版本的 paper_id 列表
    -- 以下来自当前生产 schema（保留）
    method_tags     JSONB DEFAULT '[]',           -- 提取的方法名列表
    dataset_info    JSONB DEFAULT '{}',           -- 数据集名称和指标
    code_url        TEXT,                         -- 开源代码链接
    reading_level   TEXT,                         -- 阅读进度：unread / skimmed / read / intensive
    digest          TEXT,                         -- 用户笔记/摘要
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 全文搜索索引
CREATE INDEX idx_papers_title_fts ON papers USING gin(to_tsvector('english', title));
CREATE INDEX idx_papers_abstract_fts ON papers USING gin(to_tsvector('english', abstract));
CREATE INDEX idx_papers_user ON papers(user_id);
CREATE INDEX idx_papers_venue ON papers(venue);
CREATE INDEX idx_papers_year ON papers(year);
CREATE INDEX idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE INDEX idx_papers_arxiv_id ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;

-- 项目-论文关联表（N:M）
CREATE TABLE project_papers (
    id          TEXT PRIMARY KEY,           -- pp-xxx
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id    TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, paper_id)
);

CREATE INDEX idx_pp_project ON project_papers(project_id);
CREATE INDEX idx_pp_paper ON project_papers(paper_id);
```

### 2.4 引用关系

```sql
CREATE TABLE citations (
    id              TEXT PRIMARY KEY,           -- cit-xxx
    paper_id        TEXT NOT NULL REFERENCES papers(id),
    cited_paper_id  TEXT REFERENCES papers(id) ON DELETE SET NULL, -- 引用的论文（如果在库内）
    cited_doi       TEXT,                        -- 引用的论文 DOI（外部文献）
    cited_title     TEXT,                        -- 引用的论文标题
    cited_authors   JSONB,
    cited_year      INTEGER,
    citation_context TEXT,                       -- 引用上下文（具体引用语句）
    classification  TEXT DEFAULT 'unknown',      -- supporting / mentioning / disputing
    confidence      REAL NOT NULL DEFAULT 0.0,   -- 分类置信度（0-1）
    verified        BOOLEAN NOT NULL DEFAULT false,
    verified_by     TEXT,                        -- local_db / crossref / arxiv / semantic_scholar
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_citations_paper ON citations(paper_id);
CREATE INDEX idx_citations_cited ON citations(cited_paper_id) WHERE cited_paper_id IS NOT NULL;
```

### 2.5 会话与消息

```sql
-- 会话表
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,           -- sess-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,              -- agent-{user_id}
    thread_id       TEXT NOT NULL,              -- LangGraph thread_id
    title           TEXT NOT NULL DEFAULT '新会话',
    status          TEXT NOT NULL DEFAULT 'active', -- active / archived
    metadata        JSONB NOT NULL DEFAULT '{}',
    message_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sessions_user_thread ON sessions(user_id, thread_id);

-- WebSocket 消息表
CREATE TABLE ws_messages (
    id              TEXT PRIMARY KEY,           -- msg-xxx
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    seq             INTEGER NOT NULL,           -- 会话内递增序列号（消息排序）
    direction       TEXT NOT NULL,              -- inbound / outbound
    msg_type        TEXT NOT NULL,              -- user/message / message/text / sub_agent/progress / ...
    subtype         TEXT,                       -- 消息子类型
    payload         JSONB NOT NULL DEFAULT '{}',
    priority_kind   TEXT NOT NULL DEFAULT 'normal', -- normal / alert / progress
    is_delivered    BOOLEAN NOT NULL DEFAULT false,
    is_replay       BOOLEAN NOT NULL DEFAULT false, -- 是否回放消息
    msg_id          TEXT,                       -- 客户端去重 ID
    correlation_id  TEXT,                       -- 请求-响应对应 ID
    delivered_at    TIMESTAMPTZ,
    delivered_sessions TEXT[],                   -- 已送达的 session 列表
    apns_sent_at    TIMESTAMPTZ,                -- iOS APNs 推送时间
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ws_session ON ws_messages(session_id);
CREATE INDEX idx_ws_user ON ws_messages(user_id);
CREATE INDEX idx_ws_created ON ws_messages(created_at);
CREATE INDEX idx_ws_session_seq ON ws_messages(session_id, seq);

-- 会话摘要归档（LangGraph Store episodes）
CREATE TABLE conversation_archive (
    id              TEXT PRIMARY KEY,           -- arch-xxx
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    summary_text    TEXT NOT NULL,
    summary_type    TEXT NOT NULL DEFAULT 'rolling', -- rolling / topic / final
    start_msg_id    TEXT,
    end_msg_id      TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_archive_session ON conversation_archive(session_id);
```

### 2.6 Agent 任务

```sql
CREATE TABLE agent_tasks (
    id              TEXT PRIMARY KEY,           -- task-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    mode            TEXT NOT NULL DEFAULT 'full_ingest', -- screening / full_ingest / rag_query / survey
    name            TEXT NOT NULL DEFAULT '',    -- 任务名称（用户可读）
    agent_name      TEXT NOT NULL,              -- LiteratureAgent / KnowledgeAgent / ...
    task_kind       TEXT NOT NULL,              -- search / download / convert / chunk / embed / verify
    celery_task_id  TEXT,                       -- Celery 异步任务 ID
    status          TEXT NOT NULL DEFAULT 'pending', -- pending / running / done / failed
    progress        JSONB DEFAULT '{}',          -- {stage, current, total, details}
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

-- 任务步骤明细
CREATE TABLE task_steps (
    id              TEXT PRIMARY KEY,           -- step-xxx
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id),
    step_order      INTEGER NOT NULL,
    step_name       TEXT NOT NULL,              -- search / evaluate / download / convert / chunk / embed
    status          TEXT NOT NULL DEFAULT 'pending',
    detail          JSONB DEFAULT '{}',          -- {papers_processed, errors, duration_ms}
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_steps_task ON task_steps(task_id);
```

### 2.7 术语词表

```sql
CREATE TABLE glossary_terms (
    id              TEXT PRIMARY KEY,           -- gl-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    en_term         TEXT NOT NULL,               -- adversarial attack
    zh_term         TEXT NOT NULL,               -- 对抗攻击
    variants        JSONB NOT NULL DEFAULT '[]', -- ["adversarial perturbation", "对抗扰动"]
    domain          TEXT NOT NULL DEFAULT '',     -- computer_vision / nlp / ...
    source_paper_ids JSONB NOT NULL DEFAULT '[]',-- 来源于哪些论文
    tf              REAL NOT NULL DEFAULT 0.0,   -- 归一化词频（跨文章平均）
    df              INTEGER NOT NULL DEFAULT 0,  -- 文档频率
    cross_doc_score REAL NOT NULL DEFAULT 0.0,   -- 跨文档加权分
    llm_confidence  REAL NOT NULL DEFAULT 0.0,   -- LLM 审核置信度
    user_verified   BOOLEAN NOT NULL DEFAULT false,
    last_seen_at    TIMESTAMPTZ,                 -- 最近在文献中出现的时间
    usage_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, en_term, zh_term)
);

CREATE INDEX idx_glossary_user ON glossary_terms(user_id);
CREATE INDEX idx_glossary_domain ON glossary_terms(user_id, domain);
CREATE INDEX idx_glossary_en ON glossary_terms(en_term);
```

### 2.8 写作模板

```sql
CREATE TABLE writing_templates (
    id              TEXT PRIMARY KEY,           -- tpl-xxx
    user_id         TEXT,                        -- NULL = 系统预置模板，非 NULL = 用户自定义
    name            TEXT NOT NULL,               -- "CVPR Related Work"
    venue           TEXT,                        -- cvpr / iccv / neurips / acl / ...
    domain          TEXT NOT NULL DEFAULT '',     -- computer_vision / nlp / system / ...（按领域过滤模板）
    section_type    TEXT NOT NULL,               -- related_work / method / introduction / abstract
    structure_json  JSONB NOT NULL,              -- 结构骨架（填空模板）
    sample_text     TEXT,                        -- 示例文本
    source_paper_ids JSONB DEFAULT '[]',         -- 参考的模范论文
    is_system       BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_templates_venue ON writing_templates(venue);
CREATE INDEX idx_templates_user ON writing_templates(user_id) WHERE user_id IS NOT NULL;
```

### 2.9 外部验证缓存

```sql
-- 外部引用验证结果缓存
CREATE TABLE external_validations (
    id              TEXT PRIMARY KEY,           -- ev-xxx
    identifier_type TEXT NOT NULL,               -- doi / arxiv_id / title
    identifier      TEXT NOT NULL,
    validation_result JSONB NOT NULL,            -- {exists, title, authors, year, venue, doi}
    validated_by    TEXT NOT NULL,               -- crossref / arxiv / semantic_scholar
    validated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,        -- 30 天后过期
    UNIQUE (identifier_type, identifier)
);

CREATE INDEX idx_ev_expires ON external_validations(expires_at);
```

### 2.10 反幻觉事件

```sql
-- 幻觉检测事件记录（用于评估和改进）
CREATE TABLE hallucination_events (
    id              TEXT PRIMARY KEY,           -- he-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    citation_id     TEXT,                        -- 关联的 citation ID
    event_type      TEXT NOT NULL,               -- false_citation / claim_mismatch / fabricated_doi / ...
    llm_output      TEXT NOT NULL,               -- LLM 原始输出
    expected_output TEXT,                        -- 期望的正确输出
    verified_result JSONB NOT NULL,              -- 验证结果
    action_taken    TEXT NOT NULL,               -- keep / flag / delete / revise / reject
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_he_user ON hallucination_events(user_id);
CREATE INDEX idx_he_type ON hallucination_events(event_type);
CREATE INDEX idx_he_created ON hallucination_events(created_at);
```

### 2.11 基础设施

```sql
-- 搜索日志
CREATE TABLE search_logs (
    id              TEXT PRIMARY KEY,           -- slog-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    query           TEXT NOT NULL,
    source          TEXT NOT NULL,               -- arxiv / semanticscholar / cnki / ...
    result_count    INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_search_user ON search_logs(user_id);
CREATE INDEX idx_search_created ON search_logs(created_at);

-- 期刊/会议分级缓存
CREATE TABLE journal_ranks (
    id              TEXT PRIMARY KEY,           -- jr-xxx
    venue           TEXT NOT NULL UNIQUE,        -- 会议/期刊名
    rank            TEXT NOT NULL,               -- CCF-A / CCF-B / CCF-C / SCI-1 / ...
    source          TEXT NOT NULL,               -- ccf / sci / custom
    year            INTEGER,                     -- 评级年份
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 视频解析结果（由 Capture Agent 管理，逐步迁移到 captures 表）
CREATE TABLE videos (
    id              TEXT PRIMARY KEY,           -- vid-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    url             TEXT NOT NULL,
    platform        TEXT,                        -- youtube / bilibili / 抖音 / ...
    title           TEXT,
    duration_sec    INTEGER,
    transcript      TEXT,                        -- ASR 转写全文
    summary         TEXT,                        -- LLM 摘要
    keywords        JSONB DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_videos_user ON videos(user_id);
```

### 2.12 碎片知识采集（Capture Agent）[新增]

```sql
-- 碎片知识表（网页、实验记录、会议笔记、音视频等）
CREATE TABLE captures (
    id              TEXT PRIMARY KEY,           -- cap-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    capture_type    TEXT NOT NULL,               -- web_clip / experiment_note / meeting_note / audio / video
    title           TEXT NOT NULL,
    content         TEXT,                         -- 文本内容 / 摘要
    source_url      TEXT,                         -- 来源 URL（web_clip 时）
    tags            JSONB NOT NULL DEFAULT '[]',
    transcript      TEXT,                         -- ASR 转写文本（audio/video 时由 Capture Agent 生成）
    summary         TEXT,                         -- LLM 摘要
    keywords        JSONB DEFAULT '[]',
    embedding       vector(1024),                -- 可选语义检索（与知识库统一检索）
    status          TEXT NOT NULL DEFAULT 'active', -- active / archived / deleted
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_captures_user ON captures(user_id);
CREATE INDEX idx_captures_type ON captures(user_id, capture_type);
CREATE INDEX idx_captures_created ON captures(created_at);
```

### 2.13 订阅

```sql
CREATE TABLE subscriptions (
    id              TEXT PRIMARY KEY,           -- sub-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    query           TEXT NOT NULL,               -- 搜索 query
    source          TEXT NOT NULL DEFAULT 'arxiv',
    frequency       TEXT NOT NULL DEFAULT 'daily', -- daily / weekly
    max_papers_per_check INTEGER NOT NULL DEFAULT 20, -- 每次检查最多推送论文数
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription_results (
    id              TEXT PRIMARY KEY,           -- sr-xxx
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    paper_id        TEXT,
    paper_title     TEXT,
    paper_doi       TEXT,
    is_new          BOOLEAN NOT NULL DEFAULT true,
    notified        BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sub_results_sub ON subscription_results(subscription_id);
```

### 2.14 Agent 事件

```sql
-- Agent 事件（Checkpoint 准备，Phase 4）
CREATE TABLE agent_events (
    id              TEXT PRIMARY KEY,           -- ev-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    agent_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ae_agent ON agent_events(agent_id);
CREATE INDEX idx_ae_created ON agent_events(created_at);
```

---

## 3. pgvector 向量存储设计

### 3.1 架构概览

使用 PostgreSQL `pgvector` 扩展，在同一数据库实例中管理向量，消除 ChromaDB 的外部依赖：

```
PostgreSQL
├── 业务表 (Section 2)
├── pgvector 扩展
│   ├── paper_chunks          # 论文语义切片
│   ├── glossary_embeddings   # 术语 embedding
│   ├── topic_embeddings      # 主题摘要
│   └── session_summaries     # 会话摘要
└── 事务一致性：业务表 JOIN 向量表在同一事务中
```

### 3.2 论文切片向量表

```sql
-- 启用 pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- 论文切片表
CREATE TABLE paper_chunks (
    id              TEXT PRIMARY KEY,           -- chk-xxx
    paper_id        TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    
    -- 内容
    chunk_text      TEXT NOT NULL,               -- 切片原始文本
    chunk_type      TEXT NOT NULL DEFAULT 'body', -- body / abstract / figure_caption / table
    section_title   TEXT,                         -- 所属 section 标题
    section_level   INTEGER,                     -- heading 层级（2=##, 3=###）
    chunk_order     INTEGER NOT NULL,            -- 在论文中的顺序
    
    -- 向量（embedding 维度取决于模型，此处以 1024 为例）
    embedding       vector(1024),                -- doubao-embedding / bge-large-zh 等
    
    -- 元数据
    token_count     INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 向量索引（IVFFlat：适合 < 100万 条数据；HNSW：适合更大规模）
CREATE INDEX idx_chunks_embedding_ivf 
    ON paper_chunks 
    USING ivfflat (embedding vector_cosine_ops) 
    WITH (lists = 100);

-- 可切换为 HNSW 以获得更好的查询性能
-- CREATE INDEX idx_chunks_embedding_hnsw 
--     ON paper_chunks 
--     USING hnsw (embedding vector_cosine_ops);

-- 业务索引
CREATE INDEX idx_chunks_paper ON paper_chunks(paper_id);
CREATE INDEX idx_chunks_user ON paper_chunks(user_id);
CREATE INDEX idx_chunks_type ON paper_chunks(chunk_type);

-- 向量检索函数（注意：WHERE 子句使用裸距离比较以利用 IVFFlat/HNSW 索引）
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
        AND pc.embedding <=> query_embedding < max_distance  -- 裸距离比较，走 IVFFlat/HNSW 索引
    ORDER BY pc.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;
```

### 3.3 术语词表向量表

```sql
CREATE TABLE glossary_embeddings (
    id              TEXT PRIMARY KEY,           -- gle-xxx
    glossary_term_id TEXT NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
    -- 注：无需 user_id（通过 glossary_term_id → glossary_terms 获取），避免冗余不一致
    -- 向量化内容：中英文术语拼接
    term_text       TEXT NOT NULL,               -- "adversarial attack 对抗攻击"
    embedding       vector(1024),
    
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_gle_embedding_ivf 
    ON glossary_embeddings 
    USING ivfflat (embedding vector_cosine_ops) 
    WITH (lists = 50);

-- 模糊术语匹配函数（加入 glossary_terms 表以获取 user_id）
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
    WHERE gt.user_id = p_user_id        -- user_id 通过 JOIN glossary_terms 获取
    ORDER BY ge.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;
```

### 3.4 会话与主题摘要向量表

```sql
-- 会话摘要（LangGraph Store episodes 对应表）
CREATE TABLE session_summaries (
    id              TEXT PRIMARY KEY,           -- ssum-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    
    summary_text    TEXT NOT NULL,
    summary_type    TEXT NOT NULL,              -- rolling / topic / final
    embedding       vector(1024),
    
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ssum_user ON session_summaries(user_id);
CREATE INDEX idx_ssum_session ON session_summaries(session_id);

-- 研究方向主题摘要
CREATE TABLE topic_embeddings (
    id              TEXT PRIMARY KEY,           -- top-xxx
    user_id         TEXT NOT NULL REFERENCES users(id),
    
    topic_name      TEXT NOT NULL,
    description     TEXT NOT NULL,
    embedding       vector(1024),
    
    paper_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_topic_user ON topic_embeddings(user_id);
```

### 3.5 向量检索 SQL 示例

> **注意**：`vector(1024)` 维度硬编码依赖当前 embedding 模型（doubao / bge-large-zh 均为 1024 维）。
> 如更换模型（如 text-embedding-3-large 为 3072 维），需 `ALTER TABLE ... ALTER COLUMN embedding TYPE vector(3072)` 并重建索引。

```sql
-- RAG 检索：根据用户查询找到最相关的论文 chunk
SELECT pc.chunk_text, pc.section_title, p.title,
       1 - (pc.embedding <=> $query_embedding) AS similarity
FROM paper_chunks pc
JOIN papers p ON pc.paper_id = p.id
WHERE pc.user_id = $user_id
  AND 1 - (pc.embedding <=> $query_embedding) > 0.5
ORDER BY pc.embedding <=> $query_embedding
LIMIT 10;

-- 跨语言检索：中文 query embedding → 英文论文 chunk
SELECT pc.chunk_text, p.title, p.year,
       1 - (pc.embedding <=> $zh_query_embedding) AS similarity
FROM paper_chunks pc
JOIN papers p ON pc.paper_id = p.id
WHERE pc.user_id = $user_id
ORDER BY pc.embedding <=> $zh_query_embedding
LIMIT 20;

-- 术语模糊匹配
SELECT gt.en_term, gt.zh_term,
       1 - (ge.embedding <=> $term_embedding) AS similarity
FROM glossary_embeddings ge
JOIN glossary_terms gt ON ge.glossary_term_id = gt.id
WHERE ge.user_id = $user_id
  AND 1 - (ge.embedding <=> $term_embedding) > 0.7
ORDER BY ge.embedding <=> $term_embedding
LIMIT 5;

-- 混合检索：向量 + 全文搜索
SELECT pc.chunk_text, p.title,
       1 - (pc.embedding <=> $query_embedding) AS vec_similarity,
       ts_rank(to_tsvector('english', pc.chunk_text), plainto_tsquery('english', $query_text)) AS text_rank
FROM paper_chunks pc
JOIN papers p ON pc.paper_id = p.id
WHERE pc.user_id = $user_id
  AND (
    1 - (pc.embedding <=> $query_embedding) > 0.4
    OR to_tsvector('english', pc.chunk_text) @@ plainto_tsquery('english', $query_text)
  )
ORDER BY (vec_similarity * 0.7 + text_rank * 0.3) DESC
LIMIT 10;
```

---

## 4. Redis 数据结构

### 4.1 Key 设计规范

```
{namespace}:{subsystem}:{identifier}
```

### 4.2 消息与通信

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `outbox:{user_id}` | List | 出站消息队列（BRPOP 消费） | 无 |
| `agent:reports:{task_id}` | Pub/Sub Channel | 子 Agent → Main Agent 状态上报 | 无 |
| `ws:connections:{user_id}` | Set | WebSocket 连接 ID 集合 | 无 |
| `ws:session:{session_id}` | Hash | 当前在线 session→ws 映射 | 随连接断开 |

### 4.3 缓存

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `glossary:hot:{user_id}:{domain}` | Sorted Set | 热点术语（按 usage_count） | 1h |
| `glossary:user:{user_id}` | Hash | 用户术语词表摘要 | 1h |
| `rag:cache:{query_hash}` | String (JSON) | LLM 回答缓存 | 1h |
| `external:validation:{doi}` | String (JSON) | 外部验证结果缓存 | 30d |

### 4.4 限流与锁

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `rate:{user_id}:{endpoint}` | String | 请求计数 | 1min |
| `lock:ingest:{paper_id}` | String | 防重复入库锁 | 10min |
| `lock:glossary:collect:{user_id}` | String | 术语收集防并发 | 5min |

### 4.5 并行子 Agent 调度 [新增]

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `agent:context:pool:{session_id}` | Hash | 共享上下文池（子 Agent 间传递搜索结果/用户意图/术语列表） | 随 session 生命周期 |
| `agent:parallel:group:{group_id}` | Hash | 并行组状态追踪（`{agent_id: status, ...}`） | 1h |
| `agent:parallel:results:{group_id}` | List | 并行子 Agent 结果收集 | 1h |

### 4.6 Celery 任务

| Key | 类型 | 用途 | TTL |
|-----|------|------|-----|
| `celery:task:{celery_task_id}` | String (JSON) | Celery 任务状态缓存 | 1h |
| `celery:beat:last_run:{task_name}` | String | Beat 上次执行时间 | 永久 |

---

## 5. 索引策略

### 5.1 必须索引（P0）

| 表 | 索引 | 类型 | 原因 |
|----|------|------|------|
| papers | `user_id` | B-tree | 多用户隔离查询入口 |
| papers | `title` (FTS) | GIN | 标题全文搜索 |
| paper_chunks | `paper_id` | B-tree | chunk→paper 关联 |
| paper_chunks | `embedding` | IVFFlat/HNSW | 向量检索 |
| glossary_terms | `(user_id, en_term, zh_term)` | UNIQUE | 去重 |
| glossary_terms | `(user_id, domain)` | B-tree | 按领域查询术语 |
| glossary_embeddings | `embedding` | IVFFlat | 术语模糊检索 |
| sessions | `(user_id, thread_id)` | B-tree | 会话恢复（复合索引） |
| ws_messages | `(session_id, created_at)` | B-tree | 消息历史查询 |
| agent_tasks | `(user_id, status)` | B-tree | 用户任务列表 |

### 5.2 推荐索引（P1）

| 表 | 索引 | 类型 | 原因 |
|----|------|------|------|
| papers | `doi` (partial) | B-tree | 外部引用快速查重 |
| papers | `arxiv_id` (partial) | B-tree | 同上 |
| papers | `(venue, year)` | B-tree | 按会议/年份筛选 |
| citations | `paper_id` | B-tree | 论文→引用关系 |
| search_logs | `created_at` | B-tree | 周度统计 |

### 5.3 pgvector 索引选择

| 场景 | 推荐索引 | 参数 | 说明 |
|------|----------|------|------|
| 开发/测试 (< 10万向量) | IVFFlat | lists = 行数 / 1000 | 构建快，查询可接受 |
| 生产 (10万 - 100万) | IVFFlat | lists = 行数 / 1000 | 稳定可靠 |
| 大规模 (> 100万) | HNSW | m=16, ef_construction=200 | 查询更快，构建更慢 |

---

## 6. 迁移方案（SQLite → PostgreSQL）

### 6.1 迁移步骤

```
Phase 0: 准备
  1. 部署 PostgreSQL 16+ + pgvector 扩展
  2. 创建空 schema（执行所有 CREATE TABLE）
  3. 验证 schema 正确性

Phase 1: 数据迁移脚本
  1. 读取 SQLite 数据（Python sqlite3）
  2. 转换数据（id 格式 / JSON → JSONB / 时间格式）
  3. 批量 INSERT 到 PostgreSQL（asyncpg / psycopg2 COPY）
  4. 数据校验（行数对比 + 采样校验）

Phase 2: 向量迁移 (ChromaDB → pgvector)
  1. 遍历 ChromaDB collections
  2. 逐个 collection 导出 embedding + metadata
  3. 写入 pgvector 表（使用批量 INSERT）
  4. 创建索引（迁移完成后统一创建）

Phase 3: 切换
  1. 修改 config.py 数据库连接
  2. 灰度测试（单用户先切换）
  3. 全量切换
  4. 保留 SQLite 作为备份（30 天后删除）
```

### 6.2 迁移脚本结构

```python
# scripts/migrate_to_postgres.py
class MigrationRunner:
    async def run(self):
        await self.migrate_users()
        await self.migrate_papers()          # 包含 project_papers
        await self.migrate_sessions()        # 包含 ws_messages
        await self.migrate_glossary()
        await self.migrate_templates()
        await self.migrate_agent_tasks()     # 包含 task_steps
        await self.migrate_vectors()         # ChromaDB → pgvector
        await self.validate_counts()
```

---

## 7. 多用户数据隔离策略

### 7.1 隔离层级

| 层级 | 机制 | 说明 |
|------|------|------|
| **应用层** | 所有查询强制带 `WHERE user_id = $user_id` | 由 DAO 层统一处理 |
| **向量层** | `search_paper_chunks` 函数内置 `p_user_id` 过滤 | 防止跨用户向量检索 |
| **缓存层** | Redis key 包含 `{user_id}` | 防止跨用户缓存读取 |
| **可选** | PostgreSQL RLS (Row Level Security) | 数据库层面的兜底隔离 |

### 7.2 课题组共享（Phase 2 预留）

```sql
-- 预留：课题组表
CREATE TABLE research_groups (
    id          TEXT PRIMARY KEY,           -- grp-xxx
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE group_members (
    group_id    TEXT NOT NULL REFERENCES research_groups(id),
    user_id     TEXT NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL DEFAULT 'member', -- admin / member
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

-- 共享论文表
CREATE TABLE shared_papers (
    id              TEXT PRIMARY KEY,       -- shr-xxx
    paper_id        TEXT NOT NULL REFERENCES papers(id),
    shared_by       TEXT NOT NULL REFERENCES users(id),
    shared_with     TEXT,                    -- NULL = 全组共享
    shared_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 论文的 pgvector RLS 示例
ALTER TABLE paper_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY paper_chunks_isolation ON paper_chunks
    USING (
        user_id = current_setting('app.current_user_id')
        OR paper_id IN (
            SELECT sp.paper_id FROM shared_papers sp
            WHERE sp.shared_with IS NULL  -- 全组共享
               OR sp.shared_with = current_setting('app.current_user_id')
        )
    );
```

---

> 本文档定义了 v3 的完整数据库架构。业务表 22 张（含 captures），向量表 4 张，Redis 数据结构 18 组。迁移从 SQLite+ChromaDB 到 PostgreSQL+pgvector。
