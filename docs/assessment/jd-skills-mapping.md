# Paper Agent v3 — JD 关键词/技术 → 项目实现对照表

> 求职技能覆盖度评估 | 2026-06-14

---

## 阅读说明

| 完成度 | 含义 |
|--------|------|
| ✅ 已完成 | 代码存在且可用 |
| 🔧 重构中 | 代码存在但需修 Bug/适配 |
| 📋 规划中 | 设计已确定，待实现 |
| 🔮 Phase 2 | 后续版本规划 |

---

## 1. AI Agent / LLM 应用开发

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **Agent 开发** | Plan-then-Execute 双 LangGraph 图 Agent | 📋 规划中 |
| **Multi-Agent 协作** | Supervisor + Search/Analysis/Report 子 Agent 接口预留 | 🔮 Phase 2 |
| **ReAct / Tool Use** | LLM Tool Use 自主决策，Server 工具 + iOS 工具双类 | 📋 规划中 |
| **Autonomous Agent** | 24/7 常驻守护进程，完全主动（订阅推送 + 主动发现） | 📋 规划中 |
| **Human-in-the-Loop** | LangGraph `interrupt()` 关键步骤暂停等用户确认 | 📋 规划中 |
| **Agent 状态管理** | ResearchState + LangGraph MemorySaver checkpoint | 📋 规划中 |

---

## 2. LangChain / LangGraph 生态

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **LangChain** | LangChain BaseTool/StructuredTool 工具定义 | 📋 规划中 |
| **LangGraph** | StateGraph 双图结构 (Plan Graph + Execute Graph) | 📋 规划中 |
| **LangSmith/LangFuse** | 未集成 (MVP 不包含) | ❌ |
| **LangChain Memory** | 部分使用 (ConversationSummaryBuffer, MemorySaver) | 📋 规划中 |
| **LangChain RAG** | LangChain VectorStore 接口 + ChromaDB | 📋 规划中 |
| **LCEL** | 未使用 (项目中无链式声明式需求) | ❌ |

---

## 3. RAG / 向量检索 / 知识库

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **RAG** | ChromaDB 双 Collection + LLM Tool Use 自主检索 | 🔧 重构中 |
| **向量数据库** | ChromaDB (papers_abstract + papers_fulltext + conversations + knowledge) | ✅ 已完成 |
| **Embedding** | 火山引擎 Embedding API | ✅ 已完成 |
| **Reranker** | Cross-Encoder (sentence-transformers / BGE-Reranker) | 📋 规划中 |
| **文档分块** | SectionChunker (section-aware 论文分块) | ✅ 已完成 |
| **多跳检索** | Query Decomposition (复杂问题拆解) | 📋 规划中 |
| **引用校验** | CitationVerifier (DB 匹配 + 原文对照) | 🔧 重构中 |
| **知识提取** | LLM 结构化提取 (contribution/method/dataset/limitation) | ✅ 已完成 |

---

## 4. 后端工程

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **Python 异步** | asyncio 全栈 (FastAPI + Celery + Redis + httpx) | ✅ 已完成 |
| **FastAPI** | REST API + WebSocket + SSE | 📋 规划中 |
| **WebSocket** | 全双工对话通道 (4 种事件 + 会话恢复) | 📋 规划中 |
| **API 认证** | API Key 中间件 | 📋 规划中 |
| **速率限制** | Redis 滑动窗口 | 📋 规划中 |
| **错误处理** | 全局异常处理 + 结构化 ErrorResponse | 📋 规划中 |
| **流式输出** | SSE (LLM token streaming) + WS (status push) | 📋 规划中 |

---

## 5. 数据库 & 存储

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **SQLite** | AgentDB (12 表: projects/papers/documents/knowledge/citations/...) | ✅ 已完成 |
| **ChromaDB** | 双 Collection + embedding, 单用户嵌入式 | ✅ 已完成 |
| **数据库设计** | 多项目类型 documents 抽象层 + metadata JSON 灵活字段 | 📋 规划中 |
| **数据迁移** | SQLite schema 版本管理 (目前手动) | ❌ |

---

## 6. 异步任务 & 消息队列

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **Celery** | Celery Worker 异步长任务 (下载/转换/索引/综述) | 📋 规划中 |
| **Redis** | 事件总线 (BRPOP) + Celery Broker + 速率限制 | 📋 规划中 |
| **定时任务** | Celery Beat (health_check / subscriptions / trending / cleanup) | 📋 规划中 |
| **任务重试** | Celery 内置指数退避 + Agent 层自动重试 ≤2 次 | 📋 规划中 |

---

## 7. LLM 集成

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **多供应商 LLM** | LLMClientV2 (Volcano/OpenAI/Anthropic/任意兼容 API) | ✅ 已完成 |
| **流式输出** | SSE 解析 + chat_stream() 生成器 | ✅ 已完成 |
| **指数退避重试** | LLMClientV2 内置 (最多 3 次, base_delay 1s) | ✅ 已完成 |
| **速率限制** | 滑动窗口 RPM/TPM (LLMClientV2 内置) | ✅ 已完成 |
| **结构化输出** | chat_json() + Pydantic schema 约束 | ✅ 已完成 |
| **Function Calling** | Anthropic tool_use 格式 + OpenAI function calling | ✅ 已完成 |
| **Token 计数** | 当前 len(text)//3 → 升级 tiktoken 精确计数 | 🔧 重构中 |

---

## 8. Prompt Engineering

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **Prompt 管理** | prompts.py 统一管理 (Plan/Verify/Parse/Clarify/Generate) | 📋 规划中 |
| **提示词优化** | 3 阶段 Pipeline (Parse → Clarify → Generate) | ✅ 已完成 |
| **Few-shot / 示例** | 未系统化 (各 prompt 内嵌示例) | ❌ |
| **Prompt 版本管理** | 无 | ❌ |
| **System Prompt 设计** | 24/7 完全主动科研助理 System Prompt (待精调) | 📋 规划中 |

---

## 9. MCP 协议

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **MCP Server** | FastMCP 13 个工具, stdio 传输 | ✅ 已完成 |
| **MCP Tool 设计** | 参数 JSON Schema + description + 分类 | ✅ 已完成 |
| **MCP 适配** | ToolRegistry → to_mcp() 格式导出 | 📋 规划中 |
| **MCP Client** | 未实现 (Claude Code 作为 MCP Client) | ❌ |

---

## 10. 部署 & DevOps

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **Docker** | Dockerfile (multi-stage 可选) | 📋 规划中 |
| **docker-compose** | 4 service (agent + worker + beat + redis) | 📋 规划中 |
| **CI/CD** | 不做 MVP | ❌ |
| **健康检查** | /health 端点 + Celery Beat 自动健康检查 | 📋 规划中 |
| **日志管理** | 全局滚动日志 (每天 rotate, keep 30 天) | 📋 规划中 |
| **数据备份** | 手动 SQLite + ChromaDB + 文件备份脚本 | 📋 规划中 |

---

## 11. 测试

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **单元测试** | pytest (P0: ToolRegistry, Memory, EventBus, Reporter) | 📋 规划中 |
| **集成测试** | E2E 搜索→综述 全流程 | 📋 规划中 |
| **API 测试** | FastAPI TestClient | 📋 规划中 |
| **覆盖率** | 目标核心逻辑 70%+ | 📋 规划中 |

---

## 12. 其他亮点技能

| JD 关键词 | 项目实现 | 完成度 |
|-----------|----------|--------|
| **爬虫/数据采集** | 6 个学术 Provider + Playwright (CNKI) | ✅ 已完成 |
| **PDF 处理** | pymupdf4llm (PDF → 结构化 Markdown) | ✅ 已完成 |
| **引用管理** | BibTeX 导出 + citation_chase (1-hop) | ✅ 已完成 |
| **期刊等级** | CCF + SCI → 统一 A+/A/B/C 等级 | ✅ 已完成 |
| **iOS 推送** | APNs (离线通知) | 🔶 骨架就位 (device_tokens + register API + APNsPusher 占位；aioapns 真集成后补) |
| **跨平台** | Ubuntu Server + iOS Client + Docker | 📋 规划中 |
| **开源实践** | 完整文档 (产品+架构+协议+部署) | 🔧 编写中 |

---

## 13. 统计

| 分类 | 总计 | ✅ | 🔧 | 📋 | ❌ |
|------|------|-----|------|------|-----|
| AI Agent / LLM 应用 | 6 | 0 | 0 | 6 | 0 |
| LangChain/LangGraph | 6 | 0 | 0 | 4 | 2 |
| RAG / 向量检索 | 8 | 4 | 2 | 2 | 0 |
| 后端工程 | 7 | 1 | 0 | 6 | 0 |
| 数据库 & 存储 | 4 | 2 | 0 | 1 | 1 |
| 异步任务 & 消息队列 | 4 | 0 | 0 | 4 | 0 |
| LLM 集成 | 8 | 6 | 1 | 1 | 0 |
| Prompt Engineering | 6 | 1 | 0 | 2 | 3 |
| MCP 协议 | 4 | 2 | 0 | 1 | 1 |
| 部署 & DevOps | 6 | 0 | 0 | 5 | 1 |
| 测试 | 4 | 0 | 0 | 4 | 0 |
| 其他亮点 | 7 | 4 | 0 | 2 | 1 |
| **总计** | **70** | **20** | **3** | **38** | **9** |

- **已有能力**: 23/70 (33%)
- **MVP 后将达**: 61/70 (87%)
- **不覆盖**: 9/70 (13%) — 主要是 CI/CD、LangFuse、Prompt 版本管理

---

> 版本: v1.0 | 随着实施进度更新
