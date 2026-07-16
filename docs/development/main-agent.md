# [DEPRECATED] MainAgent — v3.1 架构

> v3.1 | 2026-07-12 | Fast Triage + ReAct Execute + thinking 策略
>
> LLM: DeepSeek v4 Pro (Anthropic-compatible API) · 详细文档: [api-reference.md](api-reference.md) · [websocket-protocol.md](websocket-protocol.md)

---

## §1 核心理念

```
用户消息 → Fast Triage（一次调用）→ chat/ops/research 三条路径
                         research → Intent → Plan(todo list) → Execute(ReAct) → Evaluate
                         ops      → 直接 ReAct（跳过意图分类）
                         chat     → 直接回复 → END
```

**三个关键设计决策**：

1. **结构化节点禁用 thinking** — 分支判断、打分、校验走 `flash + no thinking + tool_choice`，极致快速和确定性
2. **生成类节点保持 thinking** — 规划、执行、回复走 `pro + thinking`，深度推理
3. **子 Agent = 特殊 Tool** — 以 `agent_` 前缀区分，LLM 不需要知道内部实现，只管 input/output schema

---

## §2 节点流程

```
                        ┌─────────────────────────────────┐
                        │ Fast Triage                      │
                        │ model: deepseek-v4-flash         │
                        │ thinking: disabled               │
                        │ tool_choice: triage_output       │
                        │ → {chat, ops, research} 独立打分  │
                        └────────────┬────────────────────┘
                                     │
                            代码路由（不靠 LLM）:
                            research > 0.4 → research
                            ops > 0.6      → ops
                            else           → chat
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
         research                  ops                    chat
              │                      │                      │
              ▼                      ▼                      ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │ Intent Classify   │   │ Execute (ReAct)   │   │ inline_reply     │
   │ flash, no think   │   │ pro, thinking     │   │ pro, thinking    │
   │ tool_choice       │   │ 自由 tool_use      │   │ → END            │
   │ → scenarios[]     │   │ → END              │   └──────────────────┘
   └────────┬──────────┘   └──────────────────┘
            │
            ▼
   ┌──────────────────┐
   │ Scenario Plan     │
   │ pro, no thinking  │  ← 决策节点，不容忍自由工具
   │ tool_choice:      │
   │   plan_output     │
   │ → {todos,         │
   │    needs_clarify, │
   │    danger_level}   │
   └────────┬──────────┘
            │
    needs_clarify=true?
     YES ──→ ask(questions) → 用户回 → 重新 Plan
     NO
            │
            ▼
   ┌──────────────────┐
   │ Execute (ReAct)   │
   │ pro, thinking     │
   │ 自由 tool_use     │
   │                   │
   │ for each todo:    │
   │   并行 tool_calls │
   │   串行 ReAct loop │
   │   ↓               │
   │   todo checkpoint │  ← flash, no thinking, tool_choice
   │   {done, retry}   │
   │                   │
   │ 所有 todo 结束    │
   └────────┬──────────┘
            │
            ▼
   ┌──────────────────┐
   │ Evaluate           │
   │ flash, no thinking │
   │ tool_choice        │
   │ → {satisfied,      │
   │    next_action}    │
   └────────┬──────────┘
            │
   ┌────────┼────────┬────────┬────────┐
   ▼        ▼        ▼        ▼        ▼
  done   retry    ask_user  replan   fail
   │        │        │        │        │
   ▼        ▼        ▼        ▼        ▼
  END   Execute  ask卡片   Plan     END
        (补tool)  等回复  (带hint)
```

**循环上限**：总轮数 ≤ 8，ask_user 累计 ≤ 2，replan 不限制（靠总轮数兜底）。

---

## §3 模型策略表

| 节点 | 模型 | thinking | tool_choice | 理由 |
|------|:---:|:---:|:---:|------|
| Fast Triage | flash | 禁用 | 强制 | 分流要快（<500ms），确定性枚举 |
| Intent Classify | flash | 禁用 | 强制 | 分类要确定性，不需要推理 |
| Scenario Plan | pro | 禁用 | 强制 | 决策节点，不容忍自由工具调用 |
| Execute (ReAct) | pro | 开启 | 无 | 自由工具链，需要深度推理 |
| Todo Checkpoint | flash | 禁用 | 强制 | 纯判断，低成本 |
| Evaluate | flash | 禁用 | 强制 | 5 出口决策，确定性 |
| inline_reply | pro | 开启 | 无 | 自然对话需要思考质量 |

**thinking 策略**：
- structured output 节点禁用 thinking：`tool_choice` 强制调用指定 tool 时，DeepSeek v4 Pro 要求 `thinking: {type: "disabled"}`
- 生成类节点保持 thinking：`thinking_delta` 事件被跳过（等待 `text_delta`），DEBUG_PROTOCOL=1 时推送到 WS 供调试

---

## §4 节点详解

### §4.1 Fast Triage — 一次调用完成分流

**职责**：对用户消息在三个维度独立打分，同时生成简短回复供 chat 路径使用。

**输出**：
```json
{
  "scores": {
    "chat": 0.95,
    "ops": 0.02,
    "research": 0.15
  },
  "brief_reply": "你好！有什么可以帮你的？",
  "intent_hint": null
}
```

**路由规则（代码执行，不靠 LLM）**：
```
research > 0.4  → research 路径
ops > 0.6       → ops 路径（直接 ReAct，不进入意图分类）
else            → chat 路径（brief_reply 直接推用户）
```

research 阈值设为 0.4（较低）以确保科研意图不遗漏。ops 阈值 0.6（较高）以避免误触发系统操作。

**兜底**：如果所有分数都很低（极端情况），chat 路径是自然兜底——LLM 会反问用户想要什么。

### §4.2 Intent Classify — 场景匹配

**职责**：将 research 消息映射到 1~N 个业务场景。

**输入**：
- 用户最新消息
- 从 Store 加载的 user_preferences + profile
- Checkpointer 提供的 state.messages（已经过 trim）

**输出**：
```json
{
  "scenarios": [
    {"id": "S1", "confidence": 0.85, "label": "文献调研"},
    {"id": "S12", "confidence": 0.72, "label": "学术翻译"}
  ],
  "overall_confidence": 0.85
}
```

**17 个业务场景 ID**（S1~S17，见 [main_agent_prompts.py](../../src/paper_search/agent/main_agent_prompts.py)）。

### §4.3 Scenario Plan — 强制结构化，不容忍自由工具

**职责**：根据命中的场景，生成 todo list 形式的执行计划。

**为什么不容忍自由工具调用**：
- Plan 是决策节点，不是执行节点
- 前置调查（"先看看有多少论文"）是 Execute 的事，不是 Plan 的事
- 允许自由工具会导致 LLM 在工具循环中迷失，plan 永远出不来

**输出**（`tool_choice: plan_output`）：
```json
{
  "summary": "搜索 transformer 论文并生成综述",
  "danger_level": "medium",
  "permissions": ["search", "download"],
  "estimated_seconds": 600,
  "needs_clarify": false,
  "clarify_questions": [],
  "todos": [
    {
      "id": "todo-1",
      "label": "跨源搜索论文",
      "tool_calls": [
        {"name": "agent_search", "params": {"query": "...", "source": "arxiv", "max_results": 30}},
        {"name": "agent_search", "params": {"query": "...", "source": "semantic_scholar", "max_results": 30}}
      ],
      "parallel": true,
      "success_criterion": "合计 ≥ 30 篇去重论文"
    },
    {
      "id": "todo-2",
      "label": "论文入库 + 综述",
      "tool_calls": [
        {"name": "agent_ingest", "params": {"project_id": "$todo-1.project_id", "max_results": 30}}
      ],
      "parallel": false,
      "success_criterion": "论文已入库，生成 Markdown 综述"
    }
  ]
}
```

**如果需要澄清**：
```json
{
  "needs_clarify": true,
  "clarify_questions": [
    {"id": "q1", "question": "transformer 的哪个方向？", "kind": "choice",
     "options": ["理论分析", "高效注意力", "长上下文"], "required": true},
    {"id": "q2", "question": "年份范围？", "kind": "text", "default": "2022-2026"}
  ],
  "todos": []
}
```

**澄清→Plan 循环**：用户回答后重新调 Plan（带用户答案），直到 `needs_clarify=false`。

**Plan 节点容错**：如果 SSE 结束还没调用 `plan_output`，推送 error 给用户，human-in-loop 判断是否重试。

### §4.4 Execute — ReAct Loop

**职责**：按 todo list 执行，每个 todo 内 LLM 自由调用工具（ReAct 循环）。

**模型**：`deepseek-v4-pro`，thinking 开启，不设 tool_choice。

**关键行为**：
- **同一轮内多个 tool_use → 并行执行**（互不依赖时）
- **跨轮次 → 串行执行**（依赖上一轮 tool_result 时）
- **text 输出流式推给用户**（进度可见）
- **子 Agent 以 `agent_` 前缀区分**，返回进度通过 `tool/start → progress → result`

```
Todo-1 执行:
  Round 1:
    LLM → text: "开始搜索..."
         + tool_use: agent_search(arxiv)
         + tool_use: agent_search(semantic)    ← 并行
    Execute → 两个 agent 并行跑
  Round 2:
    LLM ← tool_result: arxiv 25篇, semantic 20篇
    LLM → text: "去重后 38 篇。进入筛选..."
    stop_reason: "end_turn"

  Todo Checkpoint:
    success_criterion: "≥ 30 篇去重论文"  ✅ 满足 → 进入 todo-2
```

**子 Agent 与普通 Tool 的区别**（服务端实现层，LLM 不可见）：

| | 普通 Tool | 子 Agent (`agent_*`) |
|------|:---:|:---:|
| 执行方式 | 函数调用 | 独立 LangGraph 图 |
| 上下文 | 共享主 Agent | 独立上下文窗口 |
| WS 通知 | 单条 tool/result | tool/start → progress → result |
| 可取消 | 否 | 是 |
| 内部 Plan | 无 | ✅ 预定义工作流 |
| 自主决策 | 无 | ✅ 流程内可调整参数 |

### §4.5 Todo Checkpoint

**职责**：每个 todo 执行完后，简短判断是否满足 `success_criterion`。

**模型**：`deepseek-v4-flash`，no thinking，tool_choice。

```json
{
  "todo_id": "todo-1",
  "completed": true,
  "result_summary": "38 篇论文，arXiv 25 + Semantic 20，去重后 38 篇",
  "needs_retry": false,
  "retry_hint": null
}
```

不满足时：
```json
{
  "completed": false,
  "needs_retry": true,
  "retry_hint": "只找到 12 篇，放宽年份限制或增加 PubMed 数据源"
}
```

### §4.6 Evaluate — 5 出口

**职责**：所有 todo 完成后，判断整体是否满足用户需求。

**模型**：`deepseek-v4-flash`，no thinking，tool_choice。

**需要完整上下文**：
- Plan 原始输出 + 每个 todo 的结果摘要（不需要每篇论文的完整内容）
- 关键指标、异常 tool output

**5 出口**：

| next_action | 场景 | 流转 |
|---|---|---|
| `done` | 满意 | END，推 final_message |
| `retry_tools` | 补几个 tool | Execute（带 needs_more_tools） |
| `ask_user` | 需要用户判断 | ask 卡片 → 等回复 |
| `replan` | 方向不对 | Plan（带 replan_hint） |
| `fail` | 彻底失败 | END，推 fail 消息 |

---

## §5 Tool 命名规范

| 前缀 | 含义 | 示例 |
|------|------|------|
| `agent_` | 子 Agent（独立 LangGraph 图） | `agent_search`, `agent_ingest`, `agent_survey`, `agent_translate` |
| 无前缀 | 主 Agent 本地 Tool | `search_papers`, `download_paper`, `exec_command`, `ask_user` |

**从 LLM 视角，两者都是 tool，没有区别。** 区别只在服务端实现。

---

## §6 调试模式

环境变量 `DEBUG_PROTOCOL=1` 时：

- LLM thinking 过程通过 `status{level:debug, stage:llm:thinking_delta}` 推送到 WS
- 所有 debug 消息 `priority=silent`，不持久化、不触发 APNs
- iOS 生产 build 不渲染

详见 [websocket-protocol.md §5.2](websocket-protocol.md)。

---

## §7 子 Agent 参数设计

以 `agent_ingest` 为例：

```json
{
  "name": "agent_ingest",
  "label": "论文搜索入库",
  "description": "搜索 → 评估 → 下载 → 转换 → 索引",
  "params": {
    "user_query": {"type": "string", "required": true},
    "sources": {"type": "string[]", "default": ["arxiv", "semantic_scholar"]},
    "year_from": {"type": "int", "default": 2022},
    "max_results": {"type": "int", "default": 30, "min": 5, "max": 200},
    "evaluate_papers": {"type": "bool", "default": true},
    "download_top_k": {"type": "int | null", "default": null}
  },
  "output": {
    "project_id": "string",
    "papers_found": "int",
    "papers_downloaded": "int",
    "top_papers": [{"title": "string", "doi": "string"}]
  },
  "estimated_duration": "2-5 minutes"
}
```

**设计原则**：`max_results` 有上限防误填，布尔开关让主 Agent 控制粒度，`output` schema 让 Plan 知道下一步能拿到什么。

---

> 版本: v3.1 | 2026-07-12 | LLM: DeepSeek v4 Pro · Flash
