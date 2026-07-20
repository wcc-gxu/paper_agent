"""AgentState — 统一 Agent 状态管理 + LifecycleLogger。

Supervisor 是 Agent 状态的唯一写入者。所有状态变更经过本模块，
确保 Redis + DB + Lifecycle 日志三方一致。

Redis 存储:
  agent:state:{agent_id}  → Hash (完整状态)
  agent:active            → SET  (所有非 stopped 的 agent_id)

用法:
    from .agent_state import AgentStateManager, LifecycleLogger

    mgr = AgentStateManager(redis, db)
    state = await mgr.get_or_create(agent_id, user_id)
    await mgr.transition(agent_id, "starting")
    ...
    await mgr.transition(agent_id, "idle", pid=12345)
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATA_DIR = Path(os.getenv("PAPER_SEARCH_BASE_DIR", str(Path.home()))) / ".paper_search"
LIFECYCLE_LOG_PATH = DATA_DIR / "logs" / "agent_lifecycle.jsonl"

# ── 状态枚举 ────────────────────────────────────────────────

VALID_STATES = frozenset({
    "pending", "starting", "idle", "busy",
    "stopping", "stopped", "crashed", "stalled",
})

TERMINAL_STATES = frozenset({"stopped", "crashed"})
ACTIVE_STATES = VALID_STATES - TERMINAL_STATES - {"pending"}

# ── 默认值（参考 agent-manifest.md §2.3 + config.py）─────

DEFAULT_AGENT_STATE: dict[str, Any] = {
    "agent_type": "main",
    "display_name": "我的科研助理",
    "system_prompt": "",
    "llm_provider": os.getenv("LLM_PROVIDER", "deepseek"),
    "llm_model": os.getenv("LLM_MODEL", "deepseek-v4-pro"),
    "checkpoint_backend": "",
    "session_default": "main",
    "iteration_limit": 8,
    "user_timeout_seconds": 1800,
    "message_window_trim_max_tokens": 8000,
    "data_dir": str(DATA_DIR),
    "user_preferences": "{}",
    "state": "pending",
    "pid": "0",
    "current_node": "",
    "active_turns": "0",
    "current_session_id": "",
    "started_at": "",
    "last_active_at": "",
    "last_error": "",
    "exit_code": "0",
    "restart_count": "0",
    "created_at": "",
    "updated_at": "",
    "extra": "{}",
}


def _now_ts() -> float:
    return _time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# LifecycleLogger
# ═══════════════════════════════════════════════════════════════


class LifecycleLogger:
    """统一的结构化 Agent 生命周期日志。

    写入 ~/.paper_search/logs/agent_lifecycle.jsonl（每行一条 JSON）。
    """

    def __init__(self, log_path: Optional[Path] = None):
        self._path = Path(log_path or LIFECYCLE_LOG_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, agent_id: str, user_id: str = "",
            detail: Optional[dict] = None) -> None:
        """写入一条生命周期事件。

        Args:
            event: 事件类型 (e.g. "agent.launch.start", "agent.crash")
            agent_id: Agent ID
            user_id: 用户 ID
            detail: 附加信息 (pid, error, from_state, to_state, ...)
        """
        record = {
            "ts": _now_iso(),
            "event": event,
            "agent_id": agent_id,
            "user_id": user_id,
            "detail": detail or {},
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("Failed to write lifecycle log: %s", event, exc_info=True)

    def create(self, agent_id: str, user_id: str, auto_created: bool = False):
        self.log("agent.create", agent_id, user_id, {"auto_created": auto_created})

    def launch_start(self, agent_id: str, user_id: str):
        self.log("agent.launch.start", agent_id, user_id)

    def launch_success(self, agent_id: str, user_id: str, pid: int, startup_ms: float = 0):
        self.log("agent.launch.success", agent_id, user_id,
                 {"pid": pid, "startup_ms": int(startup_ms)})

    def launch_failed(self, agent_id: str, user_id: str, error: str):
        self.log("agent.launch.failed", agent_id, user_id, {"error": error})

    def launch_timeout(self, agent_id: str, user_id: str, timeout_s: float):
        self.log("agent.launch.timeout", agent_id, user_id, {"timeout_s": timeout_s})

    def state_transition(self, agent_id: str, from_state: str, to_state: str,
                         **extra):
        self.log("agent.state.transition", agent_id, "",
                 {"from": from_state, "to": to_state, **extra})

    def crash(self, agent_id: str, user_id: str, exit_code: int, node: str = "",
              last_error: str = ""):
        self.log("agent.crash", agent_id, user_id,
                 {"exit_code": exit_code, "node": node, "last_error": last_error})

    def auto_restart(self, agent_id: str, user_id: str, restart_count: int):
        self.log("agent.auto_restart", agent_id, user_id,
                 {"restart_count": restart_count})

    def stop_requested(self, agent_id: str, user_id: str, method: str = "api"):
        self.log("agent.stop.requested", agent_id, user_id, {"method": method})

    def stop_success(self, agent_id: str, user_id: str, uptime_s: float = 0):
        self.log("agent.stop.success", agent_id, user_id, {"uptime_seconds": int(uptime_s)})

    def stop_forced(self, agent_id: str, user_id: str, reason: str = ""):
        self.log("agent.stop.forced", agent_id, user_id, {"reason": reason})

    def health_warning(self, agent_id: str, detail: str):
        self.log("agent.health.warning", agent_id, "", {"detail": detail})

    def health_timeout(self, agent_id: str, node: str, age_s: float):
        self.log("agent.health.timeout", agent_id, "",
                 {"node": node, "age_seconds": int(age_s)})


# ═══════════════════════════════════════════════════════════════
# AgentStateManager
# ═══════════════════════════════════════════════════════════════


@dataclass
class AgentState:
    """Agent 完整状态（Redis Hash 的 Python 表示）。"""

    agent_id: str = ""
    user_id: str = ""

    # ── 配置字段 ──
    agent_type: str = "main"
    display_name: str = "我的科研助理"
    system_prompt: str = ""
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-pro"
    checkpoint_backend: str = ""
    session_default: str = "main"
    iteration_limit: int = 8
    user_timeout_seconds: int = 1800
    message_window_trim_max_tokens: int = 8000
    data_dir: str = ""
    user_preferences: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    # ── 运行时状态 ──
    state: str = "pending"
    pid: int = 0
    current_node: str = ""
    active_turns: int = 0
    current_session_id: str = ""
    started_at: str = ""
    last_active_at: str = ""
    last_error: str = ""
    exit_code: int = 0
    restart_count: int = 0

    # ── 时间戳 ──
    created_at: str = ""
    updated_at: str = ""

    def to_redis_hash(self) -> dict[str, str]:
        """转为 Redis Hash 字段（全部 string）。"""
        data = {
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "agent_type": self.agent_type,
            "display_name": self.display_name,
            "system_prompt": self.system_prompt,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "checkpoint_backend": self.checkpoint_backend,
            "session_default": self.session_default,
            "iteration_limit": str(self.iteration_limit),
            "user_timeout_seconds": str(self.user_timeout_seconds),
            "message_window_trim_max_tokens": str(self.message_window_trim_max_tokens),
            "data_dir": self.data_dir or str(DATA_DIR),
            "user_preferences": json.dumps(self.user_preferences, ensure_ascii=False),
            "state": self.state,
            "pid": str(self.pid),
            "current_node": str(self.current_node) if self.current_node else "",
            "active_turns": str(self.active_turns),
            "current_session_id": self.current_session_id or "",
            "started_at": str(self.started_at) if self.started_at else "",
            "last_active_at": str(self.last_active_at) if self.last_active_at else "",
            "last_error": self.last_error or "",
            "exit_code": str(self.exit_code),
            "restart_count": str(self.restart_count),
            "created_at": str(self.created_at) if self.created_at else "",
            "updated_at": str(self.updated_at) if self.updated_at else "",
            "extra": json.dumps(self.extra, ensure_ascii=False),
        }
        return data

    @classmethod
    def from_redis_hash(cls, data: dict[str, str],
                        agent_id: str = "") -> "AgentState":
        """从 Redis Hash 还原。"""
        def _int(v: str, default: int = 0) -> int:
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        def _json(v: str, default: Any = None) -> Any:
            if default is None:
                default = {}
            try:
                return json.loads(v) if v else default
            except (json.JSONDecodeError, TypeError):
                return default

        return cls(
            agent_id=data.get("agent_id", agent_id),
            user_id=data.get("user_id", ""),
            agent_type=data.get("agent_type", "main"),
            display_name=data.get("display_name", "我的科研助理"),
            system_prompt=data.get("system_prompt", ""),
            llm_provider=data.get("llm_provider", "deepseek"),
            llm_model=data.get("llm_model", "deepseek-v4-pro"),
            checkpoint_backend=data.get("checkpoint_backend", ""),
            session_default=data.get("session_default", "main"),
            iteration_limit=_int(data.get("iteration_limit", "8"), 8),
            user_timeout_seconds=_int(data.get("user_timeout_seconds", "1800"), 1800),
            message_window_trim_max_tokens=_int(data.get("message_window_trim_max_tokens", "8000"), 8000),
            data_dir=data.get("data_dir", str(DATA_DIR)),
            user_preferences=_json(data.get("user_preferences", "{}")),
            state=data.get("state", "pending"),
            pid=_int(data.get("pid", "0")),
            current_node=data.get("current_node", ""),
            active_turns=_int(data.get("active_turns", "0")),
            current_session_id=data.get("current_session_id", ""),
            started_at=data.get("started_at", ""),
            last_active_at=data.get("last_active_at", ""),
            last_error=data.get("last_error", ""),
            exit_code=_int(data.get("exit_code", "0")),
            restart_count=_int(data.get("restart_count", "0")),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            extra=_json(data.get("extra", "{}")),
        )

    def to_dict(self) -> dict:
        """转为 JSON-safe dict（供 API 返回）。"""
        d = asdict(self)
        d["iteration_limit"] = self.iteration_limit
        d["user_timeout_seconds"] = self.user_timeout_seconds
        d["message_window_trim_max_tokens"] = self.message_window_trim_max_tokens
        return d


class AgentStateManager:
    """Agent 状态管理器 — Supervisor 的唯一状态写入入口。

    所有状态变更通过此类完成，确保:
      1. Redis agent:state:{agent_id} 更新
      2. agent:active SET 同步
      3. LifecycleLogger 记录
      4. DB 异步同步
    """

    def __init__(self, redis: Any, db: Any = None,
                 lifecycle: Optional[LifecycleLogger] = None):
        self._redis = redis
        self._db = db
        self._lifecycle = lifecycle or LifecycleLogger()
        self._state_cache: dict[str, AgentState] = {}

    # ── Redis key helpers ──────────────────────────────────

    @staticmethod
    def _state_key(agent_id: str) -> str:
        return f"agent:state:{agent_id}"

    @staticmethod
    def _active_key() -> str:
        return "agent:active"

    # ── Read ───────────────────────────────────────────────

    async def get(self, agent_id: str) -> Optional[AgentState]:
        """从 Redis 读取 Agent 状态。"""
        if agent_id in self._state_cache:
            return self._state_cache[agent_id]
        try:
            data = await self._redis.hgetall(self._state_key(agent_id))
            if not data:
                return None
            state = AgentState.from_redis_hash(data, agent_id=agent_id)
            self._state_cache[agent_id] = state
            return state
        except Exception as e:
            logger.error("Failed to get agent state for %s: %s", agent_id, e)
            return None

    async def get_or_create(self, agent_id: str, user_id: str) -> AgentState:
        """获取 Agent 状态，不存在则自动创建。

        查找顺序:
          1. Redis agent:state:{agent_id}
          2. DB agents 表 (按 agent_id)
          3. DB agents 表 (按 user_id 取默认)
          4. 都没有 → 自动创建（DB + Redis）
        """
        # 1. Redis
        state = await self.get(agent_id)
        if state:
            return state

        # 2. DB (by agent_id)
        if self._db:
            row = self._db._fetchone(
                "SELECT * FROM agents WHERE id = %s", (agent_id,))
            if row:
                state = await self._hydrate_from_db(agent_id, user_id, row)
                return state

            # 3. DB (by user_id — reuse existing agent)
            default = self._db.get_default_agent(user_id)
            if default:
                agent_id = default["id"]
                state = await self._hydrate_from_db(agent_id, user_id, default)
                return state

        # 4. Auto-create
        return await self._auto_create(agent_id, user_id)

    async def list_active(self) -> list[str]:
        """返回所有非 stopped 的 agent_id 列表。"""
        try:
            members = await self._redis.smembers(self._active_key())
            return list(members)
        except Exception:
            return []

    # ── Write (state transitions) ──────────────────────────

    async def _set_state(self, agent_id: str, state: AgentState):
        """写入完整状态到 Redis Hash。"""
        data = state.to_redis_hash()
        key = self._state_key(agent_id)
        try:
            await self._redis.hset(key, mapping=data)
            self._state_cache[agent_id] = state

            # 维护 active SET
            if state.state in TERMINAL_STATES:
                await self._redis.srem(self._active_key(), agent_id)
            else:
                await self._redis.sadd(self._active_key(), agent_id)
        except Exception as e:
            logger.error("Failed to set agent state for %s: %s", agent_id, e)

    async def update(self, agent_id: str, **fields) -> Optional[AgentState]:
        """部分更新 agent 状态。"""
        state = await self.get(agent_id)
        if not state:
            return None

        for k, v in fields.items():
            if hasattr(state, k):
                setattr(state, k, v)

        state.updated_at = _now_iso()
        await self._set_state(agent_id, state)
        return state

    async def transition(self, agent_id: str, to_state: str,
                         **extra) -> Optional[AgentState]:
        """状态转移（带 lifecycle 日志记录）。

        Args:
            agent_id: Agent ID
            to_state: 目标状态
            **extra: 附加字段更新 (pid, last_error, exit_code, node, ...)
        """
        if to_state not in VALID_STATES:
            logger.error("Invalid state transition to %s for %s", to_state, agent_id)
            return None

        state = await self.get(agent_id)
        if not state:
            logger.warning("Cannot transition %s: state not found", agent_id)
            return None

        from_state = state.state
        if from_state == to_state and not extra:
            return state

        state.state = to_state
        state.updated_at = _now_iso()

        for k, v in extra.items():
            if hasattr(state, k):
                setattr(state, k, v)

        if to_state == "idle" and "started_at" in extra:
            state.started_at = extra.get("started_at", _now_iso())

        # 维护进程退出时的字段
        if to_state in TERMINAL_STATES:
            state.current_node = ""
            state.active_turns = 0
            state.pid = extra.get("pid", state.pid)

        await self._set_state(agent_id, state)

        # Lifecycle 日志
        self._lifecycle.state_transition(agent_id, from_state, to_state, **extra)

        # 异步写 DB
        self._sync_to_db_background(agent_id, state)

        return state

    async def delete(self, agent_id: str):
        """删除 agent 状态（仅用于清理）。"""
        try:
            await self._redis.delete(self._state_key(agent_id))
            await self._redis.srem(self._active_key(), agent_id)
            self._state_cache.pop(agent_id, None)
        except Exception as e:
            logger.error("Failed to delete agent state for %s: %s", agent_id, e)

    # ── Internal ───────────────────────────────────────────

    async def _hydrate_from_db(self, agent_id: str, user_id: str,
                                row: dict) -> AgentState:
        """从 DB 行重建 Redis 状态。"""
        now = _now_iso()

        def _to_str(v):
            """DB 时间列可能返回 datetime，统一转 ISO 字符串。"""
            if v is None:
                return now
            if isinstance(v, str):
                return v
            if hasattr(v, 'isoformat'):
                return v.isoformat()
            return str(v)

        state = AgentState(
            agent_id=row.get("id", agent_id),
            user_id=row.get("user_id", user_id),
            system_prompt=row.get("system_prompt", ""),
            llm_provider=row.get("llm_provider", "deepseek"),
            state=row.get("state", "pending"),
            created_at=_to_str(row.get("created_at")),
            updated_at=_to_str(row.get("updated_at")),
        )

        # 从 DB 加载用户偏好
        user_prefs_raw = row.get("user_preferences", "{}")
        if isinstance(user_prefs_raw, dict):
            state.user_preferences = user_prefs_raw
        elif isinstance(user_prefs_raw, str) and user_prefs_raw:
            try:
                state.user_preferences = json.loads(user_prefs_raw)
            except json.JSONDecodeError:
                pass

        extra_raw = row.get("extra", "{}")
        if isinstance(extra_raw, dict):
            state.extra = extra_raw
        elif isinstance(extra_raw, str) and extra_raw:
            try:
                state.extra = json.loads(extra_raw)
            except json.JSONDecodeError:
                pass

        await self._set_state(agent_id, state)
        logger.info("Agent state hydrated from DB: %s", agent_id)
        return state

    async def _auto_create(self, agent_id: str, user_id: str) -> AgentState:
        """自动创建新 Agent（Redis + DB）。"""
        now = _now_iso()

        # 从 DB user_preferences 表加载偏好
        user_prefs = {}
        if self._db:
            try:
                prefs_row = self._db.get_v4_preferences(user_id)
                if prefs_row:
                    user_prefs = {
                        "research_domain": prefs_row.get("research_domain", ""),
                        "writing_style": prefs_row.get("writing_style", "APA"),
                        "language_pref": prefs_row.get("language_pref", "zh"),
                        "mentor_quotes": prefs_row.get("mentor_quotes", ""),
                        "other": prefs_row.get("other", {}),
                    }
            except Exception:
                pass

        state = AgentState(
            agent_id=agent_id,
            user_id=user_id,
            agent_type="main",
            display_name="我的科研助理",
            system_prompt="",
            llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-v4-pro"),
            checkpoint_backend="",
            session_default="main",
            iteration_limit=8,
            user_timeout_seconds=1800,
            message_window_trim_max_tokens=8000,
            data_dir=str(DATA_DIR),
            user_preferences=user_prefs,
            state="pending",
            created_at=now,
            updated_at=now,
        )

        # 写入 DB
        if self._db:
            try:
                self._db._execute(
                    """INSERT INTO agents (id, user_id, system_prompt, llm_provider,
                       state, user_preferences, extra, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'pending', %s::jsonb, %s::jsonb,
                               %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (agent_id, user_id, "", state.llm_provider,
                     json.dumps(user_prefs, ensure_ascii=False),
                     json.dumps({}, ensure_ascii=False),
                     now, now),
                )
            except Exception as e:
                logger.warning("Failed to auto-create agent in DB: %s (continuing in Redis only)", e)

        # 写入 Redis
        await self._set_state(agent_id, state)

        # Lifecycle 日志
        self._lifecycle.create(agent_id, user_id, auto_created=True)
        logger.info("Agent auto-created: %s (user=%s)", agent_id, user_id)

        return state

    def _sync_to_db_background(self, agent_id: str, state: AgentState):
        """异步同步状态到 DB（fire-and-forget，失败不阻塞）。"""
        if not self._db:
            return

        async def _do_sync():
            for attempt in range(3):
                try:
                    self._db.update_agent(
                        agent_id,
                        user_id=state.user_id,
                        state=state.state,
                        system_prompt=state.system_prompt,
                        llm_provider=state.llm_provider,
                    )
                    return
                except Exception as e:
                    if attempt < 2:
                        await _asyncio_sleep(0.5)
                    else:
                        logger.warning(
                            "Failed to sync agent state to DB after 3 attempts: %s (agent=%s)",
                            e, agent_id)

        import asyncio
        try:
            asyncio.create_task(_do_sync())
        except RuntimeError:
            pass


async def _asyncio_sleep(s: float):
    import asyncio
    await asyncio.sleep(s)
