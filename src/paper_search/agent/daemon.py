"""Agent Supervisor — 管理所有用户 Agent 子进程。

v4.1: 替代旧的 AgentManager 单用户模型。
  - 启动时扫描 agents 表，为每个活跃用户创建子进程
  - 消息路由: BRPOP agent:ws:{uid} → stdin → 子进程
  - 出站拦截: 子进程 stdout → LPUSH agent:outbox:{uid}
  - 3 层健康检测: poll() / 队列积压 / stdout 超时
  - 控制指令: SUBSCRIBE agent:control
  - 状态维护: HSET agent:status Hash

使用方式:
    python -m paper_search.agent.daemon
    python -m paper_search.agent.daemon --data-dir /path
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
QUEUE_STALE_SECONDS = 43200  # 0.5d — idle but queue is old
STDOUT_SILENT_SECONDS = 15


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

    # ── Launch / Stop ──────────────────────────────────────

    async def launch_agent(self, user_id: str) -> bool:
        """启动一个用户 Agent 子进程。"""
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
            await self._hsync_status(user_id, state="crashed", last_error=str(e))
            return False

        self._agents[user_id] = proc
        self._agent_started[user_id] = _now_ts()
        if proc.pid:
            self._pid_to_uid[proc.pid] = user_id

        # 更新 DB state（Supervisor 管理）
        try:
            from .pgdb import PostgresAgentDB
            db = PostgresAgentDB()
            db.update_agent(
                db.get_default_agent(user_id)["id"],
                user_id=user_id, state="starting")
            db.close()
        except Exception as e:
            logger.warning("Failed to update DB state for %s: %s", user_id, e)

        await self._hsync_status(
            user_id, state="starting", pid=proc.pid or 0,
            node=None, active_turns=0,
        )

        # Start reading stdout in background
        asyncio.create_task(self._read_agent_stdout(user_id, proc))
        # Start reading stderr for crash logs
        asyncio.create_task(self._read_agent_stderr(user_id, proc))

        logger.info("Agent %s launched: pid=%d", user_id, proc.pid)
        return True

    async def stop_agent(self, user_id: str, graceful: bool = True) -> bool:
        """停止一个用户 Agent。"""
        proc = self._agents.get(user_id)
        if proc is None or proc.returncode is not None:
            await self._hsync_status(user_id, state="stopped")
            return False

        await self._hsync_status(user_id, state="stopping")
        if graceful:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Agent %s did not exit gracefully, force killing", user_id)
                proc.kill()
        else:
            proc.kill()

        await self._hsync_status(user_id, state="stopped")
        # 更新 DB
        try:
            from .pgdb import PostgresAgentDB
            db = PostgresAgentDB()
            agent = db.get_default_agent(user_id)
            if agent:
                db.update_agent(agent["id"], user_id=user_id, state="stopped")
            db.close()
        except Exception as e:
            logger.warning("Failed to update DB state for %s: %s", user_id, e)
        return True

    async def _relaunch_agent(self, user_id: str):
        """Agent 崩溃后自动重启。"""
        logger.info("Auto-restarting agent %s", user_id)
        await self.launch_agent(user_id)

    # ── Message Ingress (BRPOP → stdin) ────────────────────

    async def _ingress_loop(self):
        """BRPOP 所有用户队列 → route to agent subprocess stdin."""
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)

        logger.info("Ingress loop started")
        while not self._stopping:
            if not self._agents:
                await asyncio.sleep(1)
                continue

            # Build list of active queues
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
                # Agent not running — park the message
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

    # ── Message Egress (stdout → LPUSH) ─────────────────────

    async def _read_agent_stdout(self, uid: str, proc: asyncio.subprocess.Process):
        """持续读取 Agent 子进程 stdout，解析 JSON → 路由到 Redis。"""
        logger.info("Egress reader started for %s (pid=%d)", uid, proc.pid)
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
                    self._status_cache[uid] = {
                        "state": state, "node": node,
                        "active_turns": active, "pid": proc.pid or 0,
                        "updated_at": _now_ts(),
                    }
                    await self._hsync_status(
                        uid, state=state, node=node,
                        active_turns=active, pid=proc.pid or 0,
                    )

                elif msg_type == "reply":
                    # Pass through to outbox
                    content = msg.get("content", "")
                    envelope = {
                        "type": "message",
                        "subType": "reply",
                        "agentId": f"agent-{uid}",
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
                    # Pass through other message types
                    try:
                        msg["agentId"] = msg.get("agentId", f"agent-{uid}")
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
        # Wait for process to fully exit if not already
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

        rc = proc.returncode
        state = "stopped" if rc == 0 else "crashed"
        await self._hsync_status(uid, state=state,
                                 last_error=f"exit_code={rc}" if rc != 0 else "")
        logger.info("Agent %s %s (pid=%s rc=%d)", uid, state,
                    self._agent_started.get(uid, "?"), rc)

        # 更新 DB
        try:
            from .pgdb import PostgresAgentDB
            db = PostgresAgentDB()
            agent = db.get_default_agent(uid)
            if agent:
                db.update_agent(agent["id"], user_id=uid, state=state)
            db.close()
        except Exception as e:
            logger.warning("Failed to update DB state for %s: %s", uid, e)

        if state == "crashed" and not self._stopping:
            asyncio.create_task(self._relaunch_agent(uid))

        # Clean up stale parked messages (return to main queue on relaunch)
        if uid in self._agents:
            try:
                parked = await self.redis.lrange(f"agent:ws:{uid}:parked", 0, -1)
                for msg in parked:
                    await self.redis.lpush(f"agent:ws:{uid}", msg)
                await self.redis.delete(f"agent:ws:{uid}:parked")
            except Exception:
                pass

    # ── Health Monitor (3 layers) ───────────────────────────

    async def _monitor_loop(self):
        """每 10s 检测所有 Agent 健康状态。"""
        await asyncio.sleep(5)  # Let agents initialize first
        logger.info("Health monitor started")
        while not self._stopping:
            for uid, proc in list(self._agents.items()):
                # Layer 1: OS process check
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
                            # Don't kill — agent is waiting for user input
                            logger.warning("Agent %s stuck waiting for user: node=%s age=%.0fs",
                                          uid, node, now - last_update)
                        else:
                            logger.error("Agent %s busy timeout: node=%s age=%.0fs → SIGTERM",
                                        uid, node, now - last_update)
                            proc.send_signal(signal.SIGTERM)
                            await self._hsync_status(uid, state="crashed",
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
                                    await self._hsync_status(uid, state="stalled")
                    except Exception:
                        pass

                # Layer 3: Stdout silence
                if last_update > 0 and now - last_update > STDOUT_SILENT_SECONDS:
                    logger.warning("Agent %s stdout silent for %.0fs (state=%s node=%s)",
                                  uid, now - last_update, state, node)

            await asyncio.sleep(10)

    # ── Control Channel (Pub/Sub) ───────────────────────────

    async def _control_listener(self):
        """SUBSCRIBE agent:control — 接收 API 启停命令。"""
        import redis.asyncio as aioredis
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe("agent:control")
        logger.info("Control listener started: agent:control")

        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    cmd = json.loads(message.get("data", "{}"))
                except json.JSONDecodeError:
                    continue

                action = cmd.get("cmd", "")
                uid = cmd.get("user_id", "")

                if action == "start":
                    logger.info("Control: start agent %s", uid)
                    await self.launch_agent(uid)
                elif action == "stop":
                    logger.info("Control: stop agent %s", uid)
                    await self.stop_agent(uid)
                elif action == "restart":
                    logger.info("Control: restart agent %s", uid)
                    await self.stop_agent(uid)
                    await self.launch_agent(uid)
                elif action == "shutdown":
                    logger.info("Control: supervisor shutdown requested")
                    self._stopping = True
                else:
                    logger.warning("Control: unknown command %s", action)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe("agent:control")
            await r.aclose()

    # ── Status helpers ──────────────────────────────────────

    async def _hsync_status(self, uid: str, **fields):
        """HSET agent:status {uid} JSON."""
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

    def _should_restart(self, uid: str, exit_code: int) -> bool:
        """判断是否应自动重启 Agent（非手动 stop，非正常 exit 0）。"""
        if self._stopping:
            return False
        if exit_code == 0:
            return False
        # Restart on crash (too many crashed signals → maybe stop?)
        return True

    # ── Bootstrap (shared resources) ────────────────────────

    async def bootstrap(self, data_dir: Optional[Path] = None):
        """初始化 DB/LLM/Tools — 子进程共享同一组后端。"""
        self._data_dir = data_dir
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        os.environ.setdefault("DEBUG_PROTOCOL", "1")
        logger.info("AgentSupervisor bootstrapping...")

    # ── Run ─────────────────────────────────────────────────

    async def run(self):
        """主入口 — 启动 Supervisor 所有循环。"""
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)

        # Scan DB for active agents and launch subprocesses
        from .pgdb import PostgresAgentDB
        db = PostgresAgentDB()
        try:
            active_agents = db._fetchall(
                "SELECT DISTINCT user_id FROM agents WHERE state != 'stopped'")
            logger.info("Found %d active users in DB", len(active_agents))
        except Exception as e:
            logger.error("Failed to scan agents table: %s", e)
            active_agents = []
        finally:
            db.close()

        # Launch all agents
        for row in active_agents:
            uid = row.get("user_id", "")
            if uid and uid != "user-default":
                asyncio.create_task(self.launch_agent(uid))

        # Start 4 background loops
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
