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
            del self._agents[user_id]

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
        if proc is None or proc.returncode is not None:
            return False

        if graceful:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Agent %s did not exit gracefully, force killing", user_id)
                proc.kill()
        else:
            proc.kill()

        return True

    async def _relaunch_agent(self, user_id: str):
        """Agent 崩溃后自动重启。"""
        agent_id = f"agent-{user_id}"
        state = await self._state_mgr.get(agent_id)
        rc = state.restart_count + 1 if state else 1

        self._lifecycle.auto_restart(agent_id, user_id, rc)
        logger.info("Auto-restarting agent %s (attempt #%d)", agent_id, rc)

        await self._state_mgr.update(agent_id, restart_count=rc)
        await self.launch_agent(user_id)
        await self._state_mgr.transition(agent_id, "starting")

    # ── Message Ingress (BRPOP → stdin) ────────────────────

    async def _ingress_loop(self):
        """BRPOP 所有用户队列 → route to agent subprocess stdin."""
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        # State manager 现在可以使用 redis
        self._state_mgr._redis = self.redis

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
            except Exception:
                await asyncio.sleep(0.5)
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
                except Exception:
                    pass
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
        agent_id = f"agent-{uid}"

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
                    logger.debug("Agent %s stdout non-JSON: %s", uid, line[:100])
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
                        current = await self._state_mgr.get(agent_id)
                        if current and current.state in ("starting", "pending", "busy"):
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
                        await self._state_mgr.transition(
                            agent_id, "busy",
                            pid=pid, current_node=node,
                            active_turns=active,
                            last_active_at=_now_iso(),
                        )

                    elif state == "stopped":
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
                        await self.redis.lpush(f"agent:outbox:{uid}",
                                               json.dumps(envelope, ensure_ascii=False))
                    except Exception as e:
                        logger.error("Failed to push outbox for %s: %s", uid, e)

                elif msg_type in ("tool", "status", "tool_execution",
                                   "plan_todo_update", "error"):
                    try:
                        msg["agentId"] = msg.get("agentId", agent_id)
                        await self.redis.lpush(f"agent:outbox:{uid}",
                                               json.dumps(msg, ensure_ascii=False))
                    except Exception as e:
                        logger.error("Failed to push outbox for %s: %s", uid, e)

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
        except Exception:
            pass

    async def _handle_exit(self, uid: str, proc: asyncio.subprocess.Process):
        """处理 Agent 退出。"""
        agent_id = f"agent-{uid}"

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

        rc = proc.returncode
        state = "stopped" if rc == 0 else "crashed"

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

        # Auto restart on crash
        if to_state == "crashed" and not self._stopping:
            asyncio.create_task(self._relaunch_agent(uid))

        # Clean up ready event — trigger it so any waiter unblocks
        evt = self._ready_events.pop(agent_id, None)
        if evt:
            evt.set()

        # Re-push parked messages
        if uid in self._agents:
            try:
                parked = await self.redis.lrange(f"agent:ws:{uid}:parked", 0, -1)
                for msg in parked:
                    await self.redis.lpush(f"agent:ws:{uid}", msg)
                await self.redis.delete(f"agent:ws:{uid}:parked")
            except Exception:
                pass

    # ── Health Monitor (3 layers) ──────────────────────────

    async def _monitor_loop(self):
        """每 10s 检测所有 Agent 健康状态。"""
        await asyncio.sleep(5)
        logger.info("Health monitor started")
        while not self._stopping:
            for uid, proc in list(self._agents.items()):
                rc = proc.returncode
                if rc is not None:
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
                                f"agent-{uid}",
                                f"stuck_waiting_user node={node} age={int(now - last_update)}s")
                            logger.warning("Agent %s stuck waiting for user: node=%s age=%.0fs",
                                          uid, node, now - last_update)
                        else:
                            self._lifecycle.health_timeout(
                                f"agent-{uid}", node, now - last_update)
                            logger.error("Agent %s busy timeout: node=%s age=%.0fs → SIGTERM",
                                        uid, node, now - last_update)
                            proc.send_signal(signal.SIGTERM)
                            await self._state_mgr.transition(
                                f"agent-{uid}", "crashed",
                                last_error=f"stuck_in_{node}")

                # Layer 2: Idle queue staleness
                if state == "idle":
                    try:
                        if self.redis:
                            oldest_raw = await self.redis.xrange(
                                f"agent:ws:{uid}", count=1)
                            if oldest_raw:
                                oldest_id = oldest_raw[0][0]
                                oldest_ms = int(oldest_id.split("-")[0])
                                age = (now * 1000 - oldest_ms) / 1000
                                if age > QUEUE_STALE_SECONDS:
                                    logger.warning(
                                        "Agent %s stalled: state=idle queue_oldest=%.0fs",
                                        uid, age)
                    except Exception:
                        pass

                # Layer 3: Stdout silence
                if last_update > 0 and now - last_update > STDOUT_SILENT_SECONDS:
                    self._lifecycle.health_warning(
                        f"agent-{uid}",
                        f"stdout_silent {int(now - last_update)}s state={state} node={node}")
                    logger.warning("Agent %s stdout silent for %.0fs (state=%s node=%s)",
                                  uid, now - last_update, state, node)

            await asyncio.sleep(10)

    # ── Control Channel (双向 Pub/Sub) ─────────────────────

    async def _control_listener(self):
        """SUBSCRIBE agent:control — 接收 API 控制指令，处理并回复。

        v4.2: 双向通信。API 先 SUBSCRIBE agent:control:resp:{corr_id}，
        再 PUBLISH agent:control {cmd, agent_id, user_id, correlation_id}，
        Supervisor 处理后 PUBLISH 响应到 agent:control:resp:{corr_id}。
        """
        import redis.asyncio as aioredis
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe("agent:control")
        logger.info("Control listener started: agent:control (v4.2 bidirectional)")

        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    cmd = json.loads(message.get("data", "{}"))
                except json.JSONDecodeError:
                    continue

                action = cmd.get("cmd", "")
                agent_id = cmd.get("agent_id", "")
                user_id = cmd.get("user_id", "")
                correlation_id = cmd.get("correlation_id", "")

                if not correlation_id:
                    logger.warning("Control: missing correlation_id, ignoring")
                    continue

                if action == "start":
                    logger.info("Control: start agent_id=%s user_id=%s", agent_id, user_id)
                    result = await self._handle_start(agent_id, user_id)
                elif action == "stop":
                    logger.info("Control: stop agent_id=%s user_id=%s", agent_id, user_id)
                    result = await self._handle_stop(agent_id, user_id)
                elif action == "restart":
                    logger.info("Control: restart agent_id=%s user_id=%s", agent_id, user_id)
                    result = await self._handle_restart(agent_id, user_id)
                elif action == "shutdown":
                    logger.info("Control: supervisor shutdown requested")
                    self._stopping = True
                    result = {"status": "ok", "message": "shutting down"}
                else:
                    logger.warning("Control: unknown command %s", action)
                    result = {"status": "error", "error": f"Unknown command: {action}"}

                # 发送响应
                resp_channel = f"agent:control:resp:{correlation_id}"
                try:
                    await r.publish(resp_channel, json.dumps({
                        **result,
                        "correlation_id": correlation_id,
                        "timestamp": _now_iso(),
                    }, ensure_ascii=False))
                except Exception as e:
                    logger.error("Failed to publish control response: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe("agent:control")
            await r.aclose()

    # ── Control handlers ───────────────────────────────────

    async def _handle_start(self, agent_id: str, user_id: str) -> dict:
        """处理 start 指令。

        Returns:
            {"status": "ok"|"already_running"|"error", "agent_state": {...}}
        """
        # 1. 获取或创建 agent 状态
        state = await self._state_mgr.get_or_create(agent_id, user_id)

        if not state:
            return {"status": "error", "error": "Failed to get or create agent state"}

        current_state = state.state

        # 2. 已在运行中
        if current_state in ("idle", "busy"):
            return {
                "status": "already_running",
                "agent_state": state.to_dict(),
            }

        # 3. 正在启动中 — 等待就绪
        if current_state == "starting":
            agent_uid = user_id or agent_id.replace("agent-", "")
            ready = await self.wait_agent_ready(agent_id, timeout=WORKER_READY_TIMEOUT)
            if ready:
                state = await self._state_mgr.get(agent_id)
                return {
                    "status": "already_running",
                    "agent_state": state.to_dict() if state else {},
                }
            else:
                self._lifecycle.launch_timeout(agent_id, user_id, WORKER_READY_TIMEOUT)
                return {
                    "status": "error",
                    "error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s",
                }

        # 4. 启动新 agent
        self._lifecycle.launch_start(agent_id, user_id)
        await self._state_mgr.transition(agent_id, "starting")

        agent_uid = user_id or agent_id.replace("agent-", "")
        launched = await self.launch_agent(agent_uid)

        if not launched:
            await self._state_mgr.transition(
                agent_id, "crashed",
                last_error="Failed to spawn subprocess")
            self._lifecycle.launch_failed(agent_id, user_id,
                                          "Failed to spawn subprocess")
            return {
                "status": "error",
                "error": "Failed to spawn agent subprocess",
            }

        # 5. 等待 worker 就绪
        t0 = _now_ts()
        ready = await self.wait_agent_ready(agent_id, timeout=WORKER_READY_TIMEOUT)
        elapsed_ms = (_now_ts() - t0) * 1000

        if ready:
            state = await self._state_mgr.get(agent_id)
            if state and state.state == "idle":
                pid = self._agents.get(agent_uid)
                pid = pid.pid if hasattr(pid, 'pid') and pid else 0
                self._lifecycle.launch_success(agent_id, user_id, pid, elapsed_ms)
                return {
                    "status": "ok",
                    "agent_state": state.to_dict(),
                }
            else:
                # 进程在 bootstrap 期间退出/crash
                error = state.last_error if state else "Agent exited during bootstrap"
                self._lifecycle.launch_failed(agent_id, user_id, error)
                return {
                    "status": "error",
                    "error": f"Agent failed to start: {error}",
                }
        else:
            await self._state_mgr.transition(
                agent_id, "crashed",
                last_error=f"Startup timeout after {WORKER_READY_TIMEOUT}s")
            self._lifecycle.launch_timeout(agent_id, user_id, WORKER_READY_TIMEOUT)
            return {
                "status": "error",
                "error": f"Agent startup timed out after {WORKER_READY_TIMEOUT}s",
            }

    async def _handle_stop(self, agent_id: str, user_id: str) -> dict:
        """处理 stop 指令。"""
        state = await self._state_mgr.get(agent_id)
        if not state:
            return {"status": "error", "error": "Agent not found"}

        if state.state in ("stopped", "crashed"):
            return {"status": "already_stopped", "agent_state": state.to_dict()}

        self._lifecycle.stop_requested(agent_id, user_id)
        await self._state_mgr.transition(agent_id, "stopping")

        agent_uid = user_id or agent_id.replace("agent-", "")
        stopped = await self.stop_agent(agent_uid, graceful=True)

        if stopped:
            await self._state_mgr.transition(agent_id, "stopped")
            uptime = _now_ts() - (self._agent_started.get(agent_uid, _now_ts()))
            self._lifecycle.stop_success(agent_id, user_id, uptime)
        else:
            await self._state_mgr.transition(agent_id, "stopped")

        state = await self._state_mgr.get(agent_id)
        return {"status": "ok", "agent_state": state.to_dict() if state else {}}

    async def _handle_restart(self, agent_id: str, user_id: str) -> dict:
        """处理 restart 指令。"""
        # 先停止再启动
        stop_result = await self._handle_stop(agent_id, user_id)
        if stop_result.get("status") == "error":
            return stop_result

        # 短暂等待进程完全退出
        await asyncio.sleep(1)
        return await self._handle_start(agent_id, user_id)

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

        # 同步到新 agent:state:{agent_id}
        if self._state_mgr:
            agent_id = f"agent-{uid}"
            sync_fields = {}
            for k in ("state", "pid", "node", "active_turns", "last_error"):
                if k in fields:
                    sync_fields["current_node" if k == "node" else k] = fields[k]
            if sync_fields:
                try:
                    await self._state_mgr.update(agent_id, **sync_fields)
                except Exception:
                    pass

    # ── Run ────────────────────────────────────────────────

    async def run(self):
        """主入口 — 启动 Supervisor 所有循环。"""
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        self._state_mgr._redis = self.redis

        # 扫描 DB，重建所有活跃 agent 的 Redis 状态
        try:
            rows = self._db._fetchall(
                "SELECT * FROM agents WHERE state NOT IN ('stopped', 'crashed')")
            logger.info("Found %d active agents in DB", len(rows))
            for row in rows:
                agent_id = row.get("id", "")
                user_id = row.get("user_id", "")
                if agent_id and user_id and user_id != "user-default":
                    await self._state_mgr.get_or_create(agent_id, user_id)
                    logger.info("Recovered agent from DB: %s (user=%s state=%s)",
                                agent_id, user_id, row.get("state", "?"))
        except Exception as e:
            logger.error("Failed to scan agents table: %s", e)

        # 为所有活跃 agent 创建子进程
        active_ids = await self._state_mgr.list_active()
        for agent_id in active_ids:
            state = await self._state_mgr.get(agent_id)
            if state and state.user_id and state.user_id != "user-default":
                asyncio.create_task(self.launch_agent(state.user_id))

        # 启动 3 个后台循环
        await asyncio.gather(
            self._ingress_loop(),
            self._monitor_loop(),
            self._control_listener(),
        )


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
