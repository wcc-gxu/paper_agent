# MainAgent — 5 节点显式状态机

> v1.0 | 2026-06-21 | 替代 `agent-runloop.md` (v1 AgentRunLoop) 与 v2 AgentLoop

---

## 一、设计理念

MainAgent 是 Paper Agent v3 的**唯一主 Agent**实现。把"用户消息进来 → 该怎么处理"做成 5 个可观测的节点，每个节点的 LLM 输出受 Pydantic JSON Schema 强约束，所有分支都明确写在代码里。

**核心原则**

1. **结构化输出**：LLM 不允许"自由发挥"——每次决策都返回符合 schema 的 JSON
2. **批量调用**：执行阶段 LLM 一次性返回所有 tools[]，主程序按 depends_on 拓扑排序并发执行
3. **持久化的可恢复性**：每个状态变更写 `agent_events` 表（事件源），daemon 重启可 replay 重建
4. **消息全程持久化**：所有出站消息走 outbox，离线 APNs，上线同步

---

## 二、节点流程

```
WS 消息 → BRPOP agent:ws:{agent_id}
              ↓
    [1] intent_classify         ← LLM #1 (IntentClassifyResult)
              ↓
       intent_kind ∈
        {business, chat, meta, unsupported}
              ↓
        ┌─────┴──────┐
   business     chat/meta/unsupported
        ↓             ↓
[2A] scenario_plan  [2B] inline_reply  ← LLM #3 (流式 thinking + text)
   (LLM #2,            ↓
    ScenarioPlanResult)  END
        ↓
  needs_clarify?  → ask_user_question → (回答) → 再次 scenario_plan
        ↓
  needs_approval? → propose_plan (iOS 渲染 plan 卡片) → (用户批准)
        ↓
   [3] execute_plan
   (按 depends_on 拓扑排序，asyncio.gather 并发)
        ↓
   [4] evaluate_completion       ← LLM #4 (EvaluateCompletionResult)
        ↓
   satisfied?  → END (message/text)
       |       否
       →  execute_plan (新一批 tools)  ← 最多 3 次迭代
```

文件位置：[src/paper_search/agent/main_agent.py](../../src/paper_search/agent/main_agent.py)
prompts/schemas：[main_agent_prompts.py](../../src/paper_search/agent/main_agent_prompts.py)

---

## 三、5 个节点详解

### 3.1 `intent_classify` (LLM #1)

**职责**：把用户消息分到 4 类意图之一。

**输入**：用户最新消息 + MetaMemory 偏好快照 + ShortTerm 滑动窗口

**输出（Pydantic 强约束）**：

```python
class IntentClassifyResult(BaseModel):
    intent_kind: Literal["business", "chat", "meta", "unsupported"]
    scenario_id: Optional[Literal["S1", ..., "S17"]]  # business 时必填
    confidence: float                                  # 0~1
    reasoning: str                                     # ≤200字 中文
```

**分支**：

| intent_kind + confidence | 走向 |
|---|---|
| business + ≥0.5 | `scenario_plan(scenario_id)` |
| business + <0.5 | 降级到 chat (安全策略) |
| chat / meta / unsupported | `inline_reply` |

### 3.2 `scenario_plan` (LLM #2, business 分支)

**职责**：根据 17 业务场景之一生成结构化执行计划。

**输出**：

```python
class ScenarioPlanResult(BaseModel):
    scenario_id: str
    summary: str                              # 给用户看的方案摘要 (≤300字)
    needs_clarification: bool
    clarification_questions: list[ClarificationQuestion]
    needs_approval: bool
    permissions_required: list[Permission]    # ["search","download",...]
    estimated_time_seconds: int
    tools: list[ToolCallSpec]                 # ★ 一次性返回所有调用
```

`ToolCallSpec` 包含 `call_id / kind ∈ {sub_agent,tool,ios_tool,ask_user} / name / arguments / depends_on`。

**子流程**：

1. 如 `needs_clarification=true` → 发 `tool/ask_user_question`，得到答案后**重新调** scenario_plan（最多 3 次澄清）
2. 如 `needs_approval=true` → 发 `tool/propose_plan` → iOS 渲染 plan 卡片 → 等待用户回 `approved=true/false`
3. 进入 execute_plan

### 3.3 `inline_reply` (LLM #3, non-business 分支)

**职责**：闲聊/元请求/能力外请求 直接生成自然语言回复。

**行为**：
- 调用 `llm_client_v2.chat_stream` → 流式推送 `message/thinking` (priority_kind=silent)
- 完整文本推 `message/text` (priority_kind=high → 触发 APNs)
- 仅允许调用轻量记忆工具 (`search_memory / get_user_preference / extract_to_long_term`)

**不进入** Celery 子 Agent / plan 卡片 / await_approval。

### 3.4 `execute_plan` (节点 3)

**职责**：执行 scenario_plan 返回的 `tools[]`。

**算法**：拓扑排序分批（依据 `depends_on`），每批用 `asyncio.gather(return_exceptions=True)` 并行：

```
remaining = {t.call_id: t for t in tools}
results = {}
while remaining:
    ready = [t for t in remaining.values()
             if all(dep in results for dep in t.depends_on)]
    if not ready:                       # 检测循环依赖
        # 全部标失败退出
    batch_results = await asyncio.gather(
        *(dispatch_one(t) for t in ready),
        return_exceptions=True,
    )
    results.update(...)
    remaining -= ready
```

**4 类调度**：

| kind | dispatch 路径 | 完成判定 |
|---|---|---|
| `sub_agent` | `celery_tasks.sub_agent_task.delay()` + 订阅 `agent:reports:{task_id}` | **只识别 type=lifecycle && lifecycle ∈ {agent_done, agent_failed}** |
| `tool` | `ToolRegistry.get(name)` → 同步函数走 `run_in_executor` | 函数返回 / 抛异常 |
| `ios_tool` | 推 `tool/ios_request` 给 iOS | 等 `tool/ios_result(tool_call_id=call_id)` |
| `ask_user` | 推 `tool/ask_user_question` | 等 `tool/ask_user_question(role=user)` |

每个调用各有 timeout：sub_agent 30 分钟 / CLI tool 5 分钟 / iOS 2 分钟 / ask_user 30 分钟。

### 3.5 `evaluate_completion` (LLM #4)

**职责**：判断本轮 tool 结果是否满足用户需求。

**输出**：

```python
class EvaluateCompletionResult(BaseModel):
    satisfied: bool
    reasoning: str
    needs_more_tools: list[ToolCallSpec]  # 不满足时的下一批
    final_message: str                    # 满足时给用户的自然语言回复
```

**循环**：
- `satisfied=true` → 推 `message/text(final_message)` → END
- `satisfied=false` 且有 `needs_more_tools` → 回到 execute_plan
- 最多 `MAX_PLAN_ITERATIONS=3` 次迭代

---

## 四、17 个业务场景

详见 [main_agent_prompts.py](../../src/paper_search/agent/main_agent_prompts.py) 的 `SCENARIOS` 字典。

每个 scenario 含 `name / description / agent / permissions / example`。这些信息会渲染到 `intent_classify` 的 system prompt 里作为业务能力清单。

---

## 五、事件源 Checkpoint

每个状态变更通过 `_record_event(session_id, event_type, payload)` 写入 `agent_events` 表。

**15 种 event_type**：

```
turn_started / turn_completed
intent_classified
scenario_planned / inline_reply_sent
clarification_requested / clarification_received
plan_approval_requested / plan_approved / plan_rejected
tool_call_started / tool_call_progressed / tool_call_completed / tool_call_failed
completion_evaluated
```

每条事件包含 `correlation_id`（一轮对话的 UUID），可通过它检索整条事件链。

**Crash Recovery**：daemon 启动时调 `_recover_pending_turns()`，对每个未完成的 correlation_id 运行 `_replay(events)` 重建 state，然后 `_resume_from_state()`：

| state.phase + waiting_for | 恢复动作 |
|---|---|
| waiting_for=clarification/approval | 推 high 提醒到 iOS，标 turn_completed (用户回复时开新轮) |
| 有 running tool_call | 标失败 + 推 error，标 turn_completed |
| 其他 phase | 标 turn_completed(abandoned) |

策略保守（不重跑非确定性 LLM 节点），以稳定性为先。

---

## 六、与外部组件的交互

### 6.1 出站消息 - Outbox

所有 `_push()` 走 `outbox_publish()`：双写 ws_messages 表 + LPUSH `outbox:{agent_id}`，由 API 进程的 `outbox_poller` 消费分发到 WS / APNs。

silent 消息（流式 thinking delta）不写 SQLite，只入 Redis 队列，避免 IO 暴涨。

### 6.2 入站消息 - BRPOP

`agent:ws:{agent_id}` 是 iOS → API → Agent 的入站队列。MainAgent.run() BRPOP 取消息，drain 队列里所有积压 + `parked` 队列的暂存（来自 `_wait_ws_reply` 不匹配 buffer），合并为一轮 user_content。

### 6.3 子 Agent - Celery

通过 `sub_agent_task.delay(user_query, project_id, agent_task_id)` 派发，订阅 `agent:reports:{task_id}` 收 progress + lifecycle。

子 Agent 在收尾时**必须**调 `reporter.publish_lifecycle(task_id, "agent_done"|"agent_failed", ...)`，否则主 Agent 无法判定完成。

### 6.4 MemGPT 4 层记忆

`_build_history_context` 注入：
- MetaMemory user_preferences（≥0.3 置信度）
- LongTerm get_full_profile()（用户画像）
- ShortTerm.get_context(max_tokens=8000)

每轮收尾时 `_write_short_term()` 把对话 + tool 结果同步进 ShortTerm；token 超阈值时通过 system message 提示 LLM 调用 `summarize_memory / delete_memory / extract_to_long_term`。

---

## 七、相比 v1/v2 的关键变化

| 维度 | v1 (PlanGraph) | v2 (AgentLoop) | MainAgent |
|---|---|---|---|
| 调度模型 | LangGraph 节点 + Tick-Polling | LLM tool-calling 20 轮 | 显式 5 节点状态机 |
| LLM 决策 | parse/clarify/generate 3 阶段 | 自由 tool-calling | JSON Schema 强约束 |
| 非业务分支 | 无 | LLM 间接判断 | 显式 inline_reply 分支 |
| plan 卡片 | review/plan envelope (v7.0) | 无 | tool/propose_plan (v9.0) |
| 跨轮上下文 | LangGraph SqliteSaver | 失忆 | MemGPT short_term + meta + long_term |
| 离线消息 | 直接丢 | 直接丢 | outbox 持久化 + APNs |
| Crash recovery | LangGraph checkpoint (未真用) | 无 | agent_events 事件源 replay |
| 子 Agent 完成判定 | LangGraph result | per-paper status=done (P0 bug) | type=lifecycle (修复) |

---

## 八、文件清单

```
src/paper_search/agent/
├── main_agent.py             — MainAgent 主类 (5 节点 + recovery)
├── main_agent_prompts.py     — 3 个 LLM 节点 prompt + Pydantic schemas + 17 scenarios
├── outbox.py                 — 出站双写
├── reporter.py               — Celery → Agent + lifecycle 上报
├── daemon.py                 — AgentBootstrap + MainAgent 启动入口
└── tool_registry.py          — 56 个工具 (含 5 个记忆工具的真实实现)

src/paper_search/api/
├── outbox_poller.py          — 每 agent 一个 poller，WS / APNs 分发
├── apns_pusher.py            — APNs 推送 (Phase 1 骨架)
└── app.py                    — WebSocket 端点 + sync_request 处理
```

> 已删除的旧文件：`agent_loop.py / event_bus.py / prompt_optimizer.py / task_event_adapter.py / ws_handler.py / graphs/plan_graph.py / graphs/execute_graph.py`
