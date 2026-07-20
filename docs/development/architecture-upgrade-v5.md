# Paper Agent v5 — 架构升级方案

> v4.2 → v5.0 | 2026-07-20
>
> **核心理念**：意图驱动的确定性路由，去 LLM 规划，去子 Agent，节点自治。

---

## 一、动机

### v4 架构的问题

```
fast_triage → intent_classify → plan → clarify ⇄ plan
                                     → gate → execute ⇄ todo_checkpoint → evaluate
                                              (approve)     (retry)          (done/retry/replan/fail)
```

| 问题 | 影响 |
|------|------|
| **plan 节点**：每次请求 1 次 LLM 调用生成 PlanOutput | 增加 5-10s 延迟 + token 成本 |
| **evaluate 节点**：每次 todo 完成 1 次 LLM 调用判 5 出口 | 额外延迟 + 可能产生 replan 无限循环 |
| **todo_checkpoint 节点**：每个 todo 1 次 LLM 调用验证 | 冗余——流程是确定性的 |
| **clarify 节点**：信息收集用 ReAct，逻辑与 execute 重复 | 可以用 handler 内 inline ask 替代 |
| **gate 节点**：plan_review / ask_user 逻辑混在一个方法 | handler 可以内联 push ask + wait |

**核心认知**：18 个业务场景全部是**确定性流程**。无需 LLM 规划，无需 LLM 评估。需要用户决策的点用 `ask` 即可。

### 成本对比

| 指标 | v4（当前） | v5（目标） |
|------|:---:|:---:|
| 每次请求 LLM 调用（最少） | 4（triage + intent + plan + evaluate） | 2（triage + intent） |
| 节点数 | 11 | 7（路由 3 + handler 4+） |
| 子 Agent 图 | 8 个独立 StateGraph | 0 |
| `agent_*` tool | 11 个（内建子 Agent） | N 个原子 tool |
| 并行执行 | 注释说并行、实际串行 | 全串行 |

---

## 二、设计原则

| 原则 | 说明 |
|------|------|
| **确定性路由** | intent_classify 后 → 路由表查固定 handler 节点序列 |
| **节点自治** | 每个 handler 内部处理工具调用、用户交互、错误 |
| **主/辅意图分离** | primary（1 个业务意图）+ side（0-N 个辅助意图） |
| **串行优先** | 工具调用全部串行 |
| **用户交互统一** | 所有需要用户操作的场景走 `ask` 消息 |
| **后台任务可控** | 耗时操作进 Celery 前 `ask` 确认 |
| **Turn 边界清晰** | handler 结束 → `message/reply` → turn 完成 |

---

## 三、图结构

### 3.1 节点总图

```
                          ┌─ chat ──→ inline_reply ──→ END
                          │
fast_triage ──→ intent_classify ──→ [primary route]
                          │
                          └─ ops ──→ ops_confirm ──→ execute(ReAct) ──→ END
```

### 3.2 Primary 路由表

> ✅ = 已实现 | 🔧 = 待实现

| primary intent | handler 节点 | 说明 | 状态 |
|---------------|-------------|------|:---:|
| `rag` | `rag_handler` | BM25+向量检索 → LLM 回答 | ✅ |
| `survey` | `literature_search_handler` | 搜索 → 评估 → 返回结果 + 保存调研报告 | ✅ |
| `ingest` | `ingest_handler` | 扫描本地目录 → PDF→MD → 切片入库 | ✅ |
| `cleanup` | `cleanup_handler` | 删除原始 PDF/MD 文件，保留 DB 记录 | ✅ |
| `translation` | `translate_handler` | glossary_search → LLM 翻译 | 🔧 |
| `writing` | `writing_handler` | 综述/AI检测/gap分析 | 🔧 |
| `glossary` | `glossary_handler` | 词表收集 → 校验 | 🔧 |
| `clustering` | `cluster_handler` | K-means → LLM label → 可视化 | 🔧 |
| `citation_chase` | `citation_handler` | 引用追溯（条件边） | 🔧 |
| `paper_analysis` | `paper_handler` | 单篇精读 → 知识提取 | 🔧 |
| `knowledge_mgmt` | (按需路由) | 订阅/全文获取 | 🔧 |
| `chat` | `inline_reply` | 纯 LLM 回复 | ✅ |
| `ops` | `ops_confirm` → `execute` | 运维操作 | ✅

### 3.3 已实现的 Handler 节点

```
Turn 1: 用户:"找 transformer 论文"
  → literature_search_handler
     → search_papers(BM25+向量混合) → evaluate(LLM相关性)
     → message/reply(结果列表 + 摘要) → turn 结束
     如果 query 模糊: ask(text/choice) 先澄清

Turn 2: 用户:"下载前 10 篇" 或 "写综述"
  → download_papers_handler
     10 篇 → inline 阻塞下载
     50+ 篇 → ask(confirm, "后台运行?") → Celery

## 八、日志

调研每次搜索都会更新日志记录到event_logs表中。

## 九、入库的handler默认会查找目录下的全部PDF文件，然后批量入库进DB，允许用户自定义目录路径。

## 十、清理handler会删除原始PDF/MD文件作为磁盘管理的一个步骤。

---

## 四、Intent 分类（输出格式）

```json
{
  "primary": "rag",
  "side": [
    {"type": "preference", "content": "只看 CCF-A 期刊"},
    {"type": "feedback", "content": "上次答案太短了", "sentiment": "negative"}
  ],
  "params": {
    "question": "attention 机制有哪些改进？"
  },
  "route": "rag"
}
```

`primary` 决定 handler。
`side[]` 由 side_handler 先消费。
`params` 透传给 handler 节点。

---

## 五、Handler 节点规范

每个 handler 节点遵循统一模版：

```python
async def _xxx_handler(self, state: MainState) -> dict:
    session_id = state["session_id"]
    params = state.get("intent_params", {})
    
    # 1. Push 开始状态
    await self._push_status(session_id, "executing", "正在处理...")
    
    try:
        # 2. 调工具 / LLM
        result = await do_work(params)
        
        # 3. Push 完成状态 + 回复
        await self._push_status(session_id, "done", "完成")
        await self._push(session_id, "message", "reply", "assistant",
                         payload={"content": format_result(result)})
        
        # 4. 可选: Push ask（如反馈收集）
        # await self._push(session_id, "ask", "", "assistant",
        #                  payload={"ask_id": "...", "kind": "confirm", ...})
        
        return {"final_reply": result, "_reply_pushed": True}
        
    except Exception as e:
        # 5. 错误处理: retry → fallback → ask
        await self._handle_error(session_id, e)
        return {"error": str(e)}
```

---

## 六、错误处理层级

```
1. 自动重试 1 次（相同参数）
2. 有降级方案 → 降级（如 arxiv 挂了 → 仅搜 S2）
3. 无降级 → LLM 决策: {action: "skip" | "retry_different" | "ask_user"}
4. LLM 无法解决 → push ask 给用户
5. 用户放弃 → push error → END
```

LLM 只做决策，不执行。决策空间限定为预定义 action 集合。

---

## 七、Tool 体系

### 7.1 设计规则

- Tools 可调用 LLM（当前 `agent_*` tool 已经这样做）
- 翻译的两步（glossary_search → LLM translate）中，glossary_search 是独立 tool，翻译在 handler 内调 LLM
- BM25 + 向量混合搜索在 `search_papers` / `search_kb` tool 内部实现

### 7.2 Tool 列表

| Tool | 来源 | 说明 |
|------|------|------|
| `search_papers` | LiteratureAgent | BM25+向量混合搜索，跨源（arxiv/S2） |
| `evaluate_papers` | LiteratureAgent | LLM 评估论文相关性 |
| `download_paper` | LiteratureAgent | 下载单篇 PDF |
| `convert_to_md` | LiteratureAgent | PDF→MD 转换 |
| `search_kb` | KnowledgeAgent | BM25+向量检索知识库 |
| `chunk_embed_ingest` | KnowledgeAgent | 切片+embedding+去重+入库 |
| `generate_survey` | WritingAgent | LLM 生成文献综述 |
| `check_ai_flavor` | WritingAgent | LLM 检测 AI 写作痕迹 |
| `gap_analysis` | WritingAgent | LLM 分析研究空白 |
| `glossary_search` | TranslationAgent | 词表检索匹配 |
| `collect_terms` | GlossaryAgent | TF-IDF 提取候选术语 |
| `verify_terms` | GlossaryAgent | LLM 校验术语定义 |
| `cluster_papers` | ClusteringAgent | K-means 聚类 |
| `fetch_citations` | CitationChaseAgent | S2 API 获取引用 |
| `filter_relevance` | CitationChaseAgent | LLM 过滤引用相关性 |
| `process_video` | VideoAgent | 下载+转写+摘要 |
| `record_feedback` | 新增 | 记录用户反馈/偏好/语录 |
| `update_preference` | 已有 | 更新用户偏好 |

---

## 八、WebSocket 协议影响

### 8.1 不变

- 通用信封格式、10 种 outbound type、7 种 inbound type
- `ask` 交互机制、`tool/start → progress → result` 进度推送
- APNs 离线推送规则

### 8.2 变更

- `role` 字段从信封中移除（已在 v10 标记删除，本次执行）
- `status` stage 值更新：去掉 `planning`/`verifying`，增加 handler 专属 stage（`searching`/`translating`/`clustering`）
- `plan_review` 消息删除（用 `ask(kind=plan)` 替代）
- `plan_todo_update` 消息删除（无 plan 则无 todo）

---

## 九、文档需要更新

| 优先级 | 文件 | 变更 |
|:---:|------|------|
| P0 | `CLAUDE.md` | 更新图结构、节点列表、架构概览 |
| P0 | `docs/development/development-plan.md` | v5 计划 |
| P0 | `docs/development/agent-architecture-v4.md` → `v5` | 更新架构内容 |
| P1 | `docs/development/plangraph-routing.md` | intent→handler 路由 |
| P1 | `docs/development/gap-analysis.md` | 重算 gap |
| P1 | `docs/development/acceptance-criteria.md` | 更新 AC |
| P2 | `docs/development/websocket-protocol.md` | 更新 stage 值表 |
| P2 | `docs/development/memory-system.md` | 更新上下文描述 |
| P3 | `docs/product/` 各文档 | 更新产品方案 |

---

## 十、迁移策略

1. **Phase 0**：死代码清理（无风险，先做）
2. **Phase 1**：主图简化（核心改造）
3. **Phase 2**：Handler 逐步实现（RAG 优先）
4. **Phase 3**：Celery 重新集成
5. **Phase 4**：跨 turn 状态管理
6. **Phase 5**：文档对齐

详见 `v5-development-plan.md`。

---

> 相关文档:
> - [v5 分阶段开发计划](v5-development-plan.md)
> - [WebSocket 协议 v11.1](websocket-protocol.md)
> - [数据库架构 v4.1](database-architecture.md)
