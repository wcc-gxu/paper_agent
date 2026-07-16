# [DEPRECATED] MainAgent v2 目标架构设计

> 2026-06-23 | 主 Agent 重构设计 | 配套:[websocket-protocol.md](websocket-protocol.md) v10 / [plangraph-routing.md](plangraph-routing.md)(待建,另有 agent 撰写)

本文档定义 MainAgent 的**目标架构**(v2),作为后续实现依据。当前代码仍为 v9(6 节点状态机),见 [main-agent.md](main-agent.md)。本文不描述现状,只描述要达到的状态。

---

## 一、重构动机

v9 主 Agent(6 节点状态机)存在三个问题,v2 逐一解决:

1. **内部编排泄漏**:v9 的 `message/thinking` 把 intent_classify / scenario_plan / evaluate_completion 的 LLM JSON 来回流式暴露给用户,用户看到的是 schema 字段名和 JSON 片段,体验差。v10 协议已删除 `message/thinking`,内部编排不再产生用户可见消息,改由 `status` 消息给人类可读的阶段反馈。**本项由 v10 协议解决,v2 不再重复设计。**

2. **缺状态反馈**:intent_classify 耗时 5-10s,期间用户面对空白屏幕。v10 已新增 `status` 消息(`received`/`analyzing`/`planning`/...),MainAgent 收到消息后 <200ms 必须推 `status{stage:"received"}`。**本项由 v10 协议解决,v2 在各节点入口/出口推 status。**

3. **过度规划 + 覆盖缺口**(v2 核心动机):
   - **过度规划**:scenario_plan 让 LLM 生成 `tools[]`,但 17 个业务场景全是固定流水线(ingest 7 阶段、citation_chase 7 节点、translation 3 分支…),LLM 生成 tools[] 既慢又不可靠。v2 把 tools[] 生成从 LLM 下沉到 PlanGraph 硬编码路由表,LLM 只负责判场景号和给 clarity。
   - **覆盖缺口**(P0 bug):`_handle_sub_agent` 写死按 ingest 子 Agent 分发,导致 S3(订阅)/S6(研究空白)/S8(聚类)/S9(引用追溯)/S12(翻译)/S13(视频)这些场景的子 Agent 静默跑错或跑成 ingest。v2 通过 PlanGraph 按 `scenario_id` 显式路由到对应子 Agent 修复此 bug。

---

## 二、链路总览

```
用户消息
  ↓
[0] safety_filter (regex 黑名单 → 命中时小 LLM 二次确认) [已有,沿用 v9]
  ↓ safe
[1] fast triage (规则 <10ms → 未命中 → 小模型 ~500ms)
    schema: {route, confidence, all{5类}, reasoning(不限字数)}
    保守短路: 仅 chat/unsupported 且 confidence≥0.85 才短路
    失败 fallback: business
  ↓
  ├─ chat/unsupported → [2A] inline_reply (流式) → END
  ├─ ops(超管校验) ─┐
  ├─ meta ──────────┴→ [2B] 轻量规划节点(共用函数,换 prompt)
  │    LLM: {need_tool, tool_name, query/command, risk_level}
  │    系统校验 risk(黑名单强制升级 high)→ ask(kind=confirm) → 执行
  │    meta 限只读白名单 tool
  └─ business → [3] scenario_plan(并行多场景)
       LLM: {scenarios:[{id,confidence}], clarity, clarify_questions?}
       clarity<0.6 → ask(单卡合并多场景问题)
       → [4] PlanGraph 硬编码路由(17 场景) → [5] execute(分层并行)
       → [6] evaluate_completion → [7] final reply
```

上述流程含**九个编排阶段**,其中七个为 LLM 节点(safety_filter / fast triage / inline_reply / 轻量规划 / scenario_plan / evaluate_completion / final reply),PlanGraph 为硬编码路由表(非 LLM),execute 为调度器(非 LLM)。其中 safety_filter 已在 v9 实现,v2 沿用。

---

## 三、五个意图大类

fast triage 把请求分到五类 route 之一。相比 v9 的 4 类(`business / chat / meta / unsupported`),v2 新增 `ops`,把运维操作从 business 中拆出独立处理。

| route | 含义 | 短路? | 走向 | 备注 |
|---|---|:---:|---|---|
| `chat` | 闲聊 / 问候 / 能力咨询 | ✅ confidence≥0.85 | inline_reply | 不消耗子 Agent |
| `unsupported` | 能力外 / 话题无关 | ✅ confidence≥0.85 | inline_reply | 礼貌说明边界 |
| `ops` | 运维操作(重启服务 / pip / docker) | ❌ 一律进正式流程 | 轻量规划节点 | 超管限定,非超管拒绝 + 审计 |
| `meta` | 记忆查询 / 偏好读取等元操作 | ❌ 一律进正式流程 | 轻量规划节点 | 限只读白名单 tool |
| `business` | 17 业务场景之一 | ❌ 一律进正式流程 | scenario_plan → PlanGraph | 核心路径 |

**保守短路原则**:只有 `chat` / `unsupported` 且 `confidence≥0.85` 才短路到 inline_reply;`ops` / `business` 一律进正式流程(运维和业务请求不能因"看起来像闲聊"被误短路)。

**ops 超管限定**:MVP 阶段当前用户即超管,代码保留 `is_admin` 检查;非超管发 ops 请求 → 拒绝 + 写审计日志。未来多用户时按角色扩展。

**ops 跳过 17 场景评估**:对运维请求评估 17 个学术场景无意义,fast triage 直接路由到轻量规划节点。S16"运维操作"场景编号保留,但在 v2 中由 ops route 承接,不进入 scenario_plan 评估范围(避免与 [CLAUDE.md](../../CLAUDE.md) 的 17 场景表冲突,仅调整归属)。

---

## 四、节点职责

每个节点给出:输入 / 输出 schema / 模型 / 并行性。

### 4.0 `safety_filter` [已有,沿用 v9]

- **职责**:拦截对抗性输入(prompt_injection / jailbreak / pii_leak),regex 黑名单 + 命中时小 LLM 二次确认
- **输入**:用户最新消息(无 history)
- **输出**:`SafetyResult {safe, risk_kind, reasoning, user_message}`
- **模型**:doubao-seed-2.0-mini(降级 lite)
- **并行性**:不并行(收敛判断,前置单点)
- **详细实现**:见 [main-agent.md §3.0](main-agent.md),v2 无改动

### 4.1 `fast triage` [v2 新增]

- **职责**:把请求快速分到五类 route。规则优先(<10ms 秒过常见请求),未命中调小模型(~500ms)
- **输入**:用户消息 + ShortTerm 滑动窗口(轻量)
- **输出**:

```python
class TriageResult(BaseModel):
    route: Literal["chat", "meta", "unsupported", "ops", "business"]
    confidence: float                       # 对 route 判断的置信度
    all: dict[str, float]                   # 五类各一个分数 {chat,meta,unsupported,ops,business}
    reasoning: str                          # 不限字数,审计 + debug 用
```

- **模型**:doubao-seed-2.0-mini(降级 lite)
- **并行性**:不并行(收敛判断)
- **规则层**(正则 / 关键词,命中即返回,不调 LLM):
  - 问候 / 感谢 / 能力问 → `chat`
  - 含 `重启 / restart / pip install / docker / systemctl` → `ops`
  - 含 `我的偏好 / 记不记得 / search_memory` → `meta`
- **小模型层**:规则未命中时调 mini 模型,给五类分数 + reasoning
- **保守短路**:仅 `route∈{chat,unsupported}` 且 `confidence≥0.85` 才短路到 inline_reply;`ops` / `business` 不论置信度都进正式流程
- **失败 fallback**:小模型调用失败(报错 / 超时 / 限流 / JSON 校验失败)→ 保守路由到 `business`(走完整 17 场景评估,最全链路兜底)

### 4.2 `inline_reply` [已有,v2 沿用]

- **职责**:`chat` / `unsupported` 短路,直接生成自然语言回复
- **输入**:用户消息 + ShortTerm + MetaMemory 偏好
- **输出**:流式 Markdown 文本 → `message/reply`(一次性完整,不流式 token;长回复期间由 `status` 撑场)
- **模型**:doubao-seed-2.0-lite(降级 deepseek-v4-flash)
- **并行性**:不并行(单次流式)
- **工具白名单**:仅 `search_memory / get_user_preference / extract_to_long_term`(轻量记忆工具)
- **不进入**:Celery 子 Agent / plan 卡片 / await_approval

### 4.3 `轻量规划节点` [v2 新增,ops + meta 共用]

- **职责**:为 `ops` / `meta` 生成单步执行计划(不涉子 Agent 流水线,无需 scenario_plan / PlanGraph)。单一函数 `_lightweight_plan(route)` 按 route 切换 prompt,ops 和 meta 共用同一调度框架。
- **输入**:用户消息 + route
- **输出**:

```python
class LightweightPlan(BaseModel):
    need_tool: bool                         # false → 直接 inline_reply
    tool_name: str                          # 白名单内
    query: str | None                       # meta: 查询表达式
    command: str | None                     # ops: shell 命令
    risk_level: Literal["low", "medium", "high"]
    reasoning: str
```

- **模型**:ops 命令生成用 doubao-seed-2.0-code(降级 glm-5.2);meta 规划用 doubao-seed-2.0-lite(降级 deepseek-v4-flash)
- **并行性**:不并行(单步,无并行意义)
- **risk 双判**:
  1. LLM 给 `risk_level`
  2. 系统黑名单校验:命令匹配 `rm -rf /` / `sudo` / `pip install` / `npm install` / `> /dev/sd*` 等模式 → **强制升级 high**,覆盖 LLM 判断
- **high → ask(kind=confirm)**:展示命令 / 查询给用户确认;iOS 按钮需二次确认(对应 v10 `danger_level=high`)
- **meta 限制**:只读白名单 tool(`search_memory / get_user_preference / list_memory / read_log`),任何写操作降级拒绝
- **ops 限制**:命令黑名单(见上);非超管直接拒绝 + 审计

### 4.4 `scenario_plan` [已有,v2 瘦身]

- **职责**:v2 瘦身后 LLM 只做三件事:① 判场景号(支持复合)② 给整体 clarity ③ clarity<0.6 时生成澄清问题。**不再生成 `tools[]`**(下沉到 PlanGraph)。
- **输入**:用户消息 + ShortTerm + MetaMemory + 17 场景清单(S1-S15、S17;S16 由 ops 承接)
- **输出**:

```python
class ScenarioMatch(BaseModel):
    scenario_id: Literal["S1", ..., "S15", "S17"]   # S16 不在此评估范围
    confidence: float

class ScenarioPlanResult(BaseModel):
    scenarios: list[ScenarioMatch]          # 1~N 个,支持复合意图
    clarity: float                          # 整体复合意图单一置信度 ∈[0,1]
    clarify_questions: list[ClarificationQuestion]  # clarity<0.6 时 LLM 主动生成
    reasoning: str
```

- **模型**:glm-5.2(降级 deepseek-v4-pro)
- **并行性**:✅ **多场景 plan 实例并行**——复合意图时对每个 ScenarioMatch 调 `_plan_one_scenario`,`asyncio.gather` 并行(省 (N-1)×LLM 延迟)
- **clarity 判定**:单一分数,整体复合意图一个分;非每场景独立分
- **与 v9 区别**:删除 `tools: list[ToolCallSpec]` / `needs_approval` / `permissions_required` / `estimated_time_seconds`(这些由 PlanGraph 按 scenario_id 查表生成,LLM 不参与)

### 4.5 `PlanGraph` [v2 新增,非 LLM 节点]

- **职责**:按 `scenario_id` 查硬编码路由表,展开 `tools[]`(sub_agent 调用 + 依赖关系)。替代 v9 让 LLM 生成 tools[] 的职责。
- **输入**:`scenarios: list[ScenarioMatch]` + 用户消息
- **输出**:`tools: list[ToolCallSpec]`(含 `call_id / kind / name / arguments / depends_on`)+ `permissions` + `danger_level`(按 v10 §4.4 硬映射)+ `estimated_seconds`
- **模型**:无(纯代码路由表)
- **并行性**:多场景时合并 tools[](拼接 + call_id 前缀防撞车 + depends_on 重写),不涉及 LLM 并行
- **路由表**:17 场景每个对应固定子 Agent / 工具流水线,详见 [plangraph-routing.md](plangraph-routing.md)(待建)
- **danger_level 硬映射**:按 scenario 的 `permissions_required` 查表(v10 协议 §4.4),LLM 不参与;`low` 直接执行,`medium` / `high` 弹 `ask(kind=plan)`

### 4.6 `execute` [已有,v2 分层并行强化]

- **职责**:执行 PlanGraph 返回的 `tools[]`
- **算法**:拓扑排序分批(按 `depends_on`),每批 `asyncio.gather(return_exceptions=True)`
- **分层并行**(v2 明确三层):
  - **顶层**:多个无依赖的 sub_agent 并行(复合意图 S1+S12 → ingest 与 translation 同时跑)
  - **中层**:同一 sub_agent 内多搜索源并行(arxiv + semantic_scholar + elsevier + ieee 同时)
  - **底层**:batch 操作并行(批量下载 PDF / 批量翻译)
- **4 类调度**(沿用 v9):

| kind | dispatch 路径 | 完成判定 |
|---|---|---|
| `sub_agent` | `celery_tasks.sub_agent_task.delay()` + 订阅 lifecycle | `type=lifecycle && lifecycle∈{agent_done,agent_failed}` |
| `tool` | `ToolRegistry.get(name)` → `run_in_executor` | 函数返回 / 抛异常 |
| `ios_tool` | 推 `tool/call` | 等 `tool_result` |
| `ask_user` | 推 `ask` | 等 `ask_reply` |

- **超时**:sub_agent 30min / CLI tool 5min / iOS 2min / ask_user 30min
- **P0 修复**:`_handle_sub_agent` 按 `agent_type` 分发(不再写死 ingest),复活 C 档(详见 [plangraph-routing.md](plangraph-routing.md))

### 4.7 `evaluate_completion` [已有,v2 沿用]

- **职责**:单次 LLM 判断本轮 tool 结果是否满足用户需求
- **输入**:用户原始消息 + tools 执行结果
- **输出**:`EvaluateCompletionResult {satisfied, reasoning, needs_more_tools, final_message}`
- **模型**:默认 doubao-seed-2.0-lite;实测误判率高 → 升 deepseek-v4-pro
- **并行性**:不并行(收敛判断);`satisfied=false` 时 `needs_more_tools` 回 execute 并行执行
- **循环**:最多 `MAX_PLAN_ITERATIONS=3` 次

### 4.8 `final reply` [v2 明确为独立节点]

- **职责**:`satisfied=true` 后,单次 LLM 生成完整 Markdown 回复(综述 / 对比 / 翻译结果汇总等)
- **输入**:用户消息 + tools 结果 + evaluate 的 final_message 草稿
- **输出**:`message/reply`(priority=high,一次性完整,触发 APNs)
- **模型**:glm-5.2(降级 deepseek-v4-pro)
- **并行性**:不并行(单次完整生成)
- **与 v9 区别**:v9 复用 evaluate 的 `final_message`;v2 拆出独立节点,让 evaluate 只管判断、final reply 专注生成高质量回复

---

## 五、模型分配

7 个模型全走火山引擎同一 `VOLCANO_API_KEY`,同一 SDK,靠 model ID 切换即降级。配置集中在 [src/paper_search/config.py](../../src/paper_search/config.py) 与 [src/paper_search/agent/llm_client_v2.py](../../src/paper_search/agent/llm_client_v2.py)。下表含主 Agent 节点与子 Agent 内部 LLM 调用。

| 层 | 节点 / 环节 | 主模型 | 降级 |
|---|---|---|---|
| 极速层 | fast triage / safety_filter / ingest_evaluate | doubao-seed-2.0-mini | doubao-seed-2.0-lite |
| 中速层 | inline_reply / 轻量规划(meta) / evaluate_completion / cluster_label / citation_filter / rad_route | doubao-seed-2.0-lite | deepseek-v4-flash |
| 旗舰层 | scenario_plan / 综述生成 / 翻译 / video_analyze / gap 分析 / final reply | glm-5.2 | deepseek-v4-pro |
| 代码层 | 轻量规划(ops 命令生成) | doubao-seed-2.0-code | glm-5.2 |

**特殊处理**:
- `evaluate_completion`:默认 lite,实测误判率高(漏判未完成 / 误判已完成)→ 升 pro
- **降级触发**:主模型失败(报错 / 超时 / 限流 / JSON 校验失败)→ 切 fallback 同 key 重试,最多 1 次降级
- **成本热点**:`ingest_evaluate` 每轮 ~50 次调用(每篇论文一次)用 mini 控成本;`scenario_plan` 并行 N 实例用旗舰但单次 token 小

---

## 六、并行机会

| 环节 | 并行? | 单元 | 收益 | 代价 |
|---|:---:|---|---|---|
| safety_filter | ❌ | — | 收敛判断 | — |
| fast triage | ❌ | — | 收敛判断 | — |
| scenario_plan | ✅ | 多场景 plan 实例 | 省 (N-1)×LLM 延迟 | N×旗舰 token(但单次小) |
| needs_clarification | ❌(合并) | 多场景问题合并单张 ask 卡 | 单次交互 | 合并逻辑 |
| needs_approval | ❌(合并) | 取最高 danger_level | 单次审批 | — |
| execute 顶层 | ✅ | 无依赖 sub_agent | 省 (N-1)×子 Agent 延迟 | 资源竞争(见 §七) |
| execute 中层 | ✅ | 多搜索源 | 省 ~6×搜索延迟 | 搜索 API rate limit |
| execute 底层 | ✅ | batch 下载 / 翻译 | 省几十秒 | 带宽 / 翻译并发上限 |
| needs_more_tools | ✅ | 无依赖工具 | 省串行延迟 | — |
| evaluate_completion | ❌ | — | 收敛判断 | — |
| 轻量规划 | ❌ | — | 单步 | — |
| final reply | ❌ | — | 单次完整生成 | — |

---

## 七、并发限流要点

不同资源类型分别设信号量,**不**用全局统一池:

| 资源 | 并发上限 | 说明 |
|---|:---:|---|
| LLM 调用 | ≤4 | 防火山方舟并发上限 / 限流 |
| 搜索源(arxiv) | 1 req/s | arxiv rate limit |
| 搜索源(semantic_scholar) | 1 req/s | SEMANTIC_SCHOLAR_API_KEY 限额 |
| 搜索源(elsevier) | 按 5000 req/周 节流 | ELSEVIER_API_KEY 限额 |
| 搜索源(ieee) | 按 200 req/day 节流 | IEEE_API_KEY 限额 |
| PDF 下载 | ≤5 | 带宽 + 反爬 |
| 翻译 batch | ≤20 | 单次 batch 上限 |

要点:
- 按资源瓶颈分别设信号量,非全局统一池(避免下载把 LLM 配额占满)
- 搜索 API 各自独立限流,防打爆 rate limit
- 详细实现策略(信号量池 / 令牌桶 / 退避)另定,本文只列要点

---

## 八、身份三层(命名定稿)

v2 只有三类身份,**不引入第四种**:

| 身份 | 数量 | 职责 | 协议可见? |
|---|:---:|---|:---:|
| **MainAgent** | 1(唯一) | 编排者,跑节点状态机 | 是(通过 outbox 推消息) |
| **节点(node)** | 多 | LLM 决策单元(safety / triage / plan / eval / 轻量规划),可并行多实例 | 否(不进协议不进 schema) |
| **sub_agent** | 7 | 重量执行单元(7 个 graph),有 lifecycle 上报 | 是(`tool/*` + lifecycle) |

- 讨论节点并行实例时可称 **planner shard**(如"scenario_plan 的 3 个 planner shard 并行"),但 `planner shard` 是讨论用语,**不进协议、不进 schema、不进代码类名**
- sub_agent 7 个:Ingest / RADQuery / Clustering / CitationChase / History / Translation / Video(见 [main-agent.md](main-agent.md))
- PlanGraph **不是**身份,是 MainAgent 内的编排阶段(硬编码路由表)

---

## 九、澄清触发逻辑

- **单一 clarity 分数**:整体复合意图一个分,∈[0,1](非每场景独立分)
- **阈值**:`clarity < 0.6`(默认,可配 `INTENT_ASK_THRESHOLD` 环境变量)→ prompt 指示 LLM 主动生成 `clarify_questions`
- **多场景澄清问题合并**:复合意图的多个场景的澄清问题合并成单张 `ask` 卡(非每场景一张),减少用户交互轮次
- **灰区(C3)**:`scenarios` 中所有场景 `confidence < 阈值` → `ask(kind=multi_choice)` 列候选场景 + "都不是 / 重新描述"
- 用户选"都不是" / 超时 → 降级 `chat`,走 inline_reply

---

## 十、待办与优先级

| # | 优先级 | 任务 | 依赖 |
|---|:---:|---|---|
| 1 | P0 | 修 `_handle_sub_agent` 按 `agent_type` 分发(复活 C 档) | 另有 agent 在改 |
| 2 | P1 | scenario_plan 瘦身:删 LLM 生成 `tools[]`,改 PlanGraph 硬编码路由表 | 依赖 #7 |
| 3 | P1 | fast triage 新增:规则 + 小模型级联节点 | — |
| 4 | P1 | 轻量规划节点:ops + meta 共用 `_lightweight_plan` | — |
| 5 | P2 | 并行改造:scenario_plan `asyncio.gather` 多场景 | 依赖 #2 |
| 6 | P2 | 模型路由表 + 降级(config.py / llm_client_v2) | 另有 agent 在改 |
| 7 | P2 | 17 场景路由表 + C 档实现 | 详见 [plangraph-routing.md](plangraph-routing.md)(待建) |

---

## 十一、与现有文档关系

| 文档 | 角色 | 状态 |
|---|---|---|
| **本文档**(main-agent-v2-design.md) | 目标架构(要达到的状态) | 新建 |
| [main-agent.md](main-agent.md) | 当前代码实现(v9,迁移中,顶部有横幅) | 待同步 |
| [websocket-protocol.md](websocket-protocol.md) v10 | 通信协议(已定稿) | ✅ |
| [plangraph-routing.md](plangraph-routing.md) | 17 场景路由表 + C 档计划 | 待建(另有 agent 撰写) |
| [CLAUDE.md](../../CLAUDE.md) | 项目拓扑(状态表已标 v10) | ✅ |

迁移完成后,本文档与 [main-agent.md](main-agent.md) 合并,顶部横幅移除。
