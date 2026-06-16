# 完整科研助理 Agent — 产品与架构设计

> **代号：Paper Agent v3** — 从 CLI 工具集到智能科研助理的完整进化

---

## Context

当前 `paper_agant` 已具备论文搜索、下载、转换、索引、评估、排名、综述生成等基础能力（13 个 CLI 工具），但缺少一个**完整的智能 Agent 层**来协调这些工具，实现真正的"科研助理"体验。

本方案基于 88 个结构化问题的讨论结果，覆盖需求→产品→架构→技术→难点→开发计划 6 大维度。

**第一性原则：架构优雅优先** — 代码质量和架构设计不可妥协，追求长期可维护性。

---

## 1. 需求定义

### 1.1 目标用户

| 优先级 | 用户群 | 特征 |
|--------|--------|------|
| P0 | 研究生/博士生 | 大量读论文、写综述、追踪前沿 |
| P0 | 工业界工程师 | 快速了解技术领域，应用于产品开发 |

### 1.2 核心场景（4个，全P0）

| 场景 | 描述 | 预期产出 |
|------|------|----------|
| 文献调研与筛选 | 输入主题 → 自动搜索/去重/筛选/总结 | 结构化论文列表 + 相关性评估 |
| 文献综述生成 | 给定主题 → 生成结构化综述报告 | 投稿级 Markdown/PDF 综述 |
| 每日前沿追踪 | 订阅研究方向 → 自动推送最新论文 | 每日/每周论文推送 + 摘要 |
| 论文精读与提炼 | 选定论文 → 提炼方法/贡献/局限 | 批判性分析报告 + 结构化知识卡片 |

### 1.3 关键需求参数

| 维度 | 决策 |
|------|------|
| 研究深度 | 全面系统综述（150+ 篇论文） |
| 语言 | 纯英文论文（后续扩展中文） |
| 质量标准 | 投稿级（期刊投稿标准，引用准确无误） |
| 隐私 | 全云端部署 |
| 交互方式 | 对话式 Chat |
| 输出形式 | 结构化综述 + 交互式知识库 + BibTeX + 论文摘要卡片 |

---

## 2. 产品设计

### 2.1 产品形态

```
┌─────────────────────────────────────────────────┐
│                  iOS 客户端                      │
│  对话界面 │ 论文管理 │ 知识库浏览 │ 任务面板      │
├─────────────────────────────────────────────────┤
│          REST API + WebSocket                   │
├─────────────────────────────────────────────────┤
│           Python Agent Engine (核心)             │
│  AgentRunLoop │ PlanGraph │ ToolRegistry │ Memory │
├─────────────────────────────────────────────────┤
│           CLI Tools (13个)                       │
│  search │ download │ convert │ index │ ...       │
├─────────────────────────────────────────────────┤
│     SQLite + ChromaDB + Redis(AOF) + 文件系统    │
└─────────────────────────────────────────────────┘
```

- **后端核心**：Python 单体服务，FastAPI + WebSocket
- **客户端**：iOS（主客户端），Vue WebUI（远期规划）
- **交互协议**：WebSocket（全双工实时通信）+ REST（数据查询）
- **消息格式**：自定义信封（role/type/subType/agentId/sessionId/seq/priority/timestamp/payload），7 大类 × 22 种子类
- **WS 协议**：详见 `docs/development/websocket-protocol.md`

### 2.2 核心差异化竞争力

1. **提示词优化引擎** — 多阶段 Pipeline：需求解析→歧义识别→澄清提问→方案生成
2. **Agentic Loop** — 自适应粒度、LLM+规则混合驱动、步数上限控制
3. **一键自动搜集入库** — 用户一个提示词 → 搜索→下载→转换→索引→入库 全自动
4. **知识库沉淀** — 跨项目知识积累，越用越聪明，支持 RAG 问答、知识发现、综述自动更新

### 2.3 用户完整流程

```
用户输入研究需求
    │
    ▼
┌─ 阶段1: 对话澄清 ─────────────────────────┐
│  Agent 分析需求 → 识别歧义/缺失信息        │
│  → 生成澄清问题 → 用户回答                  │
│  → (可多轮，直到需求清晰)                   │
└──────────────────────────────────────────┘
    │
    ▼
┌─ 阶段2: Plan 生成 ────────────────────────┐
│  生成结构化方案:                            │
│  - JSON (程序处理) + Markdown (用户阅读)    │
│  - 包含: 子任务分解、工具选择、搜索策略     │
│  - 验收标准、预期产出、风险提示             │
└──────────────────────────────────────────┘
    │
    ▼
┌─ 阶段3: 用户确认 ─────────────────────────┐
│  用户审核 Plan → 修改/确认/拒绝             │
│  - 可调整参数、增删子任务、修改优先级       │
│  - 确认后生成 Todolist                     │
└──────────────────────────────────────────┘
    │
    ▼
┌─ 阶段4: 逐步执行 (Agentic Loop) ──────────┐
│  逐个 step 执行:                            │
│  执行 → 指标检查 → LLM 质量评估 → 验收      │
│  - 全透明展示每步过程                       │
│  - 用户可随时暂停+调整                      │
│  - 异常自动处理 (重试/降级/求助用户)        │
└──────────────────────────────────────────┘
    │
    ▼
┌─ 阶段5: 审查汇总 ─────────────────────────┐
│  汇总所有步骤结果 → 生成最终报告             │
│  → 沉淀到知识库 → 用户审查 → 完成           │
└──────────────────────────────────────────┘
```

### 2.4 用户介入机制

- **随时暂停+调整**：任何步骤可暂停，修改参数/方向后继续或重来
- **全透明进度**：展示每步详细过程（搜索了什么、找到了什么、为什么这么决定）
- **阶段检查点**：关键阶段完成后暂停确认

### 2.5 长期关系

- **研究方向订阅**：Cron 定时搜索关键词 → 新论文推送
- **个人研究画像**：研究方向标签 + 偏好设置 + 行为历史
- **进度追踪**：追踪用户研究进度，提醒未完成任务

---

## 3. 架构设计

### 3.1 系统拓扑

```
                      ┌─────────────┐
                      │  iOS 客户端  │
                      │  (主客户端)  │
                      │  Vue WebUI  │ (远期规划)
                      └──────┬──────┘
                             │ REST + WS
                      ┌──────┴──────┐
                      │  FastAPI    │
                      │  - REST API │
                      │  - WS Chat  │
                      └──────┬──────┘
                             │
              ┌──────────────┴──────────────┐
              │       Agent Engine          │
              │  ┌──────────────────────┐   │
              │  │ Prompt Optimizer     │   │
              │  │ (3-stage Pipeline)   │   │
              │  └──────────────────────┘   │
              │  ┌──────────────────────┐   │
              │  │ Agentic Loop         │   │
              │  │ (Plan-then-Execute)  │   │
              │  └──────────────────────┘   │
              │  ┌──────────────────────┐   │
              │  │ Tool Registry        │   │
              │  │ (unified tool defs)  │   │
              │  └──────────────────────┘   │
              │  ┌──────────────────────┐   │
              │  │ Memory System        │   │
              │  │ (4-layer memory)     │   │
              │  └──────────────────────┘   │
              └──────────────┬──────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
   ┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
   │  CLI Tools  │   │  Providers  │
   │  (13 CLIs)  │   │  (6 sources)│
   └─────────────┘   └─────────────┘   └─────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
   ┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
   │   SQLite    │   │  ChromaDB   │   │  File Store │
   │  (agent.db) │   │ (2 colls)   │   │  (PDFs/MDs) │
   └─────────────┘   └─────────────┘   └─────────────┘
```

### 3.2 Agent 引擎核心设计

#### 3.2.1 决策模型：Plan-then-Execute

```
Plan Phase (用户参与)
  ┌─────────────────────────────────────┐
  │ 1. 需求解析 (LLM)                    │
  │ 2. 歧义识别 → 生成澄清问题 (LLM)     │
  │ 3. 用户回答 (多轮可选)               │
  │ 4. 方案生成: JSON + Markdown (LLM)   │
  │ 5. 用户确认/修改                     │
  │ 6. Todolist 生成                     │
  └─────────────────────────────────────┘
              │
              ▼
Execute Phase (逐步推进)
  ┌─────────────────────────────────────┐
  │ for each step in todolist:          │
  │   1. 执行工具调用                    │
  │   2. 指标检查 (自动化)               │
  │   3. LLM 质量评估                    │
  │   4. 验收判定                        │
  │   if 不达标: 自动调整策略重试 (≤2次) │
  │   if 仍不达标: 标记+汇报用户         │
  │   用户可随时暂停/调整                │
  └─────────────────────────────────────┘
```

#### 3.2.2 记忆系统（4层）

| 层级 | 存储 | 生命周期 | 内容 |
|------|------|----------|------|
| 短期记忆 | 进程内存 | 单次会话 | 当前对话的多轮上下文 + 工具调用历史 |
| 中期记忆 | SQLite | 单次任务 | 当前任务的进度、中间结果、用户决策 |
| 长期记忆 | ChromaDB + SQLite | 永久 | 跨项目知识积累：论文库、Wiki、用户画像 |
| 元记忆 | SQLite | 永久 | 策略有效性记录、用户偏好学习 |

#### 3.2.3 工具注册中心 (Tool Registry)

```python
# 统一工具定义
@dataclass
class ToolDef:
    name: str                    # e.g. "search_papers"
    description: str             # LLM 可读的描述
    parameters: dict             # JSON Schema
    handler: Callable            # 实际执行函数
    category: str                # search / download / index / analyze / export
    cost_estimate: int           # 预估 token 消耗
    is_idempotent: bool          # 是否幂等（用于重试）
```

所有工具（主 Agent 35 个 + 子 Agent 19 个，去重 ~32 个）注册到统一 ToolRegistry，Agent 按需加载。

#### 3.2.4 Loop 状态机

```
                    ┌──────────┐
                    │  START   │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
              ┌─────│ EXECUTE  │◄──────────────┐
              │     └────┬─────┘                │
              │          │                      │
              │     ┌────▼─────┐    自动重试    │
              │     │ VERIFY   │ (≤2次, 策略调整)│
              │     └────┬─────┘                │
              │          │                      │
              │     ┌────▼─────┐                │
              │  ┌──│  PASS?   │───Yes──────────┘
              │  │  └──────────┘
              │  │     │ No (不可恢复)
              │  │     ▼
              │  │  ┌──────────┐
              │  │  │ HELP USER│ (汇报+建议+等用户)
              │  │  └──────────┘
              │  │
              │  ▼
              │  ┌──────────┐
              └──│ NEXT STEP│ (todolist 中下一个)
                 └────┬─────┘
                      │
                 ┌────▼─────┐
                 │  COMPLETE │ (所有 steps done)
                 └──────────┘
```

**收敛保证机制：**
- 步数上限：每个 task 硬性限制最大操作步数
- 自动重试上限：同一 step 最多自动重试 2 次
- 若仍不达标：标记失败 + 汇报用户 + 继续下一个 step
- 全局收敛：所有 steps done/failed → 汇总报告

---

## 4. 技术方案

### 4.1 技术栈

| 层级 | 技术选型 | 理由 |
|------|----------|------|
| Web 框架 | FastAPI | 原生 REST+WS，异步，生态成熟 |
| 任务队列 | Celery + Redis | 长时间任务异步执行，重试/监控 |
| 数据库 | SQLite | 零配置，单用户够用，预留 user_id 给多用户 |
| 向量存储 | ChromaDB | 现有方案，继续使用 |
| LLM 协议 | Anthropic Messages API 兼容 | 火山引擎默认，可切换供应商 |
| 消息格式 | JSON (Anthropic tool-use 格式) | Function Calling 模式 |
| 部署 | Docker 单容器 + pip install | 二选一 |
| 前端 | iOS（主客户端） | Vue WebUI 远期规划 |

### 4.2 LLM 客户端升级

当前问题：
- 仅支持火山引擎，硬编码模型名
- 无流式输出
- 无重试机制
- JSON 解析脆弱

升级目标：

```python
class LLMClientV2:
    """Anthropic-compatible multi-provider LLM client"""
    
    def __init__(self, provider: str = "volcano"):
        self.providers = {
            "volcano": {"base_url": "...", "api_key": "..."},
            "openai": {"base_url": "...", "api_key": "..."},
            # 可扩展
        }
    
    async def chat(self, messages, tools=None, stream=True):
        """支持 tool use + streaming"""
        
    async def chat_json(self, messages, schema=None):
        """结构化输出，schema 约束"""
        
    async def switch_provider(self, provider: str):
        """运行时切换供应商"""
```

### 4.3 知识库技术方案

#### RAG 检索：ChromaDB + Reranker + LLM

```
用户问题
    │
    ▼
┌─ 第一阶段: 向量检索 ─────────────┐
│  ChromaDB 双 Collection 检索:     │
│  - papers_abstract (快速筛选)     │
│  - papers_fulltext (深度匹配)     │
│  召回 top-20                      │
└──────────────────────────────────┘
    │
    ▼
┌─ 第二阶段: 重排序 ───────────────┐
│  Cross-encoder Reranker          │
│  (如 bge-reranker-v2-m3)         │
│  20 → top-5                       │
└──────────────────────────────────┘
    │
    ▼
┌─ 第三阶段: LLM 生成 ─────────────┐
│  将 top-5 论文作为 context        │
│  → LLM 生成答案 + 引用标注        │
└──────────────────────────────────┘
```

#### 论文知识提取（引用网络增强）

```
论文入库
    │
    ├─ 1. LLM 结构化提取
    │    - 核心贡献 (Problem/Contribution)
    │    - 方法/技术栈 (Methods)
    │    - 实验数据集 (Datasets)
    │    - 关键指标 (Metrics/Results)
    │    - 局限性与未来工作 (Limitations)
    │    - 代码与复现 (Code/Reproducibility)
    │
    ├─ 2. 引用网络增强
    │    - citation_chase: 获取引用关系
    │    - 高引论文 → 深度提取
    │    - 构建局部引用图谱
    │
    └─ 3. 存储
         - 结构化知识 → SQLite (knowledge 表)
         - 向量嵌入 → ChromaDB
         - 引用关系 → SQLite (citations 表)
```

### 4.4 Prompt 优化引擎（3阶段 Pipeline MVP）

```
用户输入: "我想研究自动驾驶的安全性"
    │
    ▼
Stage 1: 需求解析 (Parse)
    │  LLM 分析:
    │  - 领域: Autonomous Driving
    │  - 子方向: Safety, Security, Verification
    │  - 歧义: "安全性"=safety or security?
    │  - 缺失: 时间范围? 论文类型? 深度?
    │  - 实体提取: "自动驾驶" → autonomous driving
    ▼
Stage 2: 澄清提问 (Clarify)
    │  生成澄清问题:
    │  1. 你关注的是功能安全(safety)还是网络安全(security)?
    │  2. 关注最近几年(2022-2026)的论文?
    │  3. 需要系统综述级别(100+篇)还是快速概览(20篇)?
    │  → 用户回答
    ▼
Stage 3: 方案生成 (Generate)
    │  生成结构化方案:
    │  - Goal: Systematic review of autonomous driving safety
    │  - Sub-tasks: [Safety standards, ML robustness, ...]
    │  - Search strategy: keywords × sources × year range
    │  - Expected output: Survey paper + Knowledge base
    │  - Acceptance criteria per sub-task
    ▼
输出: Plan (JSON + Markdown)
```

### 4.5 引用幻觉防控

**严格校验策略：**

```
LLM 生成声明 (含引用)
    │
    ▼
每条引用强制校验:
├─ 1. 引用格式检查: [Author, Year] 或 [N] 格式
├─ 2. 数据库匹配: 在 SQLite 中查找匹配论文
├─ 3. 事实校验: 声明中的方法/数据集/指标与原文匹配
└─ 4. 不一致处理:
     - 轻微不匹配 → 自动修正
     - 严重不匹配 → 标记为待审核
     - 无法匹配 → 删除该引用
```

### 4.6 崩溃恢复（检查点 + 智能恢复）

```
任务执行中...
    │
    ├─ 每步完成后 → 持久化检查点到 SQLite
    │   - task_id, step_index, state, results
    │
    ├─ 崩溃后重启
    │   ├─ 加载最近检查点
    │   ├─ LLM 分析崩溃原因
    │   ├─ 自动调整策略 (如降低并发、换关键词)
    │   └─ 从检查点恢复执行
    │
    └─ 全局幂等保证
        - 每步操作带幂等 key
        - 重放时不重复执行已完成的步骤
```

### 4.7 API 设计概览

```
REST API:
  POST   /api/tasks                    # 创建新任务
  GET    /api/tasks/{id}               # 获取任务状态
  DELETE /api/tasks/{id}               # 取消任务
  GET    /api/papers                   # 搜索/列出论文
  GET    /api/papers/{id}              # 论文详情
  POST   /api/papers/upload            # 上传PDF
  GET    /api/knowledge-base/search    # 知识库检索
  GET    /api/projects                 # 项目列表
  GET    /api/agents                   # Agent 列表
  GET    /api/agents/{id}/sessions     # Session 列表
  POST   /api/agents/{id}/sessions     # 创建新 session
  GET    /api/subscriptions            # 订阅管理

WebSocket:
  WS  /ws/chat/{agent_id}/{session_id}  # 单一全双工通道 — 对话/进度/工具/审批

(不再使用 SSE — 所有实时推送通过 WebSocket phase 消息)
```

### 4.8 WebSocket 消息协议

> 详见 `docs/development/websocket-protocol.md`（v6.0, 2026-06-16）。此处仅列概要。

**通用信封**（所有消息必含）:
```json
{
  "role": "user | assistant",
  "type": "<7 大类>",
  "subType": "<22 种子类>",
  "agentId": "agent-001",
  "sessionId": "main",
  "seq": 0,
  "priority": 0 | 1 | 2,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": { ... }
}
```

**7 大类消息**: `heartbeat` / `phase` / `thinking` / `message` / `tool` / `review` / `error`

**握手流程**:
```
① iOS → Server:  WS 连接 /ws/chat/{agent_id}/{session_id}
② iOS → Server:  message(chat, seq=1) — 首条消息必须夹带握手信息
③ Server:        检查 session → 不存在则自动创建
④ Server → iOS:  phase(connected) — 握手回复
⑤ 握手完成，进入正常交互
```

---

## 5. 难点与应对策略

### 5.1 难点矩阵

| 难点 | 严重度 | 应对策略 |
|------|--------|----------|
| LLM 幻觉导致引用错误 | 🔴 致命 | 严格校验：每条引用强制与 DB/原文校验，不一致则拒绝/标记 |
| 优化后方案质量 | 🔴 致命 | 多阶段 Pipeline + 人工确认 + 方案审查（未来加验证阶段） |
| 质量评估客观性 | 🔴 致命 | 指标+LLM 混合评估，关键步骤人工确认 |
| 知识提取质量 | 🔴 致命 | 引用网络增强 + LLM 结构化提取 + 持续优化 prompt |
| LLM 与工具可靠协作 | 🟠 严重 | 统一 Tool Registry + 超时/重试 + 错误分类处理 |
| 收敛保证 | 🟠 严重 | 步数上限 + 自动重试上限 + 降级策略 |
| 长期任务稳定性 | 🟠 严重 | 检查点+智能恢复+幂等重放 |
| 引用追踪完整性 | 🟡 中等 | citation_chase 补全 + Semantic Scholar API + 多源引用 |
| CNKI 脆弱性 | 🟡 中等 | 增加 OpenAlex（Semantic Scholar 降级方案）/ DBLP 等新来源降低单点依赖 |
| 跨领域知识关联 | 🟡 中等 | 后续版本通过知识图谱实现，MVP 用向量相似度 |

### 5.2 错误处理策略表

| 场景 | 处理方式 |
|------|----------|
| LLM API 超时/限流 | 暂停 → 通知用户 → 等恢复 |
| 搜索结果为零 | 自动重试1-2次 → 汇报用户+建议 |
| 数据不一致 | 定期健康检查 → 预警 → 用户选择修复 |
| PDF 下载失败 | 多渠道尝试 → 标注无全文 → 仅用摘要 |
| 模糊需求 | 教育式引导 → 给出具体化建议和模板 |
| 付费墙论文 | 多 OA 渠道尝试 → 用户辅助上传 |

---

## 6. 开发计划

### 6.1 MVP 范围

| 模块 | 优先级 | 说明 |
|------|--------|------|
| Agent 引擎 (Agentic Loop) | P0 | Plan-then-Execute 决策模型 + Loop 状态机 |
| 提示词优化引擎 | P0 | 3阶段 Pipeline (Parse → Clarify → Generate) |
| 一键自动搜集入库 | P0 | 关键词 → 搜索 → 下载 → 转换 → 索引 全自动 |
| 知识库 RAG 问答 | P0 | ChromaDB + Reranker + LLM |
| 引用追踪增强 | P1 | citation_chase 补全，真正的引用网络 |
| 搜索覆盖增强 | P1 | 新增 OpenAlex（Semantic Scholar 降级方案）/ DBLP 支持 |
| LLM 客户端升级 | P1 | Anthropic 兼容多供应商 + 流式 + 重试 |
| Tool Use 补全 | P2 | 确保 13 个 CLI 工具均已适配 ToolRegistry |
| FastAPI + WebSocket | P2 | REST+WS API 层 |
| Vue WebUI | P3 | 后续版本，MVP 阶段 CLI + MCP |

### 6.2 开发阶段（2-3个月集中开发）

**Phase 1: 基础设施（2-3周）**
- LLM Client V2（多供应商 + 流式 + 重试）
- Tool Registry 统一注册中心
- Memory System（4层记忆的基础实现）
- 数据库 schema 扩展（增加 knowledge/citations/checkpoints 表）
- 补充 ToolRegistry 工具

**Phase 2: Agent 核心（3-4周）**
- Agentic Loop 引擎（Plan-then-Execute + 状态机）
- Prompt 优化引擎（3阶段 Pipeline）
- 引用幻觉防控（严格校验）
- 崩溃恢复（检查点机制）
- 成本控制（步数上限）

**Phase 3: 自动流水线（2-3周）**
- 一键搜集入库（端到端自动化）
- 引用追踪补全
- 搜索覆盖增强（新来源）
- 健康检查系统

**Phase 4: 知识库（2-3周）**
- 知识提取（结构化信息 + 引用网络增强）
- RAG 问答（ChromaDB + Reranker + LLM）
- 知识发现（空白/矛盾/趋势分析）
- 综述自动更新

**Phase 5: API + 交互（2-3周）**
- FastAPI REST + WebSocket
- JSON + Markdown 双格式 Plan 输出
- 进度全透明推送
- 研究方向订阅（Cron 轮询）

**Phase 6: 测试 + 文档 + 发布（1-2周）**
- 核心逻辑单元测试 + E2E 测试
- CLI + API 文档
- Docker 镜像
- 示例和教程

### 6.3 验收标准

| 阶段 | 验收方式 | 标准 |
|------|----------|------|
| 每个 Step | 指标+LLM 评估 | 量化指标达标 + LLM 质量评分 |
| 每个 Phase | 自动化测试 + 手动验收 | 核心逻辑有测试覆盖，端到端场景可运行 |
| MVP 整体 | 真实场景验证 | 3个典型研究场景端到端可用 |

### 6.4 代码组织

保持单体包结构 `src/paper_search/`:

```
src/paper_search/
├── agent/
│   ├── db.py              # SQLite (扩展)
│   ├── chroma_store.py    # ChromaDB (扩展)
│   ├── llm_client.py      # → llm_client_v2.py
│   ├── agent_loop.py      # [NEW] Agentic Loop 引擎
│   ├── prompt_optimizer.py # [NEW] 提示词优化 Pipeline
│   ├── tool_registry.py   # [NEW] 统一工具注册中心
│   ├── memory.py          # [NEW] 4层记忆系统
│   ├── knowledge.py       # [NEW] 知识提取与管理
│   ├── verifier.py        # [NEW] 引用校验与质量评估
│   └── ...
├── api/                   # [NEW] FastAPI 层
│   ├── app.py
│   ├── routes/
│   └── ws.py
├── cli/                   # (现有, 补全)
├── mcp/server.py          # (扩展)
├── providers/             # (新增来源)
└── ...
```

### 6.5 依赖策略

**最小依赖原则：**
- 核心功能：`pip install paper-search` 即可用
- 高级功能（Reranker / 新来源 / Web UI）：可选依赖
- Docker 一键部署包含所有依赖

### 6.6 长期愿景

> **个人学术大脑** — 外挂的第二大脑，记住你读过的一切，自动关联和发现新知识。

2年后的形态：
- 持续学习用户偏好和行为，越用越精准
- 跨项目知识自动关联，发现隐藏的研究线索
- 从 idea 到投稿的全流程 AI 协作
- 支持团队协作和知识共享

---

## 附录：竞争分析摘要

当前市场 AI 科研助手对比：

| 产品 | 核心模式 | 优势 | 劣势 |
|------|----------|------|------|
| Elicit | 结构化查询+自动化筛选 | 系统综述流程成熟 | 封闭生态，无知识库沉淀 |
| Consensus | GPT+论文数据库 | 消费者友好 | 深度有限 |
| Scite | 引用上下文分析 | 引用分类(Smart Citations) | 搜索能力弱 |
| Research Rabbit | 引用图谱可视化 | 发现相关论文强 | 无 AI 分析 |
| Perplexity Deep Research | 多步搜索+综合 | 通用性高 | 学术深度不足 |
| PaperQA | RAG on papers | 开源可定制 | 基础设施弱 |
| SciSpace | 论文解释+聊天 | 论文精读好 | 搜索能力一般 |

**市场缺口（本项目定位）：**
- ❌ 缺少端到端自动化（搜索→下载→精读→综述→知识库）
- ❌ 缺少跨项目知识积累（都是每次从零开始）
- ❌ 缺少 Agentic Loop 质量控制（都是一次性生成）
- ❌ 缺少全透明可控的执行过程
- ✅ **本项目 = 完整闭环 + 知识沉淀 + Agentic 质量控制 + 全透明**

---

> 文档版本: v1.0 | 生成日期: 2026-06-13 | 基于 88 个结构化问题讨论
