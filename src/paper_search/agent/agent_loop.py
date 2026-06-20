"""AgentLoop — WebSocket 驱动的 LLM Agent Loop (v9.0 Protocol)。

Loop:
  1. BRPOP agent:ws:{agent_id} → 阻塞等待用户消息
  2. Drain 所有积压消息 → 合并上下文
  3. LLM chat_with_tools → thinking 流式推送 → text 完整回复
  4. 并行执行 tool calls (子Agent / CLI / iOS / ask_user)
  5. 等结果 (Pub/Sub + WS 回复)
  6. 结果回报 LLM → 回到步骤 3
  7. GOTO 1

v9.0 changes:
  - review 类型移除，用 tool/ask_user_question 替代
  - phase 类型移除，状态由消息序列隐式表达
  - message/thinking 流式思考 + message/text 完整回复
  - tool 类型覆盖子Agent进度和结果
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentLoop:
    """WebSocket 驱动的 LLM Agent Loop。

    不阻塞 — loop 内部只在 BRPOP 处等待用户消息。
    PlanGraph 被 LLM tool-calling 替代。
    """

    def __init__(
        self,
        agent_id: str = "agent-001",
        redis_url: str = "redis://localhost:6379/0",
        llm=None,
        db=None,
        tools: list = None,
    ):
        self._agent_id = agent_id
        self._redis_url = redis_url
        self._llm = llm
        self._db = db
        self._tools = tools or []
        self._redis = None

    # ── Redis 连接 (惰性) ────────────────────────────────

    @property
    def redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True
            )
        return self._redis

    # ── Main Loop ─────────────────────────────────────────

    async def run(self):
        """主循环 — 阻塞等待 WS 消息，处理，回复，重复。"""
        ws_queue = f"agent:ws:{self._agent_id}"

        logger.info(f"AgentLoop started: agent={self._agent_id}")

        while True:
            # ═══ STEP 1: 阻塞等待 WS 消息 ═══
            try:
                raw = await self.redis.brpop(ws_queue, timeout=0)
            except Exception as e:
                logger.error(f"BRPOP error: {e}, retrying...")
                await asyncio.sleep(1)
                continue

            msg_list = [json.loads(raw[1])]

            # Drain 所有积压消息
            while True:
                raw = await self.redis.rpop(ws_queue)
                if raw is None:
                    break
                msg_list.append(json.loads(raw))

            # 合并消息
            session_id = msg_list[0].get("_session_id", "main")
            output_channel = f"agent:output:{self._agent_id}:{session_id}"
            context = self._merge_messages(msg_list)
            logger.info(f"Drained {len(msg_list)} WS messages, session={session_id}")

            # ═══ STEP 2-6: LLM 决策循环 ═══
            try:
                await self._process_round(context, session_id, output_channel)
            except Exception as e:
                logger.error(f"Agent round failed: {e}", exc_info=True)
                await self._push_output(output_channel, self._envelope(
                    session_id, "error", "TASK_FAILED", role="system",
                    payload={"message": str(e), "recoverable": True},
                ))
            # GOTO STEP 1

    # ── 一轮处理 ─────────────────────────────────────

    async def _process_round(
        self, initial_context: list[dict], session_id: str,
        output_channel: str,
    ):
        """处理一轮完整的 LLM 对话。

        thinking → tool calls 并行执行 → 结果回 LLM → 循环
        → 最终 message/text。
        """
        context = initial_context

        for _ in range(20):  # safety: max 20 tool-calling rounds
            # ── LLM 调用 (thinking 流式推送) ──
            thinking_parts = []
            response = await self._call_llm(
                context, output_channel, session_id, thinking_parts
            )

            # ── thinking done ──
            await self._push_output(output_channel, self._envelope(
                session_id, "message", "thinking", role="assistant",
                payload={"content": "", "done": True},
            ))

            if not response.get("tool_calls"):
                # LLM 最终回复 → message/text
                final_text = response.get("content", "")
                await self._push_output(output_channel, self._envelope(
                    session_id, "message", "text", role="assistant",
                    payload={"content": final_text},
                ))
                return

            # ── 并行执行 tool calls ──
            tool_results = await self._execute_tools(
                response["tool_calls"], session_id, output_channel
            )

            # ── 结果回报 LLM ──
            thinking_text = "\n".join(thinking_parts) if thinking_parts else ""
            context = context + [{
                "role": "assistant",
                "content": thinking_text,
                "tool_calls": response["tool_calls"],
            }]
            for tc_id, result in tool_results.items():
                context.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

    # ── LLM 调用 ─────────────────────────────────────

    async def _call_llm(
        self, context: list, output_channel: str,
        session_id: str, thinking_parts: list,
    ) -> dict:
        """调用 LLM。thinking 流式推送。"""
        if not self._llm:
            return {"content": "LLM not configured", "tool_calls": None}

        try:
            response = await self._llm.chat_with_tools(
                messages=context,
                tools=self._tools,
                max_tool_rounds=1,
            )
            content = getattr(response, "content", "") or ""
            tc_list = getattr(response, "tool_calls", None)

            # 推送 thinking（如果有思考内容）
            if content:
                thinking_parts.append(content)
                await self._push_output(output_channel, self._envelope(
                    session_id, "message", "thinking", role="assistant",
                    payload={"content": content, "done": False},
                ))

            return {
                "content": content,
                "tool_calls": list(tc_list) if tc_list else None,
            }
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {"content": f"LLM error: {e}", "tool_calls": None}

    # ── Tool 并行执行 ─────────────────────────────────

    async def _execute_tools(
        self, tool_calls: list, session_id: str,
        output_channel: str,
    ) -> dict[str, dict]:
        """并行执行所有 tool call，等待全部完成。"""
        if not tool_calls:
            return {}

        tasks = {}
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            tc_name = tc.get("name", "")
            tc_args = tc.get("arguments", {})

            if tc_name == "ask_user_question":
                tasks[tc_id] = self._tool_ask_user(tc_id, tc_args, session_id, output_channel)
            elif tc_name == "launch_sub_agent":
                tasks[tc_id] = self._tool_launch_sub_agent(tc_args, session_id, output_channel)
            elif tc_name.startswith("ios_"):
                tasks[tc_id] = self._tool_ios(tc_id, tc_args, session_id, output_channel)
            else:
                tasks[tc_id] = self._tool_cli(tc_name, tc_args, session_id)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            tc_id: (r if not isinstance(r, Exception) else {"error": str(r)})
            for tc_id, r in zip(tasks.keys(), results)
        }

    # ── Tool: ask_user_question ───────────────────────

    async def _tool_ask_user(
        self, tc_id: str, args: dict, session_id: str, output_channel: str,
    ):
        """tool/ask_user_question — 替代旧 review/clarify + review/plan。"""
        questions = args.get("questions", [
            {"id": "q1", "question": args.get("question", args.get("message", "请确认"))}
        ])

        await self._push_output(output_channel, self._envelope(
            session_id, "tool", "ask_user_question", role="assistant",
            payload={
                "id": tc_id,
                "questions": questions,
                "context": args.get("context", ""),
            },
        ))

        # 阻塞等待用户回答 (tool/ask_user_question, role=user)
        reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question")
        return {"answers": reply.get("answers", [])}

    # ── Tool: launch_sub_agent ────────────────────────

    async def _tool_launch_sub_agent(
        self, args: dict, session_id: str, output_channel: str,
    ):
        """tool/launch_sub_agent → tool/sub_agent_progress → tool/sub_agent_result。"""
        agent_type = args.get("agent_type", "ingest")
        user_query = args.get("query", args.get("description", ""))
        task_id = f"sub-{agent_type}-{_now().replace(':', '').replace('-', '')[:12]}"

        # 分发 Celery task
        from .celery_tasks import sub_agent_task
        sub_agent_task.delay(
            user_query=user_query,
            project_id=task_id,
            agent_task_id=task_id,
        )

        # 通知 iOS: 子Agent 已启动
        await self._push_output(output_channel, self._envelope(
            session_id, "tool", "launch_sub_agent", role="assistant",
            payload={
                "taskId": task_id,
                "agentType": agent_type,
                "query": user_query,
                "estimatedStages": 7,
            },
        ))

        # 订阅 Pub/Sub 监听进度 + 结果
        pubsub = self.redis.pubsub()
        report_channel = f"agent:reports:{task_id}"
        await pubsub.subscribe(report_channel)

        result = None
        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                except json.JSONDecodeError:
                    continue

                status = data.get("status", "")
                stage = data.get("stage", "")

                if status in ("done", "failed"):
                    result = data
                    await self._push_output(output_channel, self._envelope(
                        session_id, "tool", "sub_agent_result", role="system",
                        payload={
                            "taskId": task_id,
                            "agentType": agent_type,
                            "status": status,
                            "summary": data.get("summary", data.get("error", "")),
                            "result": data.get("result", data.get("extra", {})),
                        },
                    ))
                    break

                # progress
                await self._push_output(output_channel, self._envelope(
                    session_id, "tool", "sub_agent_progress", role="system",
                    payload={
                        "taskId": task_id,
                        "agentType": agent_type,
                        "stage": stage,
                        "current": data.get("paper_index", 0),
                        "total": data.get("paper_total", 0),
                        "message": f"{stage} {status}",
                    },
                ))
        finally:
            await pubsub.unsubscribe(report_channel)

        return {"task_id": task_id, "result": result or {}}

    # ── Tool: CLI ─────────────────────────────────────

    async def _tool_cli(self, tool_name: str, args: dict, session_id: str):
        """执行本地 CLI tool。关键节点通过 Pub/Sub 报告。"""
        from .tool_registry import ToolRegistry
        registry = ToolRegistry.get_instance()

        tool_func = registry.get_tool(tool_name)
        if not tool_func:
            return {"error": f"Unknown tool: {tool_name}"}

        await self._report_cli_progress(tool_name, session_id, "start")

        try:
            if asyncio.iscoroutinefunction(tool_func):
                result = await tool_func(**args)
            else:
                result = tool_func(**args)
        except Exception as e:
            await self._report_cli_progress(tool_name, session_id, "failed", {"error": str(e)})
            raise

        await self._report_cli_progress(tool_name, session_id, "done", {"result": str(result)[:500]})
        return result

    async def _report_cli_progress(self, tool_name: str, session_id: str,
                                    status: str, extra: dict = None):
        """CLI tool 关键节点 → Pub/Sub + TaskLogger。"""
        try:
            from .reporter import Reporter
            reporter = Reporter(self._redis_url, self._agent_id)
            reporter.publish_report(
                task_id=f"cli-{tool_name}", agent_type="cli", stage=tool_name,
                status=status, extra=extra or {},
            )
        except Exception as e:
            logger.debug(f"CLI report failed: {e}")

    # ── Tool: iOS ─────────────────────────────────────

    async def _tool_ios(
        self, tc_id: str, args: dict, session_id: str, output_channel: str,
    ):
        """tool/ios_request → 等 tool/result(role=tool)。"""
        tool_name = args.get("tool", args.get("name", "unknown"))

        await self._push_output(output_channel, self._envelope(
            session_id, "tool", "ios_request", role="assistant",
            payload={"id": tc_id, "name": tool_name, "input": args},
        ))

        reply = await self._wait_ws_reply(session_id, "tool", "result")
        return reply

    # ── Helpers ───────────────────────────────────────

    async def _wait_ws_reply(
        self, session_id: str, msg_type: str, msg_sub: str,
        timeout: float = 300,
    ) -> dict:
        """BRPOP 等待特定 session + type + subType 的用户回复。"""
        ws_queue = f"agent:ws:{self._agent_id}"
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {msg_type}/{msg_sub}")

            try:
                raw = await self.redis.brpop(ws_queue, timeout=int(min(remaining, 30)))
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if raw is None:
                continue

            msg = json.loads(raw[1])
            if msg.get("_session_id") != session_id:
                await self.redis.lpush(ws_queue, raw[1])
                await asyncio.sleep(0.1)
                continue

            got_type = msg.get("type", "")
            got_sub = msg.get("subType", "")
            if got_type == msg_type and got_sub == msg_sub:
                return msg.get("payload", {})

            # 不是预期的消息类型 → 放回去
            await self.redis.lpush(ws_queue, raw[1])
            await asyncio.sleep(0.1)

    async def _push_output(self, channel: str, envelope: dict):
        """推送消息到 iOS (Redis Pub/Sub → API Server → WS)。"""
        try:
            await self.redis.publish(
                channel,
                json.dumps(envelope, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning(f"Push output failed: {e}")

    def _envelope(self, session_id: str, msg_type: str, sub_type: str,
                  role: str = "assistant", **kwargs) -> dict:
        """构建 WebSocket 协议信封 (v9.0)。"""
        return {
            "type": msg_type,
            "subType": sub_type,
            "role": role,
            "agentId": self._agent_id,
            "sessionId": session_id,
            "timestamp": _now(),
        } | kwargs

    def _merge_messages(self, msg_list: list[dict]) -> list[dict]:
        """合并多条 WS 消息为 LLM context。"""
        user_contents = []
        for m in msg_list:
            payload = m.get("payload", {})
            content = payload.get("content", "")
            if content:
                user_contents.append(content)
        combined = "\n\n".join(user_contents)
        return [{"role": "user", "content": combined}]
