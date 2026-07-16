# [DEPRECATED] 智驭·研 v3 数据迁移方案

> 从 SQLite + ChromaDB（单用户）迁移到 PostgreSQL + pgvector（多用户）
>
> 日期：2026-07-10

---

## 目录

1. [迁移概览](#1-迁移概览)
2. [存量数据分析](#2-存量数据分析)
3. [源→目标映射](#3-源目标映射)
4. [迁移脚本设计](#4-迁移脚本设计)
5. [迁移执行流程](#5-迁移执行流程)
6. [存量数据处理规则](#6-存量数据处理规则)
7. [回滚方案](#7-回滚方案)
8. [验收清单](#8-验收清单)

---

## 1. 迁移概览

### 1.1 迁移范围

```
迁移前                                   迁移后
┌─────────────────────────┐           ┌─────────────────────────┐
│ SQLite                   │           │ PostgreSQL 16+          │
│ ~/.paper_search/agent.db│  ──────→  │ 16 张业务表              │
│                          │           │                         │
│ LangGraph Store          │           │ LangGraph Store         │
│ (同 SQLite 库)           │           │ (同 PostgreSQL 库)       │
│                          │           │                         │
│ ChromaDB                 │           │ pgvector 0.8+           │
│ ~/.paper_search/chroma/  │  ──────→  │ 4 张向量表              │
│                          │           │                         │
│ 论文文件                  │           │ 论文文件                 │
│ ~/papers/markdown/       │  ──────→  │ ~/papers/markdown/      │
│ ~/papers/outputs/        │           │ ~/papers/outputs/       │
└─────────────────────────┘           └─────────────────────────┘
```

### 1.2 关键挑战

| 挑战 | 说明 | 策略 |
|------|------|------|
| **存量数据无 user_id** | 当前系统是单用户设计，所有表无 `user_id` 列 | 迁移时统一分配 `user_id="user-default"` |
| **Schema 差异** | 目标表有新增列（`user_id`、`tl_dr`、`source_priority` 等） | 映射规则处理缺失列 → 用默认值填充 |
| **向量格式** | ChromaDB 内部存储格式 vs pgvector `vector()` 类型 | 逐条读取 ChromaDB → numpy → SQL INSERT |
| **停机窗口** | 生产环境可能有正在运行的任务 | 支持离线迁移（停机 30-60 分钟） |
| **Checkpointer 数据** | LangGraph 标准表 `checkpoints/checkpoint_blobs/checkpoint_writes` | 直接 SQL 迁移，schema 与 LangGraph 官方一致 |

---

## 2. 存量数据分析

### 2.1 当前 SQLite 表清单（14 张）

| 表名 | 预估数据量 | 迁移风险 | 说明 |
|------|:---:|:---:|------|
| `projects` | 10-100 | 低 | 项目信息 |
| `papers` | 100-5000 | **高** | 核心数据，含文件路径引用 |
| `project_papers` | 100-10000 | 中 | N:M 关联表 |
| `search_logs` | 100-5000 | 低 | 可丢弃（选择保留） |
| `journal_ranks` | 50-500 | 低 | 可重建 |
| `citations` | 100-10000 | 中 | 引用关系 |
| `agent_tasks` | 100-500 | 中 | 任务历史 |
| `task_steps` | 500-5000 | 中 | 任务明细 |
| `sessions` | 100-500 | 中 | 会话记录 |
| `ws_messages` | 1000-50000 | **高** | 消息量最大 |
| `device_tokens` | 1-10 | 低 | 少量数据 |
| `agent_events` | 1000-5000 | 低 | Phase 4 可丢弃 |
| `videos` | 1-50 | 低 | 视频记录 |
| `subscriptions` | 1-10 | 低 | 订阅配置 |

### 2.2 当前 ChromaDB Collections（3 个）

| Collection | 预估向量数 | embedding 维度 | 说明 |
|------------|:---:|:---:|------|
| `papers_abstract` | 100-5000 | 1024 (doubao) | 标题+摘要向量 |
| `papers_fulltext` | 500-50000 | 1024 | 章节分块向量（最大） |
| `glossary_terms` | 0-500 | 1024 | 术语向量（可能为空） |

### 2.3 文件存储

| 路径 | 内容 | 预估量 | 迁移操作 |
|------|------|:---:|----------|
| `~/papers/markdown/` | PDF→MD 转换输出 | 100-5000 个 | **不移动**（路径不变，仅更新 DB 引用） |
| `~/papers/outputs/` | 项目输出 | 10-100 个 | **不移动** |
| `~/.paper_search/videos/` | 视频/音频/转录 | 1-50 组 | **不移动** |
| `~/.paper_search/chroma/` | ChromaDB 持久化 | — | 迁移后归档 |

### 2.4 LangGraph Store 表

| 表 | 数据量 | 说明 |
|----|:---:|------|
| `store_data` | 100-1000 | LangGraph Store 双后端数据 |
| `checkpoints` | 100-1000 | LangGraph Checkpointer |
| `checkpoint_blobs` | 100-1000 | state 大对象 |
| `checkpoint_writes` | 100-1000 | 写入记录 |

---

## 3. 源→目标映射

### 3.1 SQLite → PostgreSQL 表映射（14+5 张）

**保留表（结构迁移，数据全量迁移）**：

| # | 源表 (SQLite) | 目标表 (PostgreSQL) | 差异 |
|:---:|------|------|------|
| 1 | `projects` | `projects` | +`user_id` 列 |
| 2 | `papers` | `papers` | +`user_id`、+`tl_dr`、+`status`、+`duplicate_of`、+`alternate_versions`、`json_extract→jsonb` |
| 3 | `project_papers` | `project_papers` | ID 格式需要标准化（INT → `pp-xxx`） |
| 4 | `search_logs` | `search_logs` | +`user_id` |
| 5 | `journal_ranks` | `journal_ranks` | 几乎一致 |
| 6 | `citations` | `citations` | 结构变化较大（见下文） |
| 7 | `agent_tasks` | `agent_tasks` | +`agent_name`、`task_kind` |
| 8 | `task_steps` | `task_steps` | +`step_order`、`detail` JSONB |
| 9 | `sessions` | `sessions` | +`user_id`、+`metadata` |
| 10 | `ws_messages` | `ws_messages` | +`user_id`、+`priority_kind`、+`is_delivered`、+`delivered_at` |
| 11 | `device_tokens` | 保留在当前路径（不迁移） | 或临时文件 → `user_configs` |
| 12 | `agent_events` | `agent_events` | 结构一致（Phase 4 Checkpoint 准备） |
| 13 | `videos` | `videos` | +`user_id`、+`metadata` |
| 14 | `subscriptions` | `subscriptions` | +`user_id`、+`frequency` |

**新增表（结构创建，无历史数据）**：

| # | 目标表 | 初始数据 |
|:---:|------|------|
| 15 | `users` | 1 条记录：`user-default`（存量数据归属） |
| 16 | `user_configs` | 空 |
| 17 | `glossary_terms` | 空（数据从 ChromaDB `glossary_terms` 迁移） |
| 18 | `writing_templates` | 系统预置模板（INSERT 初始化脚本） |
| 19 | `external_validations` | 空 |
| 20 | `hallucination_events` | 空 |
| 21 | `conversation_archive` | 空 |

### 3.2 ChromaDB → pgvector 映射（3→4 个表）

| # | 源 Collection | 目标表 | 说明 |
|:---:|------|------|------|
| 1 | `papers_abstract` | 已融入 `paper_chunks` (chunk_type='abstract') | 不单独建表，与 fulltext 统一 |
| 2 | `papers_fulltext` | `paper_chunks` (chunk_type='body') | 主要向量数据 |
| 3 | `glossary_terms` | `glossary_embeddings` | 术语向量独立存储 |

**向量表（全部新建）**：

| # | 目标表 | 初始数据 |
|:---:|------|------|
| 1 | `paper_chunks` | 从 ChromaDB `papers_abstract` + `papers_fulltext` 迁移 |
| 2 | `glossary_embeddings` | 从 ChromaDB `glossary_terms` 迁移 |
| 3 | `session_summaries` | 空 |
| 4 | `topic_embeddings` | 空 |

### 3.3 LangGraph Store 迁移

| 源表 (SQLite) | 目标表 (PostgreSQL) | 说明 |
|------|------|------|
| `store_data` | PostgreSQL `store` schema 下的表 | 需参考 LangGraph PostgreSQL Store 文档 |
| `checkpoints` | PostgreSQL `checkpoints` 表 | LangGraph 官方支持 PostgreSQL checkpointer |
| `checkpoint_blobs` | PostgreSQL `checkpoint_blobs` 表 | 同上 |
| `checkpoint_writes` | PostgreSQL `checkpoint_writes` 表 | 同上 |

> 注：LangGraph 0.3+ 原生支持 `AsyncPostgresSaver`，迁移时直接使用官方实现，表结构由 LangGraph 自动管理。

---

## 4. 迁移脚本设计

### 4.1 脚本文件结构

```
scripts/
├── migrate_to_postgres.py          # 主入口
├── migrate/
│   ├── __init__.py
│   ├── config.py                   # 源/目标数据库连接配置
│   ├── analyzer.py                 # 存量数据分析（行数/大小/预估时间）
│   ├── sqlite_migrator.py          # SQLite → PostgreSQL 表迁移
│   ├── chroma_migrator.py          # ChromaDB → pgvector 向量迁移
│   ├── store_migrator.py           # LangGraph Store 迁移
│   ├── file_validator.py           # 文件路径验证
│   ├── verifier.py                 # 迁移后校验
│   └── rollback.py                 # 回滚工具
└── init_db.sql                     # PostgreSQL schema 初始化
```

### 4.2 核心类设计

```python
# scripts/migrate/migrate_to_postgres.py

class MigrationRunner:
    """数据迁移主控制器"""

    def __init__(self, sqlite_path: str, pg_url: str, chroma_path: str):
        self.sqlite = aiosqlite.connect(sqlite_path)
        self.pg = asyncpg.connect(pg_url)
        self.chroma = chromadb.PersistentClient(path=chroma_path)
        self.default_user_id = "user-default"

    async def run(self, phases: list[str] = None):
        """
        phases: ["analyze", "sqlite", "chroma", "store", "verify"]
        默认执行全部
        """
        results = MigrationReport()

        if "analyze" in phases:
            results.analysis = await self._analyze()

        if "sqlite" in phases:
            results.sqlite = await self._migrate_sqlite()

        if "chroma" in phases:
            results.chroma = await self._migrate_chroma()

        if "store" in phases:
            results.store = await self._migrate_store()

        if "verify" in phases:
            results.verification = await self._verify()

        return results

    # ── SQLite 迁移 ────────────────────────────

    async def _migrate_sqlite(self) -> SQLiteMigrationReport:
        """14 张表逐表迁移"""
        tables = [
            # (源表名, 目标表名, 行映射函数, 可选: 清空目标表)
            ("projects",       "projects",       self._map_project),
            ("papers",         "papers",         self._map_paper),
            ("project_papers", "project_papers", self._map_project_paper),
            ("search_logs",    "search_logs",    self._map_search_log),
            ("journal_ranks",  "journal_ranks",  self._map_journal_rank),
            ("citations",      "citations",      self._map_citation),
            ("agent_tasks",    "agent_tasks",    self._map_agent_task),
            ("task_steps",     "task_steps",     self._map_task_step),
            ("sessions",       "sessions",       self._map_session),
            ("ws_messages",    "ws_messages",    self._map_ws_message),
            ("agent_events",   "agent_events",   self._map_agent_event),
            ("videos",         "videos",         self._map_video),
            ("subscriptions",  "subscriptions",  self._map_subscription),
            ("device_tokens",  "user_configs",   self._map_device_token),
        ]

        report = SQLiteMigrationReport()
        for src_table, dst_table, mapper in tables:
            src_rows = await self._get_sqlite_rows(src_table)
            dst_rows = [mapper(r) for r in src_rows]
            await self._pg_batch_insert(dst_table, dst_rows)
            report.add_table(src_table, len(src_rows), len(dst_rows))

        # 写入默认用户
        await self._insert_default_user()
        return report

    # ── 行映射函数示例 ─────────────────────────

    def _map_paper(self, row: dict) -> dict:
        """SQLite papers 行 → PostgreSQL papers 行"""
        return {
            "id":         row["id"],                     # 保持原 ID
            "user_id":    self.default_user_id,          # ★ 统一分配
            "title":      row["title"],
            "authors":    self._json_parse(row.get("authors", "[]")),
            "year":       row.get("year"),
            "venue":      row.get("journal") or row.get("conference"),
            "venue_type": self._infer_venue_type(row),
            "doi":        row.get("doi"),
            "arxiv_id":   row.get("arxiv_id"),
            "abstract":   row.get("abstract", ""),
            "keywords":   self._json_parse(row.get("keywords", "[]")),
            "source":     row.get("source", "manual"),
            "source_priority": self._get_source_priority(row),
            "file_path":  row.get("pdf_path"),           # PDF 文件路径
            "md_path":    row.get("markdown_path"),      # MD 文件路径
            "figures_dir": None,                          # 存量数据无
            "metadata":   self._build_metadata(row),      # 剩余字段进 metadata
            "citation_count": row.get("citation_count", 0),
            "tl_dr":      row.get("digest") or row.get("tl_dr"),
            "status":     "active",
            "duplicate_of": None,
            "alternate_versions": "[]",
            "created_at": row.get("created_at", "now()"),
            "updated_at": row.get("updated_at", "now()"),
        }

    # ── ChromaDB 迁移 ─────────────────────────

    async def _migrate_chroma(self) -> ChromaMigrationReport:
        """3 个 collection → 2 个 pgvector 表"""
        report = ChromaMigrationReport()

        # 1. papers_abstract → paper_chunks (chunk_type='abstract')
        abstract_chunks = await self._migrate_collection(
            collection_name="papers_abstract",
            target_table="paper_chunks",
            chunk_type="abstract",
            batch_size=500,
        )
        report.add("papers_abstract", abstract_chunks)

        # 2. papers_fulltext → paper_chunks (chunk_type='body')
        fulltext_chunks = await self._migrate_collection(
            collection_name="papers_fulltext",
            target_table="paper_chunks",
            chunk_type="body",
            batch_size=500,
        )
        report.add("papers_fulltext", fulltext_chunks)

        # 3. glossary_terms → glossary_embeddings
        glossary_count = await self._migrate_collection(
            collection_name="glossary_terms",
            target_table="glossary_embeddings",
            batch_size=100,
        )
        report.add("glossary_terms", glossary_count)

        # 4. 创建向量索引（迁移后统一创建，先迁移再建索引才快）
        await self._create_vector_indexes()

        return report

    async def _migrate_collection(
        self, collection_name: str, target_table: str,
        chunk_type: str = None, batch_size: int = 500,
    ) -> int:
        """单个 ChromaDB collection → pgvector 表"""
        collection = self.chroma.get_collection(collection_name)
        total = collection.count()
        migrated = 0

        for offset in range(0, total, batch_size):
            batch = collection.get(
                limit=batch_size,
                offset=offset,
                include=["embeddings", "metadatas", "documents"],
            )

            rows = []
            for i in range(len(batch["ids"])):
                embedding = batch["embeddings"][i]
                metadata = batch["metadatas"][i] or {}
                document = batch["documents"][i] or ""

                row = {
                    "id":          self._gen_chunk_id(metadata, i, offset),
                    "paper_id":    metadata.get("paper_id", "unknown"),
                    "user_id":     self.default_user_id,
                    "chunk_text":  document,
                    "chunk_type":  chunk_type or metadata.get("type", "body"),
                    "section_title": metadata.get("section"),
                    "section_level": metadata.get("section_level"),
                    "chunk_order": metadata.get("chunk_order", offset + i),
                    "embedding":   self._format_vector(embedding),
                    "token_count": len(document.split()) if document else 0,
                    "metadata":    json.dumps(metadata),
                    "created_at":  "now()",
                }
                rows.append(row)

            await self._pg_batch_vector_insert(target_table, rows)
            migrated += len(rows)

        return migrated

    def _format_vector(self, embedding) -> str:
        """将 numpy/list 转为 pgvector 格式字符串"""
        if isinstance(embedding, list):
            return f"[{','.join(str(x) for x in embedding)}]"
        if hasattr(embedding, 'tolist'):
            return f"[{','.join(str(x) for x in embedding.tolist())}]"
        raise ValueError(f"Unknown embedding type: {type(embedding)}")

    # ── 迁移前分析 ─────────────────────────────

    async def _analyze(self) -> MigrationAnalysis:
        """分析存量数据，预估迁移时间"""
        analysis = MigrationAnalysis()

        # SQLite 数据量
        for table in self.ALL_SQLITE_TABLES:
            count = await self.sqlite.fetchval(f"SELECT COUNT(*) FROM {table}")
            analysis.add_table(table, count)

        # ChromaDB 数据量
        for col_name in ["papers_abstract", "papers_fulltext", "glossary_terms"]:
            col = self.chroma.get_collection(col_name)
            count = col.count()
            analysis.add_collection(col_name, count)

        # 预估时间
        analysis.estimate_duration()
        return analysis
```

### 4.3 关键技术点

| 技术点 | 实现方式 |
|--------|----------|
| **批量插入** | `asyncpg.copy_records_to_table` 或 `execute_values`，每批 500-1000 行 |
| **向量格式化** | `numpy → list → pgvector 兼容字符串` `'[0.1,0.2,...]'` |
| **文件路径验证** | 迁移前扫描 `~/papers/markdown/` 确认文件存在→更新 `md_path`，缺失→标记 |
| **事务处理** | 每张表独立事务，失败不阻塞其他表 |
| **进度输出** | 实时输出 `[3/14] papers: 2340/2340 OK` |
| **断点续传** | `--resume-from=papers` 参数支持从中间表继续 |

---

## 5. 迁移执行流程

### 5.1 离线迁移（推荐，停机 30-60 分钟）

```
Phase 0: 准备（不涉及停机）
  ├── ① 在目标机器部署 PostgreSQL 16 + pgvector
  ├── ② 执行 init_db.sql 创建所有表
  ├── ③ 验证 pgvector 扩展：SELECT extname FROM pg_extension;
  └── ④ 配置 PostgreSQL 连接（DATABASE_URL 环境变量）

       ▼ 停机开始

Phase 1: 存量分析（5 分钟）
  ├── 分析 SQLite 表行数
  ├── 分析 ChromaDB 向量数
  ├── 扫描文件路径
  └── 输出预估时间 + 磁盘空间

Phase 2: SQLite → PostgreSQL（15-30 分钟）
  ├── 14 张表逐表迁移
  ├── 每张表独立事务（失败不阻塞其他）
  ├── 写入默认用户 user-default
  └── 验证行数一致

Phase 3: ChromaDB → pgvector（10-20 分钟）
  ├── 3 个 collection → 2 个 pgvector 表
  ├── 批量迁移（每批 500 条）
  ├── 创建向量索引（迁移后统一创建）
  └── 验证向量数量一致

Phase 4: LangGraph Store 迁移（5-10 分钟）
  ├── store_data → PostgreSQL
  ├── checkpoints / checkpoint_blobs / checkpoint_writes → PostgreSQL
  └── 验证 thread_id resume 可用

Phase 5: 验证（5 分钟）
  ├── 行数对比
  ├── 抽样内容对比
  ├── 向量检索对比（10 条固定 query）
  ├── 文件路径验证
  └── 端到端回归测试

       ▼ 停机结束

Phase 6: 切流
  ├── 修改 config.py → DATABASE_URL 指向 PostgreSQL
  ├── 重启服务
  └── 灰度验证（单用户 → 全量）
```

### 5.2 灰度切换策略

```
步骤 1: 部署新代码（双写模式可选）
  config.py:
    DATABASE_URL = "postgresql://..."    # 主库：PostgreSQL
    # SQLITE_URL = "sqlite:///..."        # 保留，不启用

步骤 2: 单用户灰度
  - 用测试 user_id 发消息 → 确认正常
  - 检查 pgvector 检索结果
  - 检查术语词表功能

步骤 3: 全量切换
  - 所有用户指向 PostgreSQL
  - 保留 SQLite 文件 30 天作为备份

步骤 4: 归档清理
  - 30 天后确认无问题 → 删除 SQLite + ChromaDB 数据目录
  - 或归档到冷存储
```

### 5.3 执行命令

```bash
# 1. 初始化 PostgreSQL schema
psql $DATABASE_URL -f scripts/init_db.sql

# 2. 分析存量数据
python scripts/migrate_to_postgres.py --phase analyze

# 3. 执行迁移（全量）
python scripts/migrate_to_postgres.py --phase all

# 4. 仅迁移指定阶段
python scripts/migrate_to_postgres.py --phase sqlite    # 仅 SQLite 业务数据
python scripts/migrate_to_postgres.py --phase chroma    # 仅 ChromaDB 向量
python scripts/migrate_to_postgres.py --phase store     # 仅 LangGraph Store
python scripts/migrate_to_postgres.py --phase verify    # 仅验证

# 5. 断点续传（从指定表继续）
python scripts/migrate_to_postgres.py --phase sqlite --resume-from papers

# 6. 回滚
python scripts/migrate_to_postgres.py --phase rollback
```

---

## 6. 存量数据处理规则

### 6.1 user_id 统一分配

```
所有存量数据的 user_id = "user-default"

迁移后首次启动 → 自动创建新用户（如 "user-张三"）→ 存量数据仍属 "user-default"
管理员可通过 SQL 转移数据归属：
  UPDATE papers SET user_id = 'user-张三' WHERE user_id = 'user-default';
```

### 6.2 列级映射（缺失列处理）

| 目标列 | 源表缺列时的默认值 | 说明 |
|--------|-----|------|
| `user_id` | `"user-default"` | 存量数据统一归属 |
| `venue_type` | 推断：有期刊名→`journal`，有会议名→`conference`，无→`preprint` | — |
| `source_priority` | arXiv → 20, 正式会议 → 40, 其他 → 30 | 供去重排序 |
| `tl_dr` | 从 `digest` 列取值（如有） | 旧系统可能叫 digest |
| `status` | `"active"` | — |
| `metadata` | `{}` | 剩余未映射字段打包进此列 |
| `figures_dir` | `NULL` | 存量数据无 |
| `duplicate_of` | `NULL` | 迁移后由去重算法填充 |
| `alternate_versions` | `[]` | 同上 |
| `display_name` (users) | `"默认用户"` | — |
| `api_token` (users) | `"tok-migrated-default"` | 迁移完成后应立即更换 |

### 6.3 ID 格式标准化

```python
# 旧 ID 可能是 INT 或其他格式，新 ID 必须带前缀
ID_FORMAT_RULES = {
    "projects":       {"prefix": "prj", "old_col": "id"},
    "papers":         {"prefix": "pap", "old_col": "id"},
    "project_papers": {"prefix": "pp",  "old_col": "id"},
    "citations":      {"prefix": "cit", "old_col": "id"},
    "agent_tasks":    {"prefix": "task","old_col": "id"},
    "task_steps":     {"prefix": "step","old_col": "id"},
    "sessions":       {"prefix": "sess","old_col": "id"},
    "ws_messages":    {"prefix": "msg", "old_col": "id"},
    "videos":         {"prefix": "vid", "old_col": "id"},
    "subscriptions":   {"prefix": "sub", "old_col": "id"},
}

def normalize_id(old_id, table_name: str) -> str:
    rule = ID_FORMAT_RULES.get(table_name)
    if not rule:
        return str(old_id)
    if str(old_id).startswith(rule["prefix"] + "-"):
        return str(old_id)  # 已是标准格式
    return f"{rule['prefix']}-{old_id}"
```

### 6.4 JSON 字段处理

```python
# SQLite 中 JSON 存为 TEXT，迁移到 PostgreSQL JSONB

def _json_parse(value):
    """安全解析 JSON 字符串"""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return json.dumps([value])  # 单字符串 → 列表
    if value is None:
        return "[]" if value is None else "{}"
    return json.dumps(value, ensure_ascii=False)
```

### 6.5 文件路径验证

```python
async def validate_file_paths(paper_rows):
    """迁移前验证论文文件路径"""
    missing = []
    for row in paper_rows:
        md_path = row.get("markdown_path")
        pdf_path = row.get("pdf_path")

        if md_path and not os.path.exists(md_path):
            missing.append({
                "paper_id": row["id"],
                "missing_path": md_path,
                "type": "markdown",
            })

        if pdf_path and not os.path.exists(pdf_path):
            missing.append({
                "paper_id": row["id"],
                "missing_path": pdf_path,
                "type": "pdf",
            })

    if missing:
        logger.warning(f"{len(missing)} file paths are missing on disk")
        for m in missing[:10]:
            logger.warning(f"  {m['paper_id']}: {m['missing_path']}")

    return len(missing)
```

### 6.6 废弃表处理

```
以下表仅存在于当前 SQLite，不需要迁移到 PostgreSQL：

  - device_tokens → 转为 user_configs 记录
  - agent_events  → 由 LangGraph Checkpointer 替代（可选迁移）
  - task_checkpoints → 已废弃
```

---

## 7. 回滚方案

### 7.1 快速回滚（迁移失败或发现问题）

```bash
# 方案 A：切回 SQLite（最简单）
export DATABASE_URL=""   # 清空 PostgreSQL 连接
# 重启服务 → 自动回退到 SQLite

# 方案 B：从备份恢复 PostgreSQL
pg_restore -d $DATABASE_URL backup/pre_migration.dump
```

### 7.2 回滚条件

| 触发条件 | 操作 |
|----------|------|
| 迁移后行数不一致（任一张表差异 > 0） | 自动终止，标记 FAILED |
| 向量检索对比失败（10 条 query 差异 > 2 条） | 标记 WARNING，需人工决策 |
| 文件路径缺失 > 10% | 标记 WARNING |
| 端到端回归测试失败 | 回滚 |

### 7.3 回滚脚本

```python
# scripts/migrate/rollback.py

async def rollback(tables: list[str] = None):
    """清空指定（或全部）PostgreSQL 迁移表"""
    if tables is None:
        tables = ALL_TARGET_TABLES

    for table in tables:
        await pg.execute(f"TRUNCATE TABLE {table} CASCADE")
        logger.info(f"ROLLBACK: truncated {table}")

    # 不删除 init_db.sql 创建的 schema 结构
    logger.info("ROLLBACK complete. Schema retained for re-migration.")
```

---

## 8. 验收清单

| # | 验收项 | 级别 | 验证方式 |
|---|--------|:---:|------|
| **数据完整性** |
| V1 | 14 张业务表行数与 SQLite 源表一致 | P0 | `migrate_to_postgres.py --phase verify` |
| V2 | 3 个 ChromaDB collection 的向量元组数 = pgvector 目标表行数 | P0 | 同上 |
| V3 | 10 条固定 query Rec@5 = chroma_store vs pgvector_store | P0 | 验证脚本 |
| V4 | 抽样 20 篇论文 → 内容逐字段对比 | P0 | 人工 + 脚本 |
| V5 | 论文文件路径指向的文件存在 | P1 | 扫描验证 |
| **多用户** |
| V6 | 所有存量数据 `user_id = "user-default"` | P0 | SQL 查询 |
| V7 | 新用户查询 → 仅看到自己的论文（为空） | P0 | 端到端测试 |
| V8 | `user-default` 查询 → 看到全部存量论文 | P0 | 端到端测试 |
| **功能回归** |
| V9 | RAG 检索正常工作 | P0 | 端到端测试 |
| V10 | 文献调研 → 搜索 → 下载 → 入库 全流程正常 | P0 | 端到端测试 |
| V11 | iOS 客户端正常收发消息 | P0 | 手动测试 |
| V12 | LangGraph thread_id resume 可用 | P0 | 验证 Checkpointer |
| **性能** |
| V13 | pgvector 检索 p95 < 500ms（50 万 chunk 级） | P1 | 性能基准测试 |
| V14 | 迁移总耗时 < 60 分钟（预估数据量范围） | P1 | 计时 |
| **安全** |
| V15 | 迁移后 SQLite 文件保留在原始路径（30 天备份） | P0 | 确认文件存在 |
| V16 | ChromaDB 目录保留（30 天备份） | P0 | 同上 |

---

> **迁移完成后**：将 `~/.paper_search/agent.db` 和 `~/.paper_search/chroma/` 归档保留 30 天，确认无问题后删除。
