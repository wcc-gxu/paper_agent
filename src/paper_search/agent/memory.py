"""4层记忆系统 — 短期/中期/长期/元记忆.

架构:
  短期记忆 (进程内存)
    └─ 当前会话的对话上下文 + 工具调用历史
    └─ 容量: 受 token 限制, 滑动窗口管理

  中期记忆 (SQLite)
    └─ 当前任务的进度、中间结果、用户决策
    └─ 检查点 + 可恢复状态

  长期记忆 (ChromaDB + SQLite)
    └─ 跨项目知识: 论文库、Wiki、用户画像、笔记
    └─ 永不过期, 语义可检索

  元记忆 (SQLite)
    └─ 策略有效性记录、用户偏好学习、错误模式
    └─ 驱动 Agent 自我改进

使用方式:
    from paper_search.agent.memory import MemoryManager

    mem = MemoryManager(db)

    # 短期: 管理对话
    mem.short_term.add_message(ChatMessage(...))
    context = mem.short_term.get_context(max_tokens=8000)

    # 中期: 管理任务状态
    mem.mid_term.save_checkpoint(task_id, step_idx, state)
    checkpoint = mem.mid_term.load_checkpoint(task_id)

    # 长期: 知识检索
    results = await mem.long_term.search("transformer attention mechanism", top_k=5)

    # 元: 策略记录
    mem.meta.record_strategy(task_type, strategy, effectiveness)
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 前向引用 (避免循环导入)
from .llm_client_v2 import ChatMessage, ToolCall


# ═══════════════════════════════════════════════════════════════
# Layer 1: Short-Term Memory (进程内存)
# ═══════════════════════════════════════════════════════════════


@dataclass
class ConversationTurn:
    """一轮对话."""
    role: str  # user | assistant | tool
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    timestamp: float = field(default_factory=time.monotonic)
    token_count: int = 0  # 预估 token 数

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "timestamp": self.timestamp,
        }


class ShortTermMemory:
    """短期记忆 — 滑动窗口对话管理.

    特性:
    - 保留最近 N 轮对话
    - Token 估算和截断
    - 自动摘要 (超过阈值时压缩旧消息)
    """

    # 粗略估算: 1 token ≈ 4 字符 (英文) 或 2 字符 (中文)
    CHARS_PER_TOKEN = 3

    def __init__(self, max_turns: int = 50, max_tokens: int = 16000):
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self._turns: deque[ConversationTurn] = deque(maxlen=max_turns)
        self._summary: str = ""  # 被压缩的旧对话摘要

    def add_message(self, role: str, content: str, **kwargs):
        """添加一条消息."""
        turn = ConversationTurn(
            role=role,
            content=content,
            tool_calls=kwargs.get("tool_calls", []),
            token_count=self._estimate_tokens(content),
        )
        self._turns.append(turn)
        self._maybe_compress()

    def add_tool_call(self, name: str, arguments: dict, result: Any):
        """添加工具调用记录."""
        # Assistant 的工具调用
        self._turns.append(ConversationTurn(
            role="assistant",
            content="",
            tool_calls=[{"name": name, "arguments": arguments}],
        ))
        # Tool 结果
        result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        self._turns.append(ConversationTurn(
            role="tool",
            content=result_str,
            tool_results=[{"name": name, "result": result}],
        ))

    def get_context(self, max_tokens: int = 8000) -> list[dict]:
        """获取当前对话上下文 (适合发送给 LLM).

        Args:
            max_tokens: 最大返回 token 数

        Returns:
            Anthropic 格式的消息列表
        """
        messages = []
        total_tokens = 0

        # 如果有摘要, 先加入摘要
        if self._summary:
            summary_msg = {
                "role": "system",
                "content": f"[Previous conversation summary]\n{self._summary}",
            }
            messages.append(summary_msg)
            total_tokens += self._estimate_tokens(self._summary)

        # 从旧到新添加消息 (但限制 token)
        turns_to_include = []
        remaining = max_tokens - total_tokens
        for turn in reversed(self._turns):
            if remaining <= 0:
                break
            turns_to_include.append(turn)
            remaining -= turn.token_count

        for turn in reversed(turns_to_include):
            msg = {"role": turn.role, "content": turn.content}
            if turn.tool_calls:
                msg["tool_calls"] = turn.tool_calls
            messages.append(msg)

        return messages

    def get_last_n_turns(self, n: int) -> list[ConversationTurn]:
        """获取最近 N 轮."""
        turns = list(self._turns)
        return turns[-n:] if n < len(turns) else turns

    def _maybe_compress(self):
        """当 token 数超过阈值时, 压缩旧消息为摘要."""
        total = sum(t.token_count for t in self._turns)
        if total > self.max_tokens and len(self._turns) > 10:
            # 标记需要压缩 (实际压缩由 LLM 在合适时机完成)
            logger.debug(f"Short-term memory needs compression: {total} tokens > {self.max_tokens}")

    def set_summary(self, summary: str):
        """设置对话摘要 (由 LLM 压缩生成)."""
        self._summary = summary
        # 保留最近的 5 轮
        while len(self._turns) > 5:
            self._turns.popleft()

    def clear(self):
        self._turns.clear()
        self._summary = ""

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // self.CHARS_PER_TOKEN)


# ═══════════════════════════════════════════════════════════════
# Layer 2: Mid-Term Memory (SQLite — 任务状态)
# ═══════════════════════════════════════════════════════════════


@dataclass
class TaskCheckpoint:
    """任务检查点."""
    task_id: str
    step_index: int
    step_name: str
    state: dict[str, Any]  # 可序列化的状态快照
    results: dict[str, Any]  # 当前步骤结果
    created_at: str

    def to_row(self) -> dict:
        return {
            "task_id": self.task_id,
            "step_index": self.step_index,
            "step_name": self.step_name,
            "state": json.dumps(self.state, ensure_ascii=False),
            "results": json.dumps(self.results, ensure_ascii=False),
            "created_at": self.created_at,
        }


class MidTermMemory:
    """中期记忆 — 任务状态管理与检查点.

    持久化到 SQLite, 支持崩溃恢复.
    """

    def __init__(self, db):
        self._db = db  # AgentDB 实例
        self._ensure_table()

    def _ensure_table(self):
        """确保 checkpoints 表存在."""
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_name TEXT,
                state TEXT,
                results TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, step_index)
            )
        """)
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self._db.conn.commit()

    def save_checkpoint(self, task_id: str, step_index: int, step_name: str,
                        state: dict, results: dict):
        """保存检查点."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._db.conn.execute(
            """INSERT OR REPLACE INTO task_checkpoints
               (task_id, step_index, step_name, state, results, created_at)
               VALUES (?,?,?,?,?,?)""",
            (task_id, step_index, step_name,
             json.dumps(state, ensure_ascii=False),
             json.dumps(results, ensure_ascii=False),
             now),
        )
        self._db.conn.commit()

    def load_checkpoint(self, task_id: str) -> Optional[TaskCheckpoint]:
        """加载最近的检查点."""
        row = self._db.conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY step_index DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        rd = dict(row)
        return TaskCheckpoint(
            task_id=rd["task_id"],
            step_index=rd["step_index"],
            step_name=rd.get("step_name", ""),
            state=json.loads(rd.get("state", "{}")),
            results=json.loads(rd.get("results", "{}")),
            created_at=rd["created_at"],
        )

    def load_all_checkpoints(self, task_id: str) -> list[TaskCheckpoint]:
        """加载任务的所有检查点."""
        rows = self._db.conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        return [TaskCheckpoint(
            task_id=r["task_id"],
            step_index=r["step_index"],
            step_name=r.get("step_name", ""),
            state=json.loads(r.get("state", "{}")),
            results=json.loads(r.get("results", "{}")),
            created_at=r["created_at"],
        ) for r in rows]

    def record_action(self, task_id: str, action: str, detail: str = ""):
        """记录任务操作历史."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._db.conn.execute(
            "INSERT INTO task_history (task_id, action, detail, created_at) VALUES (?,?,?,?)",
            (task_id, action, detail, now),
        )
        self._db.conn.commit()

    def get_task_history(self, task_id: str, limit: int = 50) -> list[dict]:
        """获取任务操作历史."""
        rows = self._db.conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_task(self, task_id: str):
        """清理任务相关数据."""
        self._db.conn.execute("DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,))
        self._db.conn.execute("DELETE FROM task_history WHERE task_id = ?", (task_id,))
        self._db.conn.commit()


# ═══════════════════════════════════════════════════════════════
# Layer 3: Long-Term Memory (ChromaDB + SQLite)
# ═══════════════════════════════════════════════════════════════


@dataclass
class KnowledgeEntry:
    """知识条目."""
    id: str
    title: str
    content: str
    category: str  # contribution | method | dataset | limitation | finding
    source_paper_id: str
    source_paper_title: str
    confidence: float = 1.0  # 知识提取置信度
    created_at: str = ""
    tags: list[str] = field(default_factory=list)


class LongTermMemory:
    """长期记忆 — 跨项目知识积累.

    存储层:
    - SQLite: 结构化知识条目
    - ChromaDB: 语义检索

    知识类型:
    - 论文核心知识 (contribution/method/dataset/limitation)
    - 用户笔记和标注
    - 研究方向和兴趣画像
    - 项目 Wiki 页面
    """

    def __init__(self, db, chroma_store=None):
        self._db = db
        self._chroma = chroma_store  # ChromaStoreV2 实例 (可选)
        self._ensure_table()

    def _ensure_table(self):
        """确保知识库表存在."""
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'contribution',
                source_paper_id TEXT,
                source_paper_title TEXT,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags TEXT DEFAULT '[]'
            )
        """)
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_notes (
                id TEXT PRIMARY KEY,
                paper_id TEXT NOT NULL,
                content TEXT NOT NULL,
                note_type TEXT DEFAULT 'general',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._db.conn.commit()

    def add_knowledge(self, entry: KnowledgeEntry) -> str:
        """添加知识条目到 SQLite + ChromaDB."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eid = entry.id or _make_id(entry.title + entry.content[:50])

        self._db.conn.execute(
            """INSERT OR REPLACE INTO knowledge_entries
               (id, title, content, category, source_paper_id, source_paper_title,
                confidence, created_at, updated_at, tags)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eid, entry.title, entry.content, entry.category,
             entry.source_paper_id, entry.source_paper_title,
             entry.confidence,
             entry.created_at or now, now,
             json.dumps(entry.tags, ensure_ascii=False)),
        )
        self._db.conn.commit()

        # 同时索引到 ChromaDB (如果可用)
        if self._chroma:
            try:
                self._chroma.add_abstracts_batch([{
                    "paper_id": f"kb:{eid}",
                    "title": entry.title,
                    "abstract": entry.content,
                    "year": None,
                    "source": "knowledge_base",
                    "venue": entry.category,
                }])
            except Exception as e:
                logger.warning(f"Failed to index knowledge to ChromaDB: {e}")

        return eid

    async def search(self, query: str, top_k: int = 5,
                     categories: list[str] = None) -> list[KnowledgeEntry]:
        """语义搜索知识库.

        Args:
            query: 自然语言查询
            top_k: 返回条数
            categories: 过滤知识类型

        Returns:
            相关知识条目列表
        """
        results = []

        # 优先使用 ChromaDB 语义搜索
        if self._chroma:
            try:
                chroma_results = self._chroma.search_similar(query, n_results=top_k)
                for r in chroma_results:
                    eid = r.get("paper_id", "").replace("kb:", "")
                    if eid:
                        row = self._db.conn.execute(
                            "SELECT * FROM knowledge_entries WHERE id = ?", (eid,)
                        ).fetchone()
                        if row:
                            results.append(self._row_to_entry(dict(row)))
            except Exception as e:
                logger.warning(f"ChromaDB search failed, falling back to SQLite: {e}")

        # 降级: SQLite LIKE 搜索
        if not results:
            like_query = f"%{query}%"
            rows = self._db.conn.execute(
                "SELECT * FROM knowledge_entries WHERE title LIKE ? OR content LIKE ? "
                "ORDER BY confidence DESC LIMIT ?",
                (like_query, like_query, top_k),
            ).fetchall()
            results = [self._row_to_entry(dict(r)) for r in rows]

        if categories:
            results = [r for r in results if r.category in categories]

        return results

    def get_by_paper(self, paper_id: str) -> list[KnowledgeEntry]:
        """获取指定论文的所有知识条目."""
        rows = self._db.conn.execute(
            "SELECT * FROM knowledge_entries WHERE source_paper_id = ? ORDER BY category",
            (paper_id,),
        ).fetchall()
        return [self._row_to_entry(dict(r)) for r in rows]

    def _row_to_entry(self, row: dict) -> KnowledgeEntry:
        return KnowledgeEntry(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            category=row.get("category", "contribution"),
            source_paper_id=row.get("source_paper_id", ""),
            source_paper_title=row.get("source_paper_title", ""),
            confidence=row.get("confidence", 1.0),
            created_at=row.get("created_at", ""),
            tags=json.loads(row.get("tags", "[]")) if isinstance(row.get("tags"), str) else (row.get("tags") or []),
        )

    # ── 用户画像 ───────────────────────────────────────────

    def update_profile(self, key: str, value: Any):
        """更新用户画像字段."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        self._db.conn.execute(
            "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?,?,?)",
            (key, val_str, now),
        )
        self._db.conn.commit()

    def get_profile(self, key: str) -> Optional[Any]:
        """获取用户画像字段."""
        row = self._db.conn.execute(
            "SELECT value FROM user_profile WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        val = row["value"]
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val

    def get_full_profile(self) -> dict:
        """获取完整用户画像."""
        rows = self._db.conn.execute("SELECT key, value FROM user_profile").fetchall()
        profile = {}
        for r in rows:
            try:
                profile[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                profile[r["key"]] = r["value"]
        return profile

    # ── 用户笔记 ───────────────────────────────────────────

    def add_note(self, paper_id: str, content: str, note_type: str = "general") -> str:
        """添加用户笔记."""
        import uuid
        note_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._db.conn.execute(
            "INSERT INTO user_notes (id, paper_id, content, note_type, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (note_id, paper_id, content, note_type, now, now),
        )
        self._db.conn.commit()
        return note_id

    def get_notes(self, paper_id: str) -> list[dict]:
        """获取论文的所有笔记."""
        rows = self._db.conn.execute(
            "SELECT * FROM user_notes WHERE paper_id = ? ORDER BY created_at DESC",
            (paper_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# Layer 4: Meta-Memory (SQLite — 策略优化)
# ═══════════════════════════════════════════════════════════════


class MetaMemory:
    """元记忆 — 策略有效性与用户偏好学习.

    记录什么策略在什么场景下有效, 驱动 Agent 自我改进.
    """

    def __init__(self, db):
        self._db = db
        self._ensure_table()

    def _ensure_table(self):
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                parameters TEXT,
                effectiveness REAL DEFAULT 0.5,
                outcome TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS error_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL,
                context TEXT,
                resolution TEXT,
                recurrence_count INTEGER DEFAULT 1,
                last_seen TEXT NOT NULL
            )
        """)
        self._db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 1,
                updated_at TEXT NOT NULL
            )
        """)
        self._db.conn.commit()

    def record_strategy(self, task_type: str, strategy_name: str,
                        parameters: dict = None, effectiveness: float = 0.5,
                        outcome: str = ""):
        """记录一次策略执行."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._db.conn.execute(
            "INSERT INTO strategy_log (task_type, strategy_name, parameters, "
            "effectiveness, outcome, created_at) VALUES (?,?,?,?,?,?)",
            (task_type, strategy_name,
             json.dumps(parameters, ensure_ascii=False) if parameters else None,
             effectiveness, outcome, now),
        )
        self._db.conn.commit()

    def get_best_strategy(self, task_type: str, min_samples: int = 3) -> Optional[dict]:
        """获取某类任务的最佳策略."""
        row = self._db.conn.execute(
            """SELECT strategy_name, parameters, AVG(effectiveness) as avg_eff, COUNT(*) as cnt
               FROM strategy_log
               WHERE task_type = ? AND cnt >= ?
               GROUP BY strategy_name
               ORDER BY avg_eff DESC LIMIT 1""",
            (task_type, min_samples),
        ).fetchone()
        if row is None:
            return None
        rd = dict(row)
        return {
            "strategy": rd["strategy_name"],
            "parameters": json.loads(rd.get("parameters", "{}")) if rd.get("parameters") else {},
            "avg_effectiveness": rd["avg_eff"],
            "sample_count": rd["cnt"],
        }

    def record_error(self, error_type: str, context: str = "", resolution: str = ""):
        """记录错误模式."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 检查是否已存在
        existing = self._db.conn.execute(
            "SELECT id, recurrence_count FROM error_patterns WHERE error_type = ? AND context = ?",
            (error_type, context),
        ).fetchone()
        if existing:
            self._db.conn.execute(
                "UPDATE error_patterns SET recurrence_count = ?, last_seen = ?, resolution = ? WHERE id = ?",
                (existing["recurrence_count"] + 1, now, resolution or "", existing["id"]),
            )
        else:
            self._db.conn.execute(
                "INSERT INTO error_patterns (error_type, context, resolution, last_seen) VALUES (?,?,?,?)",
                (error_type, context, resolution, now),
            )
        self._db.conn.commit()

    def get_common_errors(self, limit: int = 10) -> list[dict]:
        """获取常见错误模式."""
        rows = self._db.conn.execute(
            "SELECT * FROM error_patterns ORDER BY recurrence_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def learn_preference(self, key: str, value: Any, confidence: float = 0.5):
        """学习用户偏好."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value

        # 增量更新
        existing = self._db.conn.execute(
            "SELECT confidence, evidence_count FROM user_preferences WHERE key = ? AND value = ?",
            (key, val_str),
        ).fetchone()

        if existing:
            new_count = existing["evidence_count"] + 1
            new_conf = (existing["confidence"] * existing["evidence_count"] + confidence) / new_count
            self._db.conn.execute(
                "UPDATE user_preferences SET confidence = ?, evidence_count = ?, updated_at = ? "
                "WHERE key = ? AND value = ?",
                (new_conf, new_count, now, key, val_str),
            )
        else:
            self._db.conn.execute(
                "INSERT OR REPLACE INTO user_preferences (key, value, confidence, evidence_count, updated_at) "
                "VALUES (?,?,?,?,?)",
                (key, val_str, confidence, 1, now),
            )
        self._db.conn.commit()

    def get_preference(self, key: str, min_confidence: float = 0.3) -> Optional[Any]:
        """获取用户偏好 (需要达到一定置信度)."""
        row = self._db.conn.execute(
            "SELECT value, confidence FROM user_preferences WHERE key = ? AND confidence >= ? "
            "ORDER BY confidence DESC LIMIT 1",
            (key, min_confidence),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]


# ═══════════════════════════════════════════════════════════════
# Memory Manager (4层统一接口)
# ═══════════════════════════════════════════════════════════════


class MemoryManager:
    """4层记忆系统管理器 — 统一接口.

    组合短/中/长/元四层记忆, 为 Agent Engine 提供统一入口.
    """

    def __init__(self, db, chroma_store=None):
        self.short_term = ShortTermMemory()
        self.mid_term = MidTermMemory(db)
        self.long_term = LongTermMemory(db, chroma_store)
        self.meta = MetaMemory(db)

    def create_session(self, session_id: str = None) -> str:
        """创建新会话, 清空短期记忆."""
        import uuid
        sid = session_id or str(uuid.uuid4())[:8]
        self.short_term.clear()
        return sid

    def get_full_context(self, task_id: str = None) -> dict:
        """为 LLM 获取完整的上下文 (短+中+元)."""
        context = {
            "conversation": self.short_term.get_context(),
        }
        if task_id:
            checkpoint = self.mid_term.load_checkpoint(task_id)
            if checkpoint:
                context["checkpoint"] = {
                    "step_index": checkpoint.step_index,
                    "step_name": checkpoint.step_name,
                    "state": checkpoint.state,
                    "results_summary": _summarize(checkpoint.results, max_len=200),
                }

        # 添加偏好
        sources_pref = self.meta.get_preference("preferred_sources")
        if sources_pref:
            context["user_preferences"] = {"preferred_sources": sources_pref}

        return context


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_id(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _summarize(data: dict, max_len: int = 200) -> str:
    s = json.dumps(data, ensure_ascii=False, default=str)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."
