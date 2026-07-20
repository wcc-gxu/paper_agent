"""Agent Supervisor — 管理所有用户 Agent 子进程。

v4.2: 统一状态管理 + 双向 Pub/Sub 控制 + 结构化生命周期日志。
  - AgentStateManager: Redis agent:state:{agent_id} Hash + agent:active SET
  - LifecycleLogger: ~/.paper_search/logs/agent_lifecycle.jsonl
  - 控制协议: API PUBLISH agent:control → Supervisor 处理 → PUBLISH agent:control:resp:{corr_id}
  - 自动创建: Redis 无状态 → DB 查 → DB 也无 → 自动创建
  - Supervisor 是 Agent 状态的唯一写入者

  - 消息路由: BRPOP agent:ws:{uid} → stdin → Agent 子进程
  - 出站拦截: Agent stdout → outbox → API
  - 3 层健康检测: poll() / busy timeout / stdout silent
  - Agent 子进程不直接连接 Redis

使用方式:
    python -m paper_search.agent.daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import redis.asyncio as aioredis
import psycopg2

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Node timeout overrides for busy detection (seconds)
NODE_TIMEOUTS = {
    "intent_classify": 120,
    "plan": 120,
    "execute": 300,
    "clarify": 300,
    "gate": 600,
    "ask_user": 600,
    "evaluate": 120,
    "inline_reply": 60,
}
DEFAULT_BUSY_TIMEOUT = 300
QUEUE_STALE_SECONDS = 43200
STDOUT_SILENT_SECONDS = 15
WORKER_READY_TIMEOUT = 120  # 等待 worker 上报 idle 的最大秒数（bootstrap 可能需要 >15s）
MAX_RESTART_COUNT = 3       # 最多自动重启次数
RESTART_BACKOFF_BASE = 1.0  # 重启退避基础秒数，指数增长: 1s → 2s → 4s


def _now_ts() -> float:
    return _time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# AgentSupervisor
# ═══════════════════════════════════════════════════════════════


class AgentSupervisor:
    """管理所有用户 Agent 子进程的生命周期、消息路由、状态监控。

    每个用户一个独立的 Python 子进程，通过 stdin/stdout pipe 通信。
    Agent 子进程不直接连接 Redis。
    Supervisor 是 Agent 状态的唯一写入者。
    """

    def __init__(self, redis_url: str = REDIS_URL):
        self.redis_url = redis_url
        self.redis: Any = None
        self._agents: dict[str, asyncio.subprocess.Process] = {}
        self._agent_started: dict[str, float] = {}
        self._status_cache: dict[str, dict] = {}
        self._pid_to_uid: dict[int, str] = {}
        self._stopping = False
        self._data_dir: Optional[Path] = None

        # v4.2: 就绪事件 (agent_id → Event)
        self._ready_events: dict[str, asyncio.Event] = {}

        # v4.2: 防重入 — 正在自动重启中的 agent
        self._relaunching: set[str] = set()

        # v4.3: user_id → agent_id 映射（解决 stdout reader 拼错 agent_id 的问题）
        self._uid_to_agent_id: dict[str, str] = {}

        # v4.2: State manager + lifecycle logger (延迟初始化)
        self._state_mgr: Any = None
        self._lifecycle: Any = None
        self._db: Any = None

    # ── Bootstrap ──────────────────────────────────────────

    async def bootstrap(self, data_dir: Optional[Path] = None):
        """初始化日志 / DB / StateManager / LifecycleLogger。"""
        self._data_dir = data_dir
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        os.environ.setdefault("DEBUG_PROTOCOL", "1")

        # 持久化日志到文件（容器重建不丢失）
        from ..logging_setup import setup_file_logging
        setup_file_logging("supervisor")

        from .agent_state import AgentStateManager, LifecycleLogger
        from .pgdb import PostgresAgentDB

        self._db = PostgresAgentDB()
        self._lifecycle = LifecycleLogger()
        self._state_mgr = AgentStateManager(
            redis=None, db=self._db, lifecycle=self._lifecycle,
        )
        logger.info("AgentSupervisor bootstrapped")

    # ── Launch / Stop ──────────────────────────────────────

    async def launch_agent(self, user_id: str) -> bool:
        """启动一个用户 Agent 子进程（内部方法，不处理状态转移）。

        Returns:
            True 如果成功启动子进程，False 如果已在运行或启动失败。
        """
        agent_id = f"agent-{user_id}"

        if user_id in self._agents:
            proc = self._agents[user_id]
            if proc.returncode is None:
                logger.info("Agent %s already running (pid=%d)", user_id, proc.pid)
                return False
            logger.info("Agent %s replacing dead process (pid=%d, rc=%s)", user_id, proc.pid, proc.returncode)
            if proc.pid:
                self._pid_to_uid.pop(proc.pid, None)
            self._agents.pop(user_id, None)

        cmd = [
            sys.executable, "-m", "paper_search.agent.agent_worker",
            "--user-id", user_id,
        ]
        logger.info("Launching agent: user=%s cmd=%s", user_id, " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error("Failed to launch agent %s: %s", user_id, e)
            return False

        self._agents[user_id] = proc
        self._agent_started[user_id] = _now_ts()
        if proc.pid:
            self._pid_to_uid[proc.pid] = user_id

        # 存储 user_id → agent_id 映射（用于 stdout reader 等需要正确 agent_id 的地方）
        agent = self._db.get_default_agent(user_id) if self._db else None
        self._uid_to_agent_id[user_id] = agent["id"] if agent else agent_id

        # 创建就绪事件
        self._ready_events[agent_id] = asyncio.Event()

        # 启动 stdout/stderr 读取
        asyncio.create_task(self._read_agent_stdout(user_id, proc))
        asyncio.create_task(self._read_agent_stderr(user_id, proc))

        logger.info("Agent %s launched: pid=%d", user_id, proc.pid)
        return True

    async def wait_agent_ready(self, agent_id: str,
                                timeout: float = WORKER_READY_TIMEOUT) -> bool:
        """等待 agent worker 上报 idle 状态。"""
        event = self._ready_events.get(agent_id)
        if not event:
            logger.warning("Agent %s ready event not found (launch not completed?)", agent_id)
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Agent %s ready timeout after %.0fs", agent_id, timeout)
            return False

    async def stop_agent(self, user_id: str, graceful: bool = True) -> bool:
        """停止一个用户 Agent。"""
        proc = self._agents.get(user_id)
        if proc is None:
            logger.info("Agent %s stop skipped: not in _agents", user_id)
            return False
        if proc.returncode is not None:
            logger.info("Agent %s stop skipped: already exited (rc=%s)", user_id, proc.returncode)
            self._agent_started.pop(user_id, None)
            return False

        logger.info("Stopping agent %s (graceful=%s)", user_id, graceful)
        if graceful:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
                logger.info("Agent %s exited gracefully (rc=%s)", user_id, proc.returncode)
            except asyncio.TimeoutError:
                logger.warning("Agent %s did not exit gracefully, force killing", user_id)
                proc.kill()
        else:
            proc.kill()
            logger.info("Agent %s killed", user_id)
        self._agent_started.pop(user_id, None)

        return True

    async def _relaunch_agent(self, user_id: str):
        """Agent 崩溃后自动重启。指数退避 + 最大次数限制。"""
        agent_id = f"agent-{user_id}"

        # 防重入：已经在重启中则跳过
        if user_id in self._relaunching:
            logger.warning("Agent %s already restarting, skipping duplicate call", user_id)
            return
        self._relaunching.add(user_id)

        try:
            state = await self._state_mgr.get(agent_id)
            rc = state.restart_count + 1 if state else 1

            if rc > MAX_RESTART_COUNT:
                logger.error("Agent %s exceeded max restart count (%d), stopping", agent_id, MAX_RESTART_COUNT)
                await self._state_mgr.transition(agent_id, "crashed",
                    last_error=f"exceeded max restart count {MAX_RESTART_COUNT}")
                self._lifecycle.crash(agent_id, user_id, -1)
                return

            delay = RESTART_BACKOFF_BASE * (2 ** (rc - 1))
            self._lifecycle.auto_restart(agent_id, user_id, rc)
            logger.info("Auto-restarting agent %s (attempt #%d/%d, delay=%.1fs)",
                        agent_id, rc, MAX_RESTART_COUNT, delay)

            await self._state_mgr.update(agent_id, restart_count=rc)
            await asyncio.sleep(delay)

            # 先 transition 再 launch（与 _handle_start 一致，避免 race）
            await self._state_mgr.transition(agent_id, "starting")
            launched = await self.launch_agent(user_id)

            if not launched:
                await self._state_mgr.transition(
                    agent_id, "crashed",
                    last_error="Failed to spawn subprocess during restart")
                self._lifecycle.launch_failed(agent_id, user_id, "Failed to spawn during restart")
                logger.error("Agent %s relaunch failed: spawn error", agent_id)
                return

            # 等待 worker 就绪
            ready = await self.wait_agent_ready(agent_id, timeout=WORKER_READY_TIMEOUT // 2)
            if ready:
                state = await self._state_mgr.get(agent_id)
                if state and state.state == "idle":
                    logger.info("Agent %s auto-restarted successfully (attempt #%d)", agent_id, rc)
                else:
                    error = state.last_error if state else "Agent exited during bootstrap"
                    await self._state_mgr.transition(agent_id, "crashed",
                        last_error=f"Auto-restart failed: {error}")
                    self._lifecycle.launch_failed(agent_id, user_id, error)
                    logger.error("Agent %s relaunch failed after bootstrap: %s", agent_id, error)
            else:
                await self._state_mgr.transition(agent_id, "crashed",
                    last_error=f"Auto-restart timeout after {WORKER_READY_TIMEOUT // 2}s")
                self._lifecycle.launch_timeout(agent_id, user_id, WORKER_READY_TIMEOUT // 2)
                logger.error("Agent %s relaunch timed out", agent_id)
        except Exception as e:
            logger.error("Agent %s relaunch error: %s", agent_id, e, exc_info=True)
        finally:
            self._relaunching.discard(user_id)

    # ── Message Ingress (BRPOP → stdin) ────────────────────

    async def _ingress_loop(self):
        """BRPOP 所有用户队列 → route to agent subprocess stdin。"""
        logger.info("Ingress loop started")

        while not self._stopping:
            if not self._agents:
                await asyncio.sleep(1)
                continue

            queues = [f"agent:ws:{uid}" for uid in self._agents
                      if self._agents[uid].returncode is None]

            if not queues:
                await asyncio.sleep(0.5)
                continue

            try:
                result = await self.redis.brpop(queues, timeout=1)
            except Exception as e:
                logger.error("Ingress BRPOP error: %s", e)
                await asyncio.sleep(1)
                continue

            if result is None:
                continue

            queue_name = result[0]
            raw_msg = result[1]
            uid = queue_name.replace("agent:ws:", "")

            proc = self._agents.get(uid)
            if proc is None or proc.returncode is not None:
                try:
                    await self.redis.rpush(f"agent:ws:{uid}:parked", raw_msg)
                except Exception as e:
                    logger.warning("Parked message RPUSH failed for %s: %s", uid, e)
                continue

            try:
                proc.stdin.write((raw_msg + "\n").encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                logger.warning("Agent %s stdin broken: %s", uid, e)
                await self._handle_exit(uid, proc)

    # ── Message Egress (stdout → outbox) ───────────────────

    async def _read_agent_stdout(self, uid: str, proc: asyncio.subprocess.Process):
        """持续读取 Agent 子进程 stdout，解析 JSON → 路由到 Redis outbox + 状态更新。"""
        logger.info("Egress reader started for %s (pid=%d)", uid, proc.pid)
        # 使用正确的 DB agent_id，不是 f"agent-{uid}"
        agent_id = self._uid_to_agent_id.get(uid, f"agent-{uid}")

        try:
            while proc.returncode is None:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                except asyncio.TimeoutError:
                    continue

                if not line:
                    break

                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("Agent %s stdout non-JSON: %s", uid, line[:200])
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "state":
                    node = msg.get("node", "")
                    state = msg.get("state", "idle")
                    active = msg.get("active_turns", 0)
                    pid = proc.pid or 0

                    self._status_cache[uid] = {
                        "state": state, "node": node,
                        "active_turns": active, "pid": pid,
                        "updated_at": _now_ts(),
                    }

                    # v4.2: 状态转移 + 就绪事件
                    if state == "idle":
                        logger.info("Agent %s state → idle (pid=%d node=%s turns=%d)", agent_id, pid, node, active)
                        current = await self._state_mgr.get(agent_id)
                        # 接受 starting/pending/busy（正常启动），以及 stopped/crashed（并发启动 + 防御）
                        if current and current.state in ("starting", "pending", "busy", "stopped", "crashed"):
                            startup_ms = msg.get("startup_ms", 0)
                            await self._state_mgr.transition(
                                agent_id, "idle",
                                pid=pid, current_node=node,
                                started_at=_now_iso(),
                            )
                            # Signal ready
                            evt = self._ready_events.get(agent_id)
                            if evt:
                                evt.set()

                        # Keep Redis hash in sync with runtime updates
                        await self._state_mgr.update(
                            agent_id,
                            pid=pid, current_node=node or "",
                            active_turns=active,
                            last_active_at=_now_iso(),
                            state="idle",
                        )

                    elif state == "busy":
                        logger.info("Agent %s state → busy (pid=%d node=%s turns=%d)", agent_id, pid, node, active)
                        await self._state_mgr.transition(
                            agent_id, "busy",
                            pid=pid, current_node=node,
                            active_turns=active,
                            last_active_at=_now_iso(),
                        )

                    elif state == "stopped":
                        logger.info("Agent %s state → stopped (pid=%d)", agent_id, pid)
                        await self._state_mgr.transition(
                            agent_id, "stopped",
                            pid=pid, current_node="",
                            active_turns=0,
                        )

                elif msg_type == "reply":
                    content = msg.get("content", "")
                    envelope = {
                        "type": "message",
                        "subType": "reply",
                        "agentId": agent_id,
                        "sessionId": msg.get("session_id", "main"),
                        "priority": "high",
                        "payload": {"content": content},
                        "timestamp": _now_iso(),
                    }
                    try:
                        await self.redis.lpush(f"outbox:{uid}",
                                               json.dumps(envelope, ensure_ascii=False))
                    except Exception as e:
                        logger.error("Failed to push outbox for %s (agent=%s): %s", uid, agent_id, e)

                elif msg_type in ("tool", "status", "tool_execution",
                                   "plan_todo_update", "error"):
                    try:
                        msg["agentId"] = msg.get("agentId", agent_id)
                        await self.redis.lpush(f"outbox:{uid}",
                                               json.dumps(msg, ensure_ascii=False))
                    except Exception as e:
                        logger.error("Failed to push outbox for %s (agent=%s): %s", uid, agent_id, e)

                elif msg_type in ("sync_ack", "sync_complete", "sync_request"):
                    logger.warning(
                        "Agent %s stdout: control message type=%s — "
                        "control messages must use SSE Pub/Sub, not stdout/outbox. Dropping.",
                        agent_id, msg_type,
                    )

        except Exception as e:
            logger.error("Agent %s stdout reader error: %s", uid, e)
        finally:
            logger.info("Agent %s stdout closed (pid=%d)", uid, proc.pid)
            await self._handle_exit(uid, proc)

    async def _read_agent_stderr(self, uid: str, proc: asyncio.subprocess.Process):
        """读取 Agent stderr → 记日志。"""
        try:
            while proc.returncode is None:
                try:
                    line = await asyncio.wait_for(proc.stderr.readline(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                logger.error("Agent %s stderr: %s", uid,
                             line.decode("utf-8", errors="replace").rstrip())
        except Exception as e:
            logger.error("Agent %s stderr reader crashed: %s", uid, e, exc_info=True)

    async def _handle_exit(self, uid: str, proc: asyncio.subprocess.Process):
        """处理 Agent 退出。幂等——重复调用不会重复转移状态或重启。"""
        agent_id = self._uid_to_agent_id.get(uid, f"agent-{uid}")

        # 已在清理中 → 跳过
        if uid not in self._agents or self._agents.get(uid) is not proc:
            logger.debug("Agent %s exit already handled or proc mismatch, skipping", uid)
            return

        logger.info("Agent %s handling exit (pid=%d)", uid, proc.pid)

        # 确保 returncode 已设置
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("Agent %s wait timeout, force killing", uid)
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    logger.error("Agent %s kill timeout — process may be stuck", uid)

        rc = proc.returncode
        to_state = "stopped" if rc == 0 else "crashed"
        await self._state_mgr.transition(
            agent_id, to_state,
            pid=proc.pid or 0, exit_code=rc,
            last_error=f"exit_code={rc}" if rc != 0 else "",
        )

        if to_state == "crashed":
            self._lifecycle.crash(agent_id, uid, rc)

        logger.info("Agent %s %s (pid=%d rc=%d)", uid, to_state,
                    self._agent_started.get(uid, 0), rc)

        # 清理所有追踪
        self._agents.pop(uid, None)
        self._agent_started.pop(uid, None)
        if proc.pid:
            self._pid_to_uid.pop(proc.pid, None)

        # Clean up ready event — trigger it so any waiter unblocks
        evt = self._ready_events.pop(agent_id, None)
        if evt:
            evt.set()

        # Re-push parked messages
        try:
            if self.redis:
                parked = await self.redis.lrange(f"agent:ws:{uid}:parked", 0, -1)
                for msg in parked:
                    await self.redis.lpush(f"agent:ws:{uid}", msg)
                await self.redis.delete(f"agent:ws:{uid}:parked")
        except Exception:
            pass

        # Auto restart on crash
        if to_state == "crashed" and not self._stopping:
            asyncio.create_task(self._relaunch_agent(uid))

    # ── Health Monitor (3 layers) ──────────────────────────

    async def _monitor_loop(self):
        """每 10s 检测所有 Agent 健康状态 + 每 60s 扫描新用户。"""
        await asyncio.sleep(5)
        logger.info("Health monitor started")
        _last_user_scan = 0.0
        while not self._stopping:
            # 每 60s 扫描新注册用户，自动创建 agent 并启动
            now = _now_ts()
            if now - _last_user_scan > 60:
                _last_user_scan = now
                try:
                    users = self._db.list_active_users()
                    for user in users:
                        user_id = user.get("id", "")
                        if not user_id:
                            continue
                        agent = self._db.get_default_agent(user_id)
                        if agent:
                            agent_id = agent["id"]
                            await self._state_mgr.get_or_create(agent_id, user_id)
                        else:
                            agent_id = self._db.create_agent(user_id=user_id)
                            await self._state_mgr.get_or_create(agent_id, user_id)
                            logger.info("Monitor: auto-created agent=%s for new user=%s", agent_id, user_id)
                        # 如果 agent 还没启动子进程，启动它
                        if user_id not in self._agents:
                            agent_id = agent.get("id", f"agent-{user_id}") if agent else f"agent-{user_id}"
                            await self._state_mgr.transition(agent_id, "starting")
                            launched = await self.launch_agent(user_id)
                            if launched:
                                logger.info("Monitor: auto-launched agent=%s for user=%s", agent_id, user_id)
                            else:
                                logger.warning("Monitor: failed to launch agent=%s", agent_id)
                except Exception as e:
                    logger.warning("Periodic user scan failed: %s", e)

            for uid, proc in list(self._agents.items()):
                agent_id = self._uid_to_agent_id.get(uid, f"agent-{uid}")
                rc = proc.returncode
                if rc is not None:
                    logger.info("Monitor: agent %s already exited (rc=%s), handling exit", uid, rc)
                    await self._handle_exit(uid, proc)
                    continue

                info = self._status_cache.get(uid, {})
                state = info.get("state", "starting")
                node = info.get("node", "")
                last_update = info.get("updated_at", 0)
                now = _now_ts()

                # Layer 2: Busy timeout (per node)
                if state == "busy" and last_update > 0:
                    timeout = NODE_TIMEOUTS.get(node, DEFAULT_BUSY_TIMEOUT)
                    if now - last_update > timeout:
                        if node in ("gate", "ask_user"):
                            self._lifecycle.health_warning(
                                agent_id,
                                f"stuck_waiting_user node={node} age={int(now - last_update)}s")
                            logger.warning("Agent %s stuck waiting for user: node=%s age=%.0fs",
                                          agent_id, node, now - last_update)
                        else:
                            self._lifecycle.health_timeout(
                                agent_id, node, now - last_update)
                            logger.error("Agent %s busy timeout: node=%s age=%.0fs → SIGTERM",
                                        agent_id, node, now - last_update)
                            proc.send_signal(signal.SIGTERM)
                            await self._state_mgr.transition(
                                agent_id, "crashed",
                                last_error=f"stuck_in_{node}")
                            await self._handle_exit(uid, proc)

                # Layer 2: Idle queue staleness
                if state == "idle":
                    try:
                        if self.redis:
                            qlen = await self.redis.llen(f"agent:ws:{uid}")
                            if qlen > 0:
                                oldest_raw = await self.redis.lindex(f"agent:ws:{uid}", 0)
                                # 检查队列最旧消息是否有时间戳
                                if oldest_raw:
                                    try:
                                        oldest_msg = json.loads(oldest_raw)
                                        ts_str = oldest_msg.get("timestamp", "")
                                    except json.JSONDecodeError:
                                        ts_str = ""
                                    if ts_str:
                                        from datetime import datetime as _dt
                                        try:
                                            ts = _dt.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
                                            age = (_dt.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
                                            if age > QUEUE_STALE_SECONDS:
                                                logger.warning(
                                                    "Agent %s stalled: state=idle queue_size=%d oldest_age=%.0fs",
                                                    agent_id, qlen, age)
                                        except ValueError:
                                            pass
                    except Exception as e:
                        logger.warning("Monitor queue staleness check failed for %s: %s", agent_id, e)

                # Layer 3: Stdout silence
                if last_update > 0 and now - last_update > STDOUT_SILENT_SECONDS:
                    self._lifecycle.health_warning(
                        agent_id,
                        f"stdout_silent {int(now - last_update)}s state={state} node={node}")
                    logger.warning("Agent %s stdout silent for %.0fs (state=%s node=%s)",
                                  agent_id, now - last_update, state, node)

            await asyncio.sleep(10)

    # ── Control Channel (双向 Pub/Sub) ─────────────────────

    async def _control_listener(self):
        """SUBSCRIBE agent:control — 接收 API 控制指令，处理并回复。

        v4.2: 双向通信。API 先 SUBSCRIBE agent:control:resp:{corr_id}，
        再 PUBLISH agent:control {cmd, agent_id, user_id, correlation_id}，
        Supervisor 处理后 PUBLISH 响应到 agent:control:resp:{corr_id}。
        """
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("agent:control")
        logger.info("Control listener started: agent:control (v4.2 bidirectional)")

        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    cmd = json.loads(message.get("data", "{}"))
                except json.JSONDecodeError:
                    logger.warning("Control: non-JSON message received: %s", str(message.get("data", ""))[:200])
                    continue

                action = cmd.get("cmd", "")
                agent_id = cmd.get("agent_id", "")
                user_id = cmd.get("user_id", "")
                correlation_id = cmd.get("correlation_id", "")

                if not correlation_id:
                    logger.warning("Control: missing correlation_id, cmd=%s agent=%s user=%s", action, agent_id, user_id)
                    continue

                try:
                    if action == "start":
                        logger.info("Control: start agent_id=%s user_id=%s", agent_id, user_id)
                        result = await self._handle_start(agent_id, user_id, correlation_id)
                    elif action == "stop":
                        logger.info("Control: stop agent_id=%s user_id=%s", agent_id, user_id)
                        result = await self._handle_stop(agent_id, user_id, correlation_id)
                    elif action == "restart":
                        logger.info("Control: restart agent_id=%s user_id=%s", agent_id, user_id)
                        result = await self._handle_restart(agent_id, user_id, correlation_id)
                    elif action == "shutdown":
                        logger.info("Control: supervisor shutdown requested")
                        self._stopping = True
                        result = {"status": "ok", "message": "shutting down"}
                    else:
                        logger.warning("Control: unknown command '%s' cmd=%s", action, str(cmd)[:200])
                        await self._sse_publish(correlation_id, "error",
                            {"error": f"Unknown command: {action}"})
                        result = {"status": "error", "error": f"Unknown command: {action}"}
                except Exception as e:
                    logger.error("Control handler crashed for %s: %s", action, e, exc_info=True)
                    await self._sse_publish(correlation_id, "error",
                        {"error": f"Internal error: {e}"})
                    result = {"status": "error", "error": str(e)}

                # 发送响应
                resp_channel = f"agent:control:resp:{correlation_id}"
                try:
                    await self.redis.publish(resp_channel, json.dumps({
                        **result,
                        "correlation_id": correlation_id,
                        "timestamp": _now_iso(),
                    }, ensure_ascii=False))
                except Exception as e:
                    logger.error("Failed to publish control response (corr_id=%s): %s", correlation_id, e)

        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.unsubscribe("agent:control")
            except Exception as e:
                logger.warning("Control listener unsubscribe error: %s", e)
            try:
                await pubsub.aclose()
            except Exception as e:
                logger.warning("Control listener pubsub aclose error: %s", e)

    # ── SSE helper ────────────────────────────────────────

    async def _sse_publish(self, corr_id: str, event: str, data: dict):
        """向 SSE channel 发布事件。"""
        if not corr_id:
            return
        try:
            await self.redis.publish(
                f"agent:sse:{corr_id}",
                json.dumps({"event": event, "data": data}, ensure_ascii=False),
            )
        except Exception as e:
            logger.error("SSE publish failed for %s: %s", corr_id, e)

    # ── Control handlers ───────────────────────────────────

    async def _handle_start(self, agent_id: str, user_id: str,
                             corr_id: str = "") -> dict:
        """处理 start 指令。

        Returns:
            {"status": "ok"|"already_running"|"error", "agent_state": {...}}
        
        如果传入 corr_id，每个步骤都会通过 SSE 推送进度事件。
        """
        logger.info("_handle_start: agent=%s user=%s corr_id=%s", agent_id, user_id, corr_id)
        # 1. 获取或创建 agent 状态
        state = await self._state_mgr.get_or_create(agent_id, user_id)

        if not state:
            logger.error("_handle_start: get_or_create failed for agent=%s", agent_id)
            await self._sse_publish(corr_id, "error", {"error": "Failed to get or create agent state"})
            return {"status": "error", "error": "Failed to get or create agent state"}

        current_state = state.state

        # 2. 已在运行中
        if current_state in ("idle", "busy"):
            logger.info("_handle_start: agent=%s already %s", agent_id, current_state)
            await self._sse_publish(corr_id, "done",
                                     {"status": "already_running", "agent_id": agent_id})
            return {
                "status": "already_running",
                "agent_state": state.to_dict(),
            }

        # 3. 正在启动中 — 等待就绪
        if current_state == "starting":
            logger.info("_handle_start: agent=%s starting, waiting for ready", agent_id)
            await self._sse_publish(corr_id, "progress",
                                     {"stage": "ready_wait", "message": "Agent 正在启动中，等待就绪..."})
            agent_uid = user_id or agent_id.replace("agent-", "")
            ready = await self.wait_agent_ready(agent_id, timeout=WORKER_READY_TIMEOUT)
            if ready:
                state = await self._state_mgr.get(agent_id)
                if state and state.state == "crashed":
                    error = state.last_error or "Agent crashed during startup"
                    logger.error("_handle_start: agent=%s crashed during wait: %s", agent_id, error)
                    await self._sse_publish(corr_id, "error",
                        {"error": f"Agent crashed during startup: {error}"})
                    return {"status": "error", "error": error}
                logger.info("_handle_start: agent=%s now %s (waited)", agent_id, state.state if state else "?")
                await self._sse_publish(corr_id, "done",
                                         {"status": "already_running", "agent_id": agent_id})
                return {
                    "status": "already_running",
                    "agent_state": state.to_dict() if state else {},
                }
            else:
                logger.error("_handle_start: agent=%s ready wait timed out after %ds", agent_id, WORKER_READY_TIMEOUT)
                self._lifecycle.launch_timeout(agent_id, user_id, WORKER_READY_TIMEOUT)
                await self._sse_publish(corr_id, "error",
                                         {"error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s"})
                return {
                    "status": "error",
                    "error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s",
                }

        # 4. 启动新 agent
        logger.info("_handle_start: launching agent=%s from state=%s", agent_id, current_state)
        self._lifecycle.launch_start(agent_id, user_id)
        await self._state_mgr.transition(agent_id, "starting")
        await self._sse_publish(corr_id, "state",
                                 {"from": current_state, "to": "starting", "agent_id": agent_id})

        await self._sse_publish(corr_id, "progress",
                                 {"stage": "launch", "message": "正在启动 Agent 子进程..."})

        agent_uid = user_id or agent_id.replace("agent-", "")
        try:
            launched = await self.launch_agent(agent_uid)
        except Exception as e:
            logger.error("launch_agent exception for %s: %s", agent_id, e, exc_info=True)
            launched = False

        if not launched:
            # 可能 agent 已经被其他入口启动了，检查是否实际在运行
            if agent_uid in self._agents:
                proc = self._agents[agent_uid]
                if proc.returncode is None:
                    logger.info("_handle_start: agent=%s already running (launched by another path), skipping", agent_id)
                    await self._sse_publish(corr_id, "done",
                        {"status": "already_running", "agent_id": agent_id})
                    state = await self._state_mgr.get(agent_id)
                    return {
                        "status": "already_running",
                        "agent_state": state.to_dict() if state else {},
                    }
            logger.error("_handle_start: agent=%s launch failed", agent_id)
            await self._state_mgr.transition(
                agent_id, "crashed",
                last_error="Failed to spawn subprocess")
            self._lifecycle.launch_failed(agent_id, user_id,
                                          "Failed to spawn subprocess")
            await self._sse_publish(corr_id, "error",
                                     {"error": "Failed to spawn agent subprocess"})
            return {
                "status": "error",
                "error": "Failed to spawn agent subprocess",
            }

        # 5. 等待 worker 就绪
        await self._sse_publish(corr_id, "progress",
                                 {"stage": "bootstrap", "message": "Worker 初始化中 (DB+LLM+Tools+Graph)..."})

        t0 = _now_ts()
        ready = await self.wait_agent_ready(agent_id, timeout=WORKER_READY_TIMEOUT)
        elapsed_ms = (_now_ts() - t0) * 1000

        if ready:
            state = await self._state_mgr.get(agent_id)
            if state and state.state == "idle":
                pid = self._agents.get(agent_uid)
                pid = pid.pid if hasattr(pid, 'pid') and pid else 0
                logger.info("_handle_start: agent=%s started OK pid=%d bootstrap=%dms", agent_id, pid, elapsed_ms)
                self._lifecycle.launch_success(agent_id, user_id, pid, elapsed_ms)
                await self._sse_publish(corr_id, "state",
                                         {"from": "starting", "to": "idle", "agent_id": agent_id, "pid": pid})
                await self._sse_publish(corr_id, "done",
                                         {"status": "started", "agent_id": agent_id, "elapsed_ms": int(elapsed_ms)})
                return {
                    "status": "ok",
                    "agent_state": state.to_dict(),
                }
            else:
                error = state.last_error if state else "Agent exited during bootstrap"
                logger.error("_handle_start: agent=%s bootstrap failed: %s", agent_id, error)
                self._lifecycle.launch_failed(agent_id, user_id, error)
                await self._sse_publish(corr_id, "error", {"error": f"Agent failed to start: {error}"})
                return {
                    "status": "error",
                    "error": f"Agent failed to start: {error}",
                }
        else:
            logger.error("_handle_start: agent=%s bootstrap timeout after %ds", agent_id, WORKER_READY_TIMEOUT)
            await self._state_mgr.transition(
                agent_id, "crashed",
                last_error=f"Startup timeout after {WORKER_READY_TIMEOUT}s")
            self._lifecycle.launch_timeout(agent_id, user_id, WORKER_READY_TIMEOUT)
            await self._sse_publish(corr_id, "error",
                                     {"error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s"})
            return {
                "status": "error",
                "error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s",
            }

    async def _handle_stop(self, agent_id: str, user_id: str,
                            corr_id: str = "") -> dict:
        """处理 stop 指令。"""
        logger.info("_handle_stop: agent=%s user=%s corr_id=%s", agent_id, user_id, corr_id)
        state = await self._state_mgr.get(agent_id)
        if not state:
            logger.warning("_handle_stop: agent=%s not found", agent_id)
            await self._sse_publish(corr_id, "error", {"error": "Agent not found"})
            return {"status": "error", "error": "Agent not found"}

        if state.state in ("stopped", "crashed"):
            logger.info("_handle_stop: agent=%s already %s", agent_id, state.state)
            await self._sse_publish(corr_id, "done",
                                     {"status": "already_stopped", "agent_id": agent_id})
            return {"status": "already_stopped", "agent_state": state.to_dict()}

        from_state = state.state
        logger.info("_handle_stop: stopping agent=%s from state=%s", agent_id, from_state)
        self._lifecycle.stop_requested(agent_id, user_id)
        await self._state_mgr.transition(agent_id, "stopping")
        await self._sse_publish(corr_id, "state",
                                 {"from": from_state, "to": "stopping", "agent_id": agent_id})

        await self._sse_publish(corr_id, "progress",
                                 {"stage": "stopping", "message": "正在发送停止信号..."})

        agent_uid = user_id or agent_id.replace("agent-", "")
        stopped = await self.stop_agent(agent_uid, graceful=True)

        if stopped:
            logger.info("_handle_stop: agent=%s stopped OK", agent_id)
            await self._sse_publish(corr_id, "progress",
                                     {"stage": "stopped", "message": "Agent 已停止"})
            await self._state_mgr.transition(agent_id, "stopped")
            uptime = _now_ts() - (self._agent_started.get(agent_uid, _now_ts()))
            self._lifecycle.stop_success(agent_id, user_id, uptime)
        else:
            logger.warning("_handle_stop: agent=%s stop_agent returned False (already exited?)", agent_id)
            await self._state_mgr.transition(agent_id, "stopped")

        await self._sse_publish(corr_id, "state",
                                 {"from": "stopping", "to": "stopped", "agent_id": agent_id})
        await self._sse_publish(corr_id, "done",
                                 {"status": "stopped", "agent_id": agent_id})

        state = await self._state_mgr.get(agent_id)
        return {"status": "ok", "agent_state": state.to_dict() if state else {}}

    async def _handle_restart(self, agent_id: str, user_id: str,
                               corr_id: str = "") -> dict:
        """处理 restart 指令。"""
        logger.info("_handle_restart: agent=%s user=%s corr_id=%s", agent_id, user_id, corr_id)
        await self._sse_publish(corr_id, "progress",
                                 {"stage": "stopping", "message": "正在停止 Agent..."})

        stop_result = await self._handle_stop(agent_id, user_id, corr_id)
        if stop_result.get("status") == "error":
            return stop_result

        await asyncio.sleep(1)

        logger.info("_handle_restart: re-launching agent=%s", agent_id)
        await self._sse_publish(corr_id, "progress",
                                 {"stage": "launch", "message": "正在重新启动 Agent..."})

        start_result = await self._handle_start(agent_id, user_id, corr_id)
        if start_result.get("status") in ("ok", "already_running"):
            state = start_result.get("agent_state", {})
            logger.info("_handle_restart: agent=%s restarted OK", agent_id)
            await self._sse_publish(corr_id, "done",
                                     {"status": "restarted", "agent_id": agent_id})
            return {"status": "ok", "agent_state": state}
        return start_result

    # ── Backward-compatible helpers ────────────────────────

    def _should_restart(self, uid: str, exit_code: int) -> bool:
        """v4.2: 保留接口 — 判断是否应自动重启。"""
        if self._stopping:
            return False
        if exit_code == 0:
            return False
        return True

    async def _hsync_status(self, uid: str, **fields):
        """v4.2: 保留旧接口 — 同步状态到 Redis。

        同时写入旧 agent:status Hash (JSON) 和新 agent:state:{agent_id} Hash (字段)。
        """
        base = self._status_cache.get(uid, {
            "state": "starting", "pid": 0,
            "node": None, "active_turns": 0,
            "started_at": _now_iso(),
        })
        base.update(fields)
        base["updated_at"] = _now_iso()
        self._status_cache[uid] = base

        if self.redis:
            try:
                await self.redis.hset("agent:status", uid,
                                       json.dumps(base, ensure_ascii=False))
            except Exception as e:
                logger.error("HSET agent:status failed for %s: %s", uid, e)
        else:
            logger.debug("_hsync_status: redis not available, skipping HSET for %s", uid)

        # 同步到新 agent:state:{agent_id}
        if self._state_mgr:
            agent_id = self._uid_to_agent_id.get(uid, f"agent-{uid}")
            sync_fields = {}
            for k in ("state", "pid", "node", "active_turns", "last_error"):
                if k in fields:
                    sync_fields["current_node" if k == "node" else k] = fields[k]
            if sync_fields:
                try:
                    await self._state_mgr.update(agent_id, **sync_fields)
                except Exception as e:
                    logger.warning("_hsync_status state_mgr.update failed for %s: %s", agent_id, e)

    # ── Run ────────────────────────────────────────────────

    async def run(self):
        """主入口 — 等待依赖就绪 → 启动所有循环。"""
        import redis.asyncio as aioredis

        # 创建唯一的 Redis 连接（内部连接池，所有任务复用）
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        self._state_mgr._redis = self.redis

        # 等待 Redis + DB 就绪
        await self._wait_for_deps()

        # 扫描用户，确保 agent 存在
        await self._bootstrap_users()

        # 为所有 agent 创建子进程
        await self._launch_all_agents()

        # 启动 3 个后台循环（复用 self.redis）
        await asyncio.gather(
            self._ingress_loop(),
            self._monitor_loop(),
            self._control_listener(),
        )

    async def _wait_for_deps(self, max_retries: int = 30, interval: float = 2):
        """等待 Redis + Postgres 就绪。"""
        # Redis
        for i in range(1, max_retries + 1):
            try:
                await self.redis.ping()
                logger.info("Redis ready: %s", self.redis_url)
                break
            except Exception:
                if i == max_retries:
                    logger.error("Redis not ready after %d attempts", max_retries)
                    raise
                logger.info("Waiting for Redis... (%d/%d)", i, max_retries)
                await asyncio.sleep(interval)

        # Postgres
        for i in range(1, max_retries + 1):
            try:
                self._db._raw_conn.cursor().execute("SELECT 1")
                logger.info("Postgres ready")
                break
            except Exception:
                if i == max_retries:
                    logger.error("Postgres not ready after %d attempts", max_retries)
                    raise
                logger.info("Waiting for Postgres... (%d/%d)", i, max_retries)
                await asyncio.sleep(interval)

    async def _bootstrap_users(self):
        """扫描 DB 用户表，为每个活跃用户确保 agent 存在并同步到 Redis。"""
        try:
            users = self._db.list_active_users()
            logger.info("Found %d active users in DB, ensuring agents exist", len(users))
            for user in users:
                user_id = user.get("id", "")
                if not user_id:
                    continue
                agent = self._db.get_default_agent(user_id)
                if agent:
                    agent_id = agent["id"]
                    await self._state_mgr.get_or_create(agent_id, user_id)
                    logger.info("Bootstrapped agent=%s user=%s state=%s",
                                agent_id, user_id, agent.get("state", "?"))
                else:
                    agent_id = self._db.create_agent(user_id=user_id)
                    await self._state_mgr.get_or_create(agent_id, user_id)
                    logger.info("Auto-created agent=%s for user=%s", agent_id, user_id)
        except Exception as e:
            logger.error("Failed to bootstrap agents from users: %s", e, exc_info=True)

    async def _launch_all_agents(self):
        """为所有活跃 agent 启动子进程。先 transition("starting") 再 launch。"""
        active_ids = await self._state_mgr.list_active()
        logger.info("Launching %d active agents", len(active_ids))
        for agent_id in active_ids:
            state = await self._state_mgr.get(agent_id)
            if not state or not state.user_id or state.user_id == "user-default":
                continue
            # 确保状态为 starting（与 _handle_start 一致）
            await self._state_mgr.transition(agent_id, "starting")
            launched = await self.launch_agent(state.user_id)
            if launched:
                logger.info("Bootstrap launched agent=%s user=%s", agent_id, state.user_id)
            else:
                logger.warning("Bootstrap failed to launch agent=%s (already running?)", agent_id)


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None):
    """Supervisor 主入口。"""
    path = Path(data_dir) if data_dir else None
    supervisor = AgentSupervisor()
    await supervisor.bootstrap(data_dir=path)
    await supervisor.run()


if __name__ == "__main__":
    data_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--data-dir" and i + 1 < len(sys.argv) - 1:
            data_dir = sys.argv[i + 2]
            break
        if arg.startswith("--data-dir="):
            data_dir = arg.split("=", 1)[1]
            break
    asyncio.run(main(data_dir=data_dir))
