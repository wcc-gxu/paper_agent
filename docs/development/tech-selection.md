# Paper Agent v3 — 技术选型文档

> 每个技术选择的原因、对比方案、权衡 | 2026-06-14

---

## 选型原则

1. **简历竞争力优先**：优先选择 JD 高频出现的技术
2. **架构优雅**：不过度工程，但也不牺牲正确性
3. **渐进复杂度**：核心路径简单，高级能力可选
4. **单用户优先**：MVP 为个人使用优化，预留多用户扩展

---

## 1. Agent 框架：LangGraph（替代自研状态机）

| 对比维度 | 自研 agent_loop.py | LangGraph |
|----------|-------------------|-----------|
| 状态管理 | 手动 DB 读写 | 内置 StateGraph + 自动 checkpoint |
| 条件分支 | if/else 硬编码 | 条件边（声明式） |
| 人机交互 | 自研 pause/resume | 内置 `interrupt()` + `Command` |
| 流式输出 | 无 | 原生 streaming |
| 崩溃恢复 | 自研 checkpoint | MemorySaver 自动持久化 |
| 多 Agent | 不支持 | `Send` API |
| JD 关键词 | 无 | ✅ LangGraph |
| 社区生态 | 无 | Anthropic 官方维护 |

**结论**：langgraph 替换自研状态机。自研代码（agent_loop.py execute() ~350 行）删除，类型定义和 Plan Phase 保留迁移。

---

## 2. 工具系统：LangChain BaseTool（替代 FastMCP + 自研）

| 对比维度 | @mcp.tool() | @register_tool (自研) | LangChain StructuredTool |
|----------|-------------|----------------------|---------------------------|
| 参数校验 | FastMCP 内置 | 自研 _infer_schema | Pydantic args_schema |
| LLM 集成 | MCP 协议 | 手动导出 to_anthropic() | 原生 tool_use |
| 流式 | 无 | 无 | 原生 |
| JD 关键词 | MCP | 无 | ✅ LangChain |
| 多格式导出 | 仅 MCP | Anthropic + OpenAI | 全部 + LangChain 生态 |

**结论**：ToolRegistry 保留作为唯一注册中心，底层工具包装为 LangChain `StructuredTool`。MCP Server 变为 ToolRegistry 的薄 MCP 适配器。CLI 不变。

---

## 3. 消息队列 & 事件总线：Redis

| 对比维度 | asyncio.Queue | SQLite 轮询 | Redis |
|----------|---------------|-------------|-------|
| 跨进程 | ❌ 单进程 | ✅ (读磁盘) | ✅ 原生 |
| Agent 重启恢复 | ❌ 丢事件 | ✅ | ✅ BRPOP 持久 |
| 延迟 | 0 (内存) | 高 (轮询) | <1ms |
| Celery 集成 | ❌ | ❌ | ✅ 原生 broker |
| JD 关键词 | 无 | SQLite | ✅ Redis |
| 新依赖 | 无 | 无 | ✅ |

**结论**：Redis。引入后同时承担事件队列、Celery Broker、速率限制、会话缓存。优点远超新增依赖的成本。

---

## 4. 异步任务：Celery + Redis Broker

| 对比维度 | asyncio.create_task | 自研线程池 | Celery |
|----------|--------------------|-----------|--------|
| 进程隔离 | ❌ 同进程 | ❌ 同进程 | ✅ 独立 Worker |
| 任务重试 | 手动 | 手动 | ✅ 内置指数退避 |
| 超时控制 | asyncio.wait_for | 手动 | ✅ soft/hard timeout |
| 进度上报 | 无 | 手动 | 自研 report() |
| 定时任务 | ❌ | ❌ | ✅ Celery Beat |
| JD 关键词 | asyncio | 无 | ✅ Celery |
| 新依赖 | 无 | 无 | ✅ |

**结论**：Celery。Worker 独立进程执行长任务（下载/转换/索引/综述），不阻塞 Agent 主线程。

### 4.1 子Agent 编排层（在 Celery 之上扩展）

Celery Worker 负责重量计算任务，但多阶段流水线（搜索→评估→下载→转换→索引→排名→综述）需要一个编排者来协调执行顺序、收集进度、处理异常。**子Agent（Python asyncio 协程 + 进度回调）** 作为编排层运行在 Agent 守护进程内：

- **轻量操作**（search、evaluate、rank）由子Agent 直接调用 Engine 同步执行
- **重量操作**（download、convert、index、survey）由子Agent 分发到 Celery Worker
- **进度收集**：每个阶段函数通过 `on_progress` 回调向子Agent 汇报进度
- **日志写入**：子Agent 通过 TaskLogger 将所有进度事件写入结构化 JSON 日志

```
子Agent (asyncio 协程)
  │
  ├── search_stage(on_progress)   → Engine.search()      [直接执行]
  ├── evaluate_stage(on_progress) → LLMClientV2          [直接执行]
  ├── download_stage(on_progress) → Celery Task          [分发到 Worker]
  ├── convert_stage(on_progress)  → Celery Task          [分发到 Worker]
  ├── index_stage(on_progress)    → Celery Task          [分发到 Worker]
  ├── rank_stage(on_progress)     → JournalRanker        [直接执行]
  └── survey_stage(on_progress)   → Celery Task          [分发到 Worker]
       │
       └── 所有进度 → TaskLogger → JSON 日志文件
```

---

## 5. 向量数据库：ChromaDB（保持现有）

| 对比维度 | ChromaDB | FAISS | Milvus | Pinecone |
|----------|----------|-------|--------|----------|
| 部署 | 嵌入式，零配置 | 嵌入式 | 独立服务 | 云服务 |
| 单用户 | ✅ | ✅ | ❌ 过重 | ❌ 付费 |
| 双 Collection | ✅ 已有 | 手动 | 原生 | 原生 |
| 迁移成本 | 0 (已在使用) | 高 | 高 | 高 |

**结论**：ChromaDB 不变。双 Collection（papers_abstract + papers_fulltext）+ 新增 agent_conversations。

---

## 6. 数据库：SQLite（保持现有）

| 对比维度 | SQLite | PostgreSQL |
|----------|--------|------------|
| 部署 | 零配置 | 独立服务 |
| 单用户性能 | 优秀 | 过度 |
| JSON 支持 | ✅ json_extract | ✅ JSONB |
| 备份 | 复制文件 | pg_dump |
| JD 关键词 | SQLite | PostgreSQL |

**结论**：SQLite 保持。单用户场景最优。预留 user_id 字段给未来多用户迁移。

---

## 7. 搜索引擎：保留现有 Provider 体系

```
providers/
├── arxiv_provider.py           (保持)
├── semanticscholar_provider.py (保持)
├── pubmed_provider.py          (保持)
├── ieee_provider.py            (保持)
├── sciencedirect_provider.py   (保持)
├── cnki_provider.py            (保持)
└── [Phase 2] github_provider.py (新增)
              web_scraper.py      (新增)
              stackoverflow.py    (新增)
```

**结论**：6 个论文 Provider 完全不动。Phase 2 新增 3 个技术文档 Provider。

---

## 8. Web 框架：FastAPI（新增）

| 对比维度 | FastAPI | Flask | 纯 WS 无框架 |
|----------|---------|-------|-------------|
| 原生 async | ✅ | ❌ | 手动 |
| WebSocket | ✅ | 需扩展 | 手动 |
| OpenAPI 文档 | ✅ 自动 | 需扩展 | ❌ |
| JD 关键词 | ✅ FastAPI | Flask | 无 |

**结论**：FastAPI。提供 REST（知识库 CRUD）+ WebSocket（对话通道）。

---

## 9. LLM 客户端：自研 LLMClientV2（保持）

| 对比维度 | 自研 LLMClientV2 | LangChain ChatOpenAI | 直接 httpx |
|----------|-----------------|---------------------|------------|
| 多供应商 | ✅ Volcano/OpenAI/Anthropic | ✅ | 手动 |
| 流式 | ✅ SSE 解析 | ✅ | 手动 |
| 重试 | ✅ 指数退避 | ✅ | 手动 |
| 速率限制 | ✅ 滑动窗口 | ❌ | 手动 |
| 迁移成本 | 0 (已有) | 中 | 高 |

**结论**：LLMClientV2 保留。LangGraph 的 LLM 节点使用 LangChain ChatOpenAI（对接 LangGraph 的 streaming），底层仍委托 LLMClientV2 的 HTTP 能力。

---

## 10. 部署：Docker + docker-compose

| 对比维度 | 纯 systemd | Docker 单容器 | docker-compose 全家桶 |
|----------|-----------|---------------|-----------------------|
| 跨平台 | ❌ Linux only | ✅ | ✅ |
| Redis + Celery 管理 | 手动 | 手动 | ✅ 一体 |
| JD 关键词 | 无 | Docker | ✅ Docker + docker-compose |
| 生产级 | ✅ | ⚠️ | ✅ |

**结论**：docker-compose 管理 3 个 service（agent + celery_worker + redis）。

---

## 技术栈总览

| 层级 | 技术 | 状态 |
|------|------|------|
| Agent 框架 | LangGraph | 新增 |
| 工具系统 | LangChain BaseTool + ToolRegistry | 改造 |
| LLM 客户端 | LLMClientV2 (Volcano/OpenAI/Anthropic) | 保持 |
| 消息队列 | Redis | 新增 |
| 异步任务 | Celery + Redis Broker | 新增 |
| 定时任务 | Celery Beat | 新增 |
| Web 框架 | FastAPI + WebSocket | 新增 |
| 向量数据库 | ChromaDB | 保持 |
| 关系数据库 | SQLite | 保持 |
| 搜索引擎 | 6 个 Provider (arxiv/s2/pubmed/ieee/sciencedirect/cnki) | 保持 |
| 部署 | Docker + docker-compose | 新增 |
| PDF 转换 | pymupdf4llm | 保持 |
| 进度日志 | TaskLogger + JSONL | 新增 |
| 子Agent编排 | Python asyncio 协程 + 进度回调 | 新增 |
| iOS 推送 | APNs | 新增 |

---

> 版本: v1.1 | 新增进度日志与子Agent选型
