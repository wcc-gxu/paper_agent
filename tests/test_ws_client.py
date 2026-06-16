#!/usr/bin/env python3
"""WebSocket 协议集成测试 — 模拟 iOS 客户端。

测试覆盖:
  1. Handshake: connect -> chat(seq=1) -> phase(connected)
  2. Heartbeat: ping -> pong
  3. Error: invalid JSON -> error
  4. Error: non-handshake first -> error
  5. Chat flow: chat -> phase(clarify) / phase(plan) / phase(execute) -> reply

使用:
    # 先启动服务器:
    uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000

    # 运行测试:
    python tests/test_ws_client.py
    python tests/test_ws_client.py --host ws://localhost:8000
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

import websockets


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestWSClient:
    """模拟 iOS WebSocket 客户端。"""

    def __init__(self, host: str = "ws://localhost:8000",
                 agent_id: str = "agent-001",
                 session_id: str = "test-session"):
        self.url = f"{host}/ws/chat/{agent_id}/{session_id}"
        self.agent_id = agent_id
        self.session_id = session_id
        self.seq = 0
        self.ws = None
        self.handshake_done = False
        self.passed = 0
        self.failed = 0

    def _ok(self, msg: str):
        self.passed += 1
        print(f"  [PASS] {msg}")

    def _fail(self, msg: str):
        self.failed += 1
        print(f"  [FAIL] {msg}")

    async def connect(self) -> dict:
        """连接并握手。返回 phase(connected) 消息。"""
        self.ws = await websockets.connect(self.url)
        print(f"[CONNECT] {self.url}")

        # Send message(chat, seq=1)
        self.seq = 1
        await self.send({
            "role": "user", "type": "message", "subType": "chat",
            "agentId": self.agent_id, "sessionId": self.session_id,
            "seq": self.seq, "priority": 1,
            "timestamp": _now(),
            "payload": {"content": "Hello", "ios_tools": []},
        })

        # Wait for phase(connected)
        resp = await self.receive(timeout=10)
        if resp.get("type") == "phase" and resp.get("subType") == "connected":
            self.handshake_done = True
            self._ok(f"Handshake: phase(connected) — title={resp['payload'].get('sessionTitle')}, history={resp['payload'].get('historyCount')}")
            return resp
        if resp.get("type") == "error":
            self._fail(f"Handshake error: {resp.get('subType')} — {resp.get('payload', {}).get('message', '')}")
        else:
            self._fail(f"Handshake failed: expected phase(connected), got {resp.get('type')}({resp.get('subType')})")
        return resp

    async def send(self, msg: dict):
        """发送 JSON 消息。"""
        text = json.dumps(msg, ensure_ascii=False)
        print(f"[SEND] {msg['type']}({msg.get('subType','')}) seq={msg.get('seq',0)}")
        await self.ws.send(text)

    async def receive(self, timeout: float = 5.0) -> dict:
        """接收 JSON 消息。"""
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        print(f"[RECV] {msg['type']}({msg.get('subType','')}) priority={msg.get('priority')}")
        return msg

    async def heartbeat_test(self):
        """测试心跳 ping/pong。"""
        print("\n[TEST] Heartbeat...")
        await self.send({
            "role": "user", "type": "heartbeat", "subType": "ping",
            "agentId": self.agent_id, "sessionId": self.session_id,
            "seq": 0, "priority": 0, "timestamp": _now(), "payload": {},
        })
        resp = await self.receive(timeout=5)
        if resp.get("type") == "heartbeat" and resp.get("subType") == "pong":
            self._ok("Heartbeat ping->pong")
        else:
            self._fail(f"Heartbeat: expected pong, got {resp.get('type')}({resp.get('subType')})")

    async def error_invalid_json_test(self):
        """测试非法 JSON -> error。"""
        print("\n[TEST] Invalid JSON...")
        await self.ws.send("not valid json{{{")
        resp = await self.receive(timeout=5)
        if resp.get("type") == "error":
            self._ok(f"Invalid JSON -> error({resp.get('subType')})")
        else:
            self._fail(f"Expected error, got {resp.get('type')}")

    async def error_handshake_violation_test(self):
        """测试握手前非 chat 消息 -> error。"""
        print("\n[TEST] Handshake violation...")
        # 创建新的短连接
        test_ws = await websockets.connect(self.url + "-violation")
        await test_ws.send(json.dumps({
            "role": "user", "type": "heartbeat", "subType": "ping",
            "agentId": self.agent_id, "sessionId": self.session_id + "-violation",
            "seq": 0, "priority": 0, "timestamp": _now(), "payload": {},
        }))
        try:
            raw = await asyncio.wait_for(test_ws.recv(), timeout=5)
            resp = json.loads(raw)
            if resp.get("type") == "error":
                self._ok(f"Handshake violation -> error({resp.get('subType')})")
            else:
                self._fail(f"Expected error, got {resp.get('type')}({resp.get('subType')})")
        except asyncio.TimeoutError:
            self._fail("No response to handshake violation")
        finally:
            await test_ws.close()

    async def chat_flow_test(self):
        """测试完整对话流。"""
        print("\n[TEST] Chat flow...")
        self.seq += 1
        await self.send({
            "role": "user", "type": "message", "subType": "chat",
            "agentId": self.agent_id, "sessionId": self.session_id,
            "seq": self.seq, "priority": 1,
            "timestamp": _now(),
            "payload": {"content": "search for transformer attention mechanism papers in AI safety"},
        })

        # 收集响应，直到收到 reply 或 review(plan) 或 phase(done)
        received_review = False
        received_plan = False
        for attempt in range(50):
            try:
                resp = await self.receive(timeout=15)
            except asyncio.TimeoutError:
                print("  [INFO] No more messages (timeout)")
                break

            t = resp.get("type", "")
            s = resp.get("subType", "")

            if t == "thinking":
                self._ok("Got thinking stream (or phase)")
            elif t == "phase" and s == "clarify":
                self._ok("Got phase(clarify)")
            elif t == "review" and s == "clarify":
                self._ok(f"Got review(clarify) with {len(resp.get('payload',{}).get('questions',[]))} questions")
                received_review = True
                # Answer clarification
                await self.send({
                    "role": "user", "type": "review", "subType": "clarify",
                    "agentId": self.agent_id, "sessionId": self.session_id,
                    "seq": 0, "priority": 2, "timestamp": _now(),
                    "payload": {"answers": [{"question_id": "q1", "answer": "AI safety"}]},
                })
            elif t == "review" and s == "plan":
                self._ok(f"Got review(plan) with {len(resp.get('payload',{}).get('steps',[]))} steps")
                received_plan = True
                # Confirm plan
                await self.send({
                    "role": "user", "type": "review", "subType": "plan",
                    "agentId": self.agent_id, "sessionId": self.session_id,
                    "seq": 0, "priority": 2, "timestamp": _now(),
                    "payload": {"taskId": resp["payload"].get("taskId", ""), "confirmed": True},
                })
            elif t == "phase" and s == "execute":
                self._ok("Got phase(execute)")
            elif t == "tool" and s == "server":
                self._ok(f"Got tool(server): {resp.get('payload',{}).get('name','')} [{resp.get('payload',{}).get('status','')}]")
            elif t == "message" and s == "reply":
                self._ok(f"Got message(reply): {resp.get('payload',{}).get('content','')[:80]}...")
                break
            elif t == "phase" and s == "done":
                self._ok("Got phase(done)")
                break
            elif t == "message" and s == "text":
                pass  # streaming text, expected
            elif t == "error":
                self._fail(f"Got error: {s} — {resp.get('payload',{}).get('message','')}")
                break
            else:
                print(f"  [INFO] {t}({s})")

        if received_review or received_plan:
            self._ok("Chat flow completed with plan phase")
        else:
            print("  [INFO] Chat flow response collected (may skip clarify if intent is clear)")

    async def run_all(self):
        print("=" * 60)
        print(f"WS Protocol Test: {self.url}")
        print("=" * 60)

        # 1. Handshake
        await self.connect()

        # 2. Heartbeat
        await self.heartbeat_test()

        # 3. Error: Invalid JSON
        await self.error_invalid_json_test()

        # 4. Error: Handshake violation
        await self.error_handshake_violation_test()

        # 5. Chat flow
        await self.chat_flow_test()

        # Summary
        print("\n" + "=" * 60)
        print(f"RESULTS: {self.passed} passed, {self.failed} failed")
        print("=" * 60)

        if self.ws:
            await self.ws.close()

        return self.failed == 0


async def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000"
    client = TestWSClient(host=host)
    ok = await client.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
