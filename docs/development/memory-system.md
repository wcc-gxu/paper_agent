# Paper Agent v3 — Memory 系统设计

> 完整 MemGPT 模式 4 层记忆 + RAG | 2026-06-14

---

## 1. 设计哲学

> 借鉴 MemGPT 虚拟内存管理：LLM 自主管理记忆，不依赖外部规则。

**核心原则**：
- **LLM 决定**：什么要记住、什么该忘记、什么时候检索
- **分层存储**：热数据（ShortTerm）、温数据（MidTerm）、冷数据（LongTerm/Meta）
- **RAG = Archival Memory**：外部文档（论文/网页）和内部记忆（对话历史）统一检索

---

## 2. 四层架构

```
┌─────────────────────────────────────────────────────┐
│                  ShortTerm Memory                    │
│           进程内存 · 滑动窗口 (~8000 tokens)          │
│     当前对话完整保留 · token超阈值触发 LLM压缩        │
├─────────────────────────────────────────────────────┤
│                  MidTerm Memory                      │
│          SQLite (task_checkpoints)                   │
│     任务进度 · 中间结果 · LangGraph checkpoint       │
│         支持崩溃恢复 · 操作审计日志                   │
├─────────────────────────────────────────────────────┤
│                  LongTerm Memory                     │
│      SQLite (knowledge_entries) + ChromaDB           │
│   论文知识 · 对话摘要 · 用户画像 · 研究偏好          │
│                  永久保留 · 语义可检索               │
├─────────────────────────────────────────────────────┤
│                   MetaMemory                         │
│           SQLite (strategy_log + preferences)        │
│    策略有效性记录 · 错误模式 · 用户偏好学习          │
│               驱动 Agent 自我改进                     │
└─────────────────────────────────────────────────────┘
```

---

## 3. ShortTerm Memory（短期记忆）

### 3.1 数据结构

```python
class ShortTermMemory:
    _turns: deque[ConversationTurn]  # 滑动窗口, max_turns=50
    _summary: str                     # 被压缩的旧对话摘要
    max_tokens: int = 8000            # 触发压缩阈值
```

### 3.2 LLM 压缩流程

```
token > 8000 → 触发压缩
    │
    ▼
Agent 注入: "你的上下文即将溢出。请管理短期记忆。"
    │
    ▼
LLM 使用记忆管理工具:
    summarize_memory(msg_12, msg_35)
        → 摘要: "用户讨论了自动驾驶安全的三个子方向..."
    
    delete_memory([msg_8, msg_9, msg_10])
        → 理由: "重复的确认回复"
    
    extract_to_long_term([
        {type: "preference", key: "preferred_sources", value: ["semantic_scholar"]},
        {type: "finding", key: "autonomous_driving_safety", value: "..."}
    ])
    
    tag_memory(msg_15, ["search_strategy", "key_decision"])
```

### 3.3 记忆管理工具（5 个）

| 工具 | 功能 | LLM 调用时机 |
|------|------|-------------|
| `summarize_memory` | 摘要一段对话 → 替换原文 | 旧对话不再需要细节 |
| `delete_memory` | 删除冗余消息 | 重复确认/问候/废弃方向 |
| `extract_to_long_term` | 提取持久知识 → LongTerm | 偏好/发现/重要决策 |
| `tag_memory` | 给消息打标签 | 方便后续检索 |
| `search_memory` | 搜索历史对话摘要 | 需要回忆之前讨论过什么 |

---

## 4. MidTerm Memory（中期记忆）

### 4.1 LangGraph Checkpoint

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string("~/.paper_search/agent.db")

plan_graph = StateGraph(ResearchState)
plan_graph.compile(checkpointer=checkpointer)
```

- 每个 node 执行后自动保存 state 快照
- Agent 重启时从 checkpoint 恢复
- `thread_id` = session_id

### 4.2 操作审计日志

```sql
task_history (task_id, action, detail, created_at)
```

记录所有关键操作：搜索了什么、下载了哪些、用户确认了什么、LLM 做了什么决策。

---

## 5. LongTerm Memory + RAG（长期记忆 = Archival Memory）

### 5.1 ChromaDB Collection 设计

```
papers_abstract       — 论文摘要索引 (快速筛选)
papers_fulltext       — 论文全文分块索引 (深度检索)
agent_conversations   — [NEW] 压缩后的对话摘要
agent_knowledge       — 结构化的知识条目 (已有 knowledge_entries)
agent_learnings       — [NEW] "用户偏好 X"、"检索 Y 好于 Z"
```

### 5.2 RAG = LLM Tool Use 自主检索

```
用户: "transformer 的注意力机制有什么改进？"
    │
    ▼
LLM 收到: 仅用户问题 (不自动检索)
    │
    ▼
LLM 思考: "这个问题需要查论文库"
    │
    ▼
LLM 发 tool_use: search_library("transformer attention improvement", top_k=5)
    │
    ▼
系统: ChromaDB 语义检索 → 5 个相关 chunk + paper_id
    │
    ▼
LLM 收到 tool_result → 分析 → 
    "还需要看第3篇的原文"
    │
    ▼
LLM 发 tool_use: read_paper("arxiv:1706.03762")
    │
    ▼
LLM 生成最终答案 (带引用标注)
```

### 5.3 检索工具集

| 工具 | 查询范围 | 用途 |
|------|----------|------|
| `search_papers` | 外部学术来源 | 搜索新论文（不在库里） |
| `search_library` | ChromaDB (papers_*) | 搜索已入库论文 |
| `search_knowledge` | ChromaDB (agent_knowledge) | 搜索提取的结构化知识 |
| `search_memory` | ChromaDB (agent_conversations) | 搜索历史对话 |
| `read_paper` | 文件系统 | 读取论文完整 Markdown |
| `get_paper_abstract` | SQLite | 仅看摘要（省 token） |
| `list_collections` | ChromaDB | 列出有哪些 collection 可用 |
| `get_user_preference` | MetaMemory | 查用户偏好 |

---

## 6. MetaMemory（元记忆）

### 6.1 策略学习

```sql
strategy_log (task_type, strategy_name, parameters, effectiveness, outcome)
```

- 记录：什么策略在什么场景下有效
- LLM 做决策时调用 `get_best_strategy(task_type)` 获取历史最优策略
- 自我改进：自动淘汰低效策略

### 6.2 错误模式

```sql
error_patterns (error_type, context, resolution, recurrence_count)
```

- 聚合同类错误 → 预判和预防
- `get_common_errors()` 帮助 LLM 了解"什么容易出错"

### 6.3 用户偏好

```sql
user_preferences (key, value, confidence, evidence_count)
```

- 增量学习：`(old_conf × N + new_conf) / (N+1)`
- 偏好多到一定置信度 (`≥0.3`) → 自动使用
- 例：`preferred_sources → ["semantic_scholar", "arxiv"]` (confidence: 0.85)

---

## 7. Event Cleanup 流程

```
Celery Beat: 每天触发 event_cleanup
    │
    ▼
LLM 审查旧事件:
    │
    ├── score ≥ 0.8 (明确有用)
    │   → 直接入库 RAG
    │   → paper → papers_fulltext
    │   → GitHub → documents (tech_research)
    │   → 权威论坛 → knowledge_entries
    │
    ├── score 0.3-0.8 (可能有用)
    │   → 发通知给 iOS (suggest_save)
    │   → 用户在线 → WS push
    │   → 用户离线 → APNs
    │   → 7天未确认 → LLM 再次审查 → 仍低则删除
    │
    └── score < 0.3 (废弃物)
        → 直接删除
        → 操作记录写入 agent.log
```

---

## 8. 与 MemGPT 论文的对应

| MemGPT 概念 | 本系统实现 |
|-------------|-----------|
| Main Context | ShortTerm Memory (滑动窗口) |
| Core Memory | MetaMemory (用户偏好 + 人设) |
| Archival Memory | LongTerm Memory (RAG: ChromaDB + SQLite) |
| Recall Memory | search_library / search_knowledge / search_memory |
| Memory Swap | summarize_memory / extract_to_long_term |
| Memory Edit | delete_memory / tag_memory |

---

> 版本: v1.0
