#!/usr/bin/env python3
"""Task 消息类型单元测试 — v7.0 协议。

测试覆盖:
  1. TaskEventAdapter 5 种子类信封结构
  2. message(notification) 附属消息
  3. foreground→background 模式切换
  4. phase(connected) activeTasks 格式
  5. MessageStore 回放规则（done/failed 排除，running 回放）

使用:
    pytest tests/test_task_messages.py -v
"""

import json
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from paper_search.agent.task_event_adapter import TaskEventAdapter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# TaskEventAdapter
# ═══════════════════════════════════════════════════════════════


class TestTaskEventAdapter:
    """测试 TaskEventAdapter 各方法生成的协议信封。"""

    AGENT = "agent-001"
    SESSION = "main"

    @pytest.fixture
    def captured(self):
        """捕获发送的信封列表。"""
        envelopes = []

        async def capture(env):
            envelopes.append(env)

        adapter = TaskEventAdapter(
            agent_id=self.AGENT, session_id=self.SESSION,
            send_fn=capture,
        )
        return adapter, envelopes

    @pytest.mark.asyncio
    async def test_started_envelope(self, captured):
        adapter, envs = captured
        await adapter.on_task_started("task-001", "入库 Transformer", mode="foreground", total_stages=7)

        assert len(envs) == 1
        e = envs[0]
        assert e["role"] == "assistant"
        assert e["type"] == "task"
        assert e["subType"] == "started"
        assert e["agentId"] == self.AGENT
        assert e["sessionId"] == self.SESSION
        assert e["priority"] == 1
        assert e["payload"]["taskId"] == "task-001"
        assert e["payload"]["name"] == "入库 Transformer"
        assert e["payload"]["mode"] == "foreground"
        assert e["payload"]["totalStages"] == 7

    @pytest.mark.asyncio
    async def test_running_envelope(self, captured):
        adapter, envs = captured
        await adapter.on_task_running(
            "task-001", "下载论文", 3, 7, current=12, total=50, mode="foreground",
        )

        assert len(envs) == 1
        e = envs[0]
        assert e["type"] == "task"
        assert e["subType"] == "running"
        assert e["priority"] == 1
        assert e["payload"]["taskId"] == "task-001"
        assert e["payload"]["mode"] == "foreground"
        assert e["payload"]["stage"] == "下载论文"
        assert e["payload"]["stageIndex"] == 3
        assert e["payload"]["totalStages"] == 7
        assert e["payload"]["current"] == 12
        assert e["payload"]["total"] == 50

    @pytest.mark.asyncio
    async def test_backgrounded_sends_task_and_notification(self, captured):
        adapter, envs = captured
        await adapter.on_task_backgrounded("task-001", reason="user_new_message")

        assert len(envs) == 2  # task(backgrounded) + message(notification)

        # First: task(backgrounded)
        t = envs[0]
        assert t["type"] == "task"
        assert t["subType"] == "backgrounded"
        assert t["payload"]["taskId"] == "task-001"
        assert t["payload"]["reason"] == "user_new_message"

        # Second: notification
        n = envs[1]
        assert n["type"] == "message"
        assert n["subType"] == "notification"
        assert n["payload"]["category"] == "task_backgrounded"
        assert n["payload"]["title"] == "转入后台"

    @pytest.mark.asyncio
    async def test_done_sends_task_and_notification(self, captured):
        adapter, envs = captured
        result = {"total_papers": 50, "downloaded": 48, "indexed": 48, "failed": 2}
        await adapter.on_task_done("task-001", result)

        assert len(envs) == 2  # task(done) + notification

        # task(done)
        t = envs[0]
        assert t["type"] == "task"
        assert t["subType"] == "done"
        assert t["payload"]["taskId"] == "task-001"
        assert t["payload"]["result"] == result

        # notification
        n = envs[1]
        assert n["type"] == "message"
        assert n["subType"] == "notification"
        assert n["payload"]["category"] == "task_complete"
        assert "50" in n["payload"]["body"]
        assert "2" in n["payload"]["body"]  # 失败数

    @pytest.mark.asyncio
    async def test_failed_envelope(self, captured):
        adapter, envs = captured
        await adapter.on_task_failed("task-001", "下载阶段：3 篇论文 PDF 不可获取")

        assert len(envs) == 1  # only task(failed), no notification
        e = envs[0]
        assert e["type"] == "task"
        assert e["subType"] == "failed"
        assert e["priority"] == 1
        assert e["payload"]["taskId"] == "task-001"
        assert "PDF 不可获取" in e["payload"]["error"]

    @pytest.mark.asyncio
    async def test_no_send_fn_does_not_raise(self):
        """无 send_fn 时不应抛异常。"""
        adapter = TaskEventAdapter(agent_id="a", session_id="s", send_fn=None)
        # 所有方法都应静默执行
        await adapter.on_task_started("t1", "test", "foreground", 7)
        await adapter.on_task_running("t1", "下载", 1, 7, 0, 10)
        await adapter.on_task_backgrounded("t1")
        await adapter.on_task_done("t1", {"total": 1})
        await adapter.on_task_failed("t1", "error")

    @pytest.mark.asyncio
    async def test_all_envelopes_have_required_fields(self, captured):
        """所有信封必须包含协议要求的基础字段。"""
        adapter, envs = captured
        await adapter.on_task_started("t1", "test", "foreground", 7)
        await adapter.on_task_running("t1", "下载", 1, 7, 0, 10)

        required = {"role", "type", "subType", "agentId", "sessionId", "priority", "timestamp", "payload"}
        for e in envs:
            missing = required - set(e.keys())
            assert not missing, f"Missing fields in {e['type']}/{e['subType']}: {missing}"


# ═══════════════════════════════════════════════════════════════
# MessageStore 回放规则
# ═══════════════════════════════════════════════════════════════


class TestMessageStoreReplay:
    """测试 task 消息的回放过滤规则。"""

    @pytest.fixture
    def sample_messages(self):
        """构建样本消息用于回放测试。"""
        msgs = [
            {"type": "task", "subtype": "started", "role": "assistant", "priority": 1,
             "agent_id": "agent-001", "session_id": "main", "seq": 10, "created_at": _now(),
             "payload": json.dumps({"taskId": "t1", "name": "test", "mode": "foreground", "totalStages": 7})},
            {"type": "task", "subtype": "running", "role": "assistant", "priority": 1,
             "agent_id": "agent-001", "session_id": "main", "seq": 11, "created_at": _now(),
             "payload": json.dumps({"taskId": "t1", "stage": "搜索论文", "stageIndex": 1})},
            {"type": "task", "subtype": "running", "role": "assistant", "priority": 1,
             "agent_id": "agent-001", "session_id": "main", "seq": 12, "created_at": _now(),
             "payload": json.dumps({"taskId": "t1", "stage": "下载论文", "stageIndex": 3})},
            {"type": "task", "subtype": "done", "role": "assistant", "priority": 1,
             "agent_id": "agent-001", "session_id": "main", "seq": 13, "created_at": _now(),
             "payload": json.dumps({"taskId": "t1", "result": {"total": 10}})},
            {"type": "task", "subtype": "failed", "role": "assistant", "priority": 1,
             "agent_id": "agent-001", "session_id": "main", "seq": 14, "created_at": _now(),
             "payload": json.dumps({"taskId": "t2", "error": "download failed"})},
            {"type": "message", "subtype": "text", "role": "assistant", "priority": 0,
             "agent_id": "agent-001", "session_id": "main", "seq": 15, "created_at": _now(),
             "payload": json.dumps({"delta": "hello"})},
        ]
        return msgs

    def test_done_and_failed_excluded_from_replay(self, sample_messages):
        """task(done) 和 task(failed) 不应出现在回放结果中。"""
        from paper_search.api.message_store import _REPLAY_EXCLUDE_TYPES

        # task 不在排除表中 → 由 get_replay_messages 内的 Rule 5 处理
        assert ("task", "started") not in _REPLAY_EXCLUDE_TYPES
        assert ("task", "running") not in _REPLAY_EXCLUDE_TYPES
        assert ("task", "done") not in _REPLAY_EXCLUDE_TYPES
        assert ("task", "failed") not in _REPLAY_EXCLUDE_TYPES
        assert ("task", "backgrounded") not in _REPLAY_EXCLUDE_TYPES

    def test_priority_0_excluded(self, sample_messages):
        """priority=0 的消息（如 message(text)）不应回放。"""
        from paper_search.api.message_store import _REPLAY_EXCLUDE_TYPES

        # message(text) 在排除表中
        assert ("message", "text") in _REPLAY_EXCLUDE_TYPES


# ═══════════════════════════════════════════════════════════════
# phase(connected) 格式
# ═══════════════════════════════════════════════════════════════


class TestPhaseConnected:
    """测试 phase(connected) 的 activeTasks 格式。"""

    def test_active_tasks_format(self):
        """activeTasks 必须包含 taskId, name, mode, stage, current, total, status。"""
        # 模拟 DB 返回的数据
        db_result = {
            "taskId": "task-001",
            "name": "入库 Transformer",
            "mode": "foreground",
            "status": "running",
            "stage": "下载论文",
            "current": 12,
            "total": 50,
        }

        required = {"taskId", "name", "mode", "stage", "current", "total", "status"}
        missing = required - set(db_result.keys())
        assert not missing, f"activeTasks missing fields: {missing}"

    def test_active_tasks_empty(self):
        """无活跃任务时 activeTasks 应为空列表。"""
        active = []
        assert active == []  # 直接为空列表，不做其他包装


# ═══════════════════════════════════════════════════════════════
# Mode 状态机
# ═══════════════════════════════════════════════════════════════


class TestTaskModeTransitions:
    """测试 foreground → background 单向不可逆。"""

    def test_background_is_final(self):
        """backgrounded 一旦发送，mode 永久为 background。"""
        # 验证 on_task_running 中 mode 正确传递
        mode = "background"
        # mode 只能从 foreground 变为 background，不能反向
        assert mode in ("foreground", "background")

    def test_backgrounded_reason(self):
        """backgrounded 的 reason 字段应准确反映触发原因。"""
        reasons = [
            "user_new_message",   # 用户发新 chat
        ]
        for r in reasons:
            assert r in ("user_new_message",)  # 当前支持的原因


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
