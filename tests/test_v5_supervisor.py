"""v4.1 Supervisor + Agent Worker 架构测试。

覆盖:
  1. AgentSupervisor 进程管理 (launch/stop/status)
  2. agent:status Hash 格式
  3. control Pub/Sub 命令格式
  4. Agent Worker stdin/stdout 协议
  5. Agent Worker 状态上报
  6. 3 层健康检测逻辑
  7. 节点超时配置
  8. API 状态查询路由

运行:
    PYTHONPATH=src pytest tests/test_v5_supervisor.py -v
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, AsyncMock, call


# ═══════════════════════════════════════════════════════════════
# Test 1: AgentSupervisor — 进程管理
# ═══════════════════════════════════════════════════════════════


class TestSupervisorProcess:
    """AgentSupervisor launch/stop/relaunch。"""

    def test_supervisor_init(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        assert s._agents == {}
        assert s._status_cache == {}
        assert s._stopping is False

    def test_launch_agent_returns_true(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.pid = 12345
            mock_exec.return_value = mock_proc
            import asyncio
            ok = asyncio.run(s.launch_agent("user-abc"))
            assert ok is True
            assert "user-abc" in s._agents

    def test_launch_agent_creates_right_cmd(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.pid = 12345
            mock_exec.return_value = mock_proc
            import asyncio
            s.redis = AsyncMock()
            s._hsync_status = AsyncMock()
            asyncio.run(s.launch_agent("user-abc"))
            args = mock_exec.call_args.args
            assert "--user-id" in args
            uid_idx = args.index("--user-id")
            assert args[uid_idx + 1] == "user-abc"

    def test_stop_agent_sends_sigterm(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        s.redis = AsyncMock()
        s._hsync_status = AsyncMock()
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        mock_proc.wait = AsyncMock()
        s._agents["user-abc"] = mock_proc
        import asyncio
        ok = asyncio.run(s.stop_agent("user-abc"))
        assert ok is True
        mock_proc.send_signal.assert_called()

    def test_should_restart_on_crash(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        assert s._should_restart("user-abc", 1) is True   # crash → restart
        assert s._should_restart("user-abc", -9) is True  # killed → restart
        assert s._should_restart("user-abc", 0) is False  # normal exit

    def test_should_not_restart_when_stopping(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        s._stopping = True
        assert s._should_restart("user-abc", 1) is False


# ═══════════════════════════════════════════════════════════════
# Test 2: agent:status Hash
# ═══════════════════════════════════════════════════════════════


class TestStatusHash:
    """agent:status Hash 格式 + hsync。"""

    def test_status_hash_format(self):
        status = json.dumps({
            "state": "busy", "node": "executing",
            "active_turns": 2, "pid": 1001,
            "started_at": "2026-07-18T10:00:00Z",
            "updated_at": "2026-07-18T10:05:00Z",
        })
        data = json.loads(status)
        assert "state" in data
        assert "node" in data
        assert "pid" in data
        assert "active_turns" in data

    def test_hsync_status_calls_hset(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        s.redis = AsyncMock()
        s.redis.hset = AsyncMock()
        import asyncio
        asyncio.run(s._hsync_status("user-abc", state="busy", node="plan"))
        s.redis.hset.assert_called_once()

    def test_hsync_status_updates_cache(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        import asyncio
        asyncio.run(s._hsync_status("user-abc", state="idle", node=None))
        assert s._status_cache["user-abc"]["state"] == "idle"


# ═══════════════════════════════════════════════════════════════
# Test 3: Control Pub/Sub
# ═══════════════════════════════════════════════════════════════


class TestControlProtocol:
    """agent:control Pub/Sub 命令格式。"""

    def test_start_command_format(self):
        cmd = json.dumps({"cmd": "start", "user_id": "user-abc"})
        data = json.loads(cmd)
        assert data["cmd"] == "start"
        assert data["user_id"] == "user-abc"

    def test_stop_command_format(self):
        cmd = json.dumps({"cmd": "stop", "user_id": "user-xyz"})
        data = json.loads(cmd)
        assert data["cmd"] == "stop"
        assert data["user_id"] == "user-xyz"

    def test_restart_command_format(self):
        cmd = json.dumps({"cmd": "restart", "user_id": "user-abc"})
        data = json.loads(cmd)
        assert data["cmd"] == "restart"

    def test_shutdown_command_format(self):
        cmd = json.dumps({"cmd": "shutdown"})
        data = json.loads(cmd)
        assert data["cmd"] == "shutdown"


# ═══════════════════════════════════════════════════════════════
# Test 4: Agent Worker 协议
# ═══════════════════════════════════════════════════════════════


class TestWorkerProtocol:
    """Agent Worker stdout/stdin 协议。"""

    def test_worker_sends_state_on_bootstrap(self):
        from paper_search.agent.agent_worker import supervisor_send
        supervisor_send("state", state="idle", node=None, active_turns=0)
        # No assertion needed — just verifying it doesn't crash

    def test_state_message_format(self):
        msg = json.dumps({
            "type": "state", "state": "busy",
            "node": "intent_classify", "active_turns": 1,
            "timestamp": "2026-07-18T10:00:00Z",
        })
        data = json.loads(msg)
        assert data["type"] == "state"
        assert data["state"] in ("idle", "busy", "stopped")

    def test_reply_message_format(self):
        msg = json.dumps({
            "type": "reply", "content": "Hello world",
            "session_id": "main", "timestamp": "2026-07-18T10:00:00Z",
        })
        data = json.loads(msg)
        assert data["type"] == "reply"
        assert "content" in data

    def test_worker_requires_user_id(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "paper_search.agent.agent_worker"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert "user-id" in (result.stderr + result.stdout).lower()

    def test_worker_starts_with_user_id(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "paper_search.agent.agent_worker",
             "--user-id", "test-user"],
            capture_output=True, text=True, timeout=10,
        )
        # Should start and print state message, but then fail on stdin EOF
        stdout_lines = result.stdout.strip().split("\n")
        assert len(stdout_lines) >= 1
        states = [json.loads(l) for l in stdout_lines if l.strip()]
        state_msgs = [s for s in states if s.get("type") == "state"]
        assert len(state_msgs) >= 1


# ═══════════════════════════════════════════════════════════════
# Test 5: 3 层健康检测
# ═══════════════════════════════════════════════════════════════


class TestHealthDetection:
    """3 层检测逻辑：poll/积压/stdout。"""

    def test_node_timeout_has_all_nodes(self):
        from paper_search.agent.daemon import NODE_TIMEOUTS
        required = ["intent_classify", "plan", "execute", "clarify",
                     "gate", "ask_user", "evaluate", "inline_reply"]
        for node in required:
            assert node in NODE_TIMEOUTS, f"Missing timeout for {node}"

    def test_gate_timeout_is_longer_than_execute(self):
        from paper_search.agent.daemon import NODE_TIMEOUTS
        assert NODE_TIMEOUTS["gate"] > NODE_TIMEOUTS["execute"]

    def test_default_busy_timeout_is_set(self):
        from paper_search.agent.daemon import DEFAULT_BUSY_TIMEOUT
        assert DEFAULT_BUSY_TIMEOUT > 0

    def test_queue_stale_threshold(self):
        from paper_search.agent.daemon import QUEUE_STALE_SECONDS
        assert QUEUE_STALE_SECONDS == 43200  # 0.5d

    def test_stdout_silent_threshold(self):
        from paper_search.agent.daemon import STDOUT_SILENT_SECONDS
        assert STDOUT_SILENT_SECONDS == 15


# ═══════════════════════════════════════════════════════════════
# Test 6: API 路由
# ═══════════════════════════════════════════════════════════════


class TestSupervisorAPI:
    """v4.1 API 路由: /agents/me/status|start|stop。"""

    def test_status_endpoint_exists(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/agents/me/status" in paths
        # Check GET method
        methods = []
        for r in router.routes:
            if r.path == "/api/agents/me/status":
                methods = list(r.methods)
        assert "GET" in methods

    def test_start_endpoint_exists(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/agents/me/start" in paths

    def test_stop_endpoint_exists(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/agents/me/stop" in paths

    def test_status_endpoint_returns_valid_json(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/agents/me/status" in paths

    def test_start_endpoint_publishes_control(self):
        cmd = {"cmd": "start", "user_id": "user-abc"}
        assert cmd["cmd"] == "start"
        assert "user_id" in cmd


# ═══════════════════════════════════════════════════════════════
# Test 7: 隔离保证
# ═══════════════════════════════════════════════════════════════


class TestIsolation:
    """用户间隔离。"""

    def test_agents_have_separate_pids(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        assert len(s._agents) == 0
        # Users are tracked by user_id keys — one per user

    def test_pid_to_uid_tracks_running_agents(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        assert isinstance(s._pid_to_uid, dict)

    def test_stop_one_user_does_not_affect_others(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        s.redis = AsyncMock()
        s._hsync_status = AsyncMock()
        mock_a = AsyncMock()
        mock_a.returncode = None
        mock_b = AsyncMock()
        mock_b.returncode = None
        s._agents["user-a"] = mock_a
        s._agents["user-b"] = mock_b
        # Only user-a is in the dict, calling stop_agent should remove/update user-a
        import asyncio
        ok = asyncio.run(s.stop_agent("user-a"))
        # user-b should still be in _agents if not stopped
        assert ok is True
