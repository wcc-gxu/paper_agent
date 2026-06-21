"""MainAgent — Phase 3: 新的主 Agent 实现。

5 节点的显式状态机（不用 LangGraph）:

  intent_classify → (business?)
       ├─ yes → scenario_plan → (needs_approval?)
       │            ├─ yes → ask_user (propose_plan) → execute_plan
       │            └─ no → execute_plan
       │                       ↓
       │                evaluate_completion → (satisfied?)
       │                       ├─ yes → END (message/text)
       │                       └─ no → execute_plan (loop, max 3 次)
       └─ no → inline_reply → END (message/text)

所有出站消息走 outbox（持久化 + Redis List → outbox_poller → WS / APNs）。
Phase 4 会在每个节点末尾写 agent_events 表用于 crash recovery。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .main_agent_prompts import (
    EVALUATE_COMPLETION_SYSTEM,
    INLINE_REPLY_SYSTEM,
    SCENARIOS,
    EvaluateCompletionResult,
    IntentClassifyResult,
    ScenarioPlanResult,
    ToolCallSpec,
    build_intent_classify_prompt,
    build_scenario_plan_prompt,
)
from .outbox import outbox_publish

logger = logging.getLogger(__name__)

# 最多 evaluate-execute 迭代次数（避免无限循环）
MAX_PLAN_ITERATIONS = 3
# 单个 tool / sub_agent 调用超时
TOOL_TIMEOUT_SEC = 5 * 60        # CLI 工具默认 5 分钟
SUB_AGENT_TIMEOUT_SEC = 30 * 60  # 子 Agent 默认 30 分钟
IOS_TIMEOUT_SEC = 2 * 60         # iOS 工具默认 2 分钟
ASK_USER_TIMEOUT_SEC = 30 * 60   # 等用户回答最长 30 分钟


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex[:16]}"


def _new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:10]}"


def _new_task_id(agent_type: str) -> str:
    return f"sub-{agent_type}-{uuid.uuid4().hex[:12]}"


# ═══════════════════════════════════════════════════════════════
# MainAgent
# ═══════════════════════════════════════════════════════════════


class MainAgent:
    """新主 Agent — 替代 v1 (AgentRunLoop+PlanGraph) 和 v2 (AgentLoop)。"""

    def __init__(
        self,
        agent_id: str = "agent-001",
        redis_url: str = "redis://localhost:6379/0",
        llm=None,
        db=None,
        memory=None,
        registry=None,
    ):
        self._agent_id = agent_id
        self._redis_url = redis_url
        self._llm = llm
        self._db = db
        self._memory = memory       # Phase 4 接入 MemoryManager
        self._registry = registry   # ToolRegistry
        self._redis = None
        # 当前正在处理的 correlation_id（每轮 BRPOP 重置）
        self._correlation_id: str = ""

    # ── Redis (惰性) ─────────────────────────────────────

    @property
    def redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    # ── 主循环 ─────────────────────────────────────────

    async def run(self):
        """主循环 — BRPOP 用户消息 → 5 节点流转 → 重复。

        启动时先做 crash recovery（事件源 replay 未完成的轮次）。
        """
        # Phase 4: 启动时恢复未完成的对话
        try:
            await self._recover_pending_turns()
        except Exception as e:
            logger.warning(f"_recover_pending_turns failed: {e}", exc_info=True)

        ws_queue = f"agent:ws:{self._agent_id}"
        parked_queue = f"agent:ws:{self._agent_id}:parked"

        logger.info(f"MainAgent started: agent={self._agent_id}")

        while True:
            try:
                raw = await self.redis.brpop(ws_queue, timeout=0)
            except Exception as e:
                logger.error(f"BRPOP error: {e}, retrying...")
                await asyncio.sleep(1)
                continue

            msg_list = [json.loads(raw[1])]

            # Drain 积压
            while True:
                more = await self.redis.rpop(ws_queue)
                if more is None:
                    break
                msg_list.append(json.loads(more))

            # 把上轮 _wait_ws_reply parked 的消息也并入（不丢用户输入）
            while True:
                more = await self.redis.rpop(parked_queue)
                if more is None:
                    break
                msg_list.append(json.loads(more))

            session_id = msg_list[0].get("_session_id", "main")
            user_content = self._combine_user_text(msg_list)

            if not user_content.strip():
                logger.info("Empty user message, skipping turn")
                continue

            # 一轮对话开始 — 分配 correlation_id
            self._correlation_id = _new_correlation_id()
            self._record_event(session_id, "turn_started", {
                "user_message": user_content[:500],
                "session_id": session_id,
            })
            logger.info(
                "🟢 TURN start | corr=%s sess=%s user=%r",
                self._correlation_id, session_id, user_content[:80],
            )

            try:
                await self._run_turn(session_id, user_content)
            except Exception as e:
                logger.error(f"MainAgent turn failed: {e}", exc_info=True)
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": str(e), "recoverable": True},
                                 priority_kind="urgent")
                self._record_event(session_id, "turn_completed",
                                   {"outcome": "errored", "error": str(e)})

    # ── 一轮对话调度 ───────────────────────────────────

    async def _run_turn(self, session_id: str, user_content: str):
        """节点 1 (intent_classify) → 分支到 inline_reply 或 scenario_plan。"""
        # 节点 1: 意图分类
        intent = await self._node_intent_classify(session_id, user_content)
        self._record_event(session_id, "intent_classified", intent.model_dump())

        if intent.intent_kind == "business" and intent.scenario_id:
            # 节点 2A: 业务场景规划
            await self._node_scenario_plan(session_id, user_content, intent)
        else:
            # 节点 2B: 闲聊 / meta / unsupported 直接回复
            await self._node_inline_reply(session_id, user_content, intent)

        # Phase 4: 把整轮对话同步到 MemGPT short_term
        # （final_text 已经被各分支写入 ws_messages，从那里取最后一条即可）
        try:
            row = self._db.conn.execute(
                """SELECT payload FROM ws_messages
                   WHERE agent_id=? AND session_id=? AND correlation_id=?
                     AND role='assistant' AND type='message' AND subtype='text'
                   ORDER BY id DESC LIMIT 1""",
                (self._agent_id, session_id, self._correlation_id),
            ).fetchone() if self._db else None
            assistant_text = ""
            if row:
                try:
                    p = json.loads(row["payload"] or "{}")
                    assistant_text = p.get("content", "")
                except Exception:
                    pass
            self._write_short_term(session_id, user_content, assistant_text)
        except Exception as e:
            logger.debug(f"short_term sync failed: {e}")

        self._record_event(session_id, "turn_completed", {"outcome": "done"})

    # ── 节点 1: intent_classify ────────────────────────

    async def _node_intent_classify(self, session_id: str, user_content: str) -> IntentClassifyResult:
        """LLM 把用户消息分类到 business/chat/meta/unsupported。"""
        history = self._build_history_context(session_id, limit=20)
        messages = history + [{"role": "user", "content": user_content}]
        system = build_intent_classify_prompt()

        try:
            data = await self._llm.chat_json(
                messages=messages,
                schema=IntentClassifyResult,
                temperature=0.1,
                system=system,
            )
        except Exception as e:
            logger.warning(f"intent_classify LLM failed: {e}, defaulting to chat")
            return IntentClassifyResult(
                intent_kind="chat", scenario_id=None,
                confidence=0.0, reasoning=f"LLM error: {e}",
            )

        try:
            result = IntentClassifyResult.model_validate(data)
        except Exception as e:
            logger.warning(f"intent_classify schema invalid: {e}, raw={data}")
            return IntentClassifyResult(
                intent_kind="chat", scenario_id=None,
                confidence=0.0, reasoning=f"Invalid schema: {e}",
            )

        # 置信度低 → 退化到 chat
        if result.intent_kind == "business" and result.confidence < 0.5:
            logger.info(f"Low confidence ({result.confidence}), treating as chat")
            result.intent_kind = "chat"
            result.scenario_id = None

        logger.info(
            "🧭 intent_classify | kind=%s scenario=%s conf=%.2f",
            result.intent_kind, result.scenario_id, result.confidence,
        )
        return result

    # ── 节点 2A: scenario_plan (business) ─────────────

    async def _node_scenario_plan(self, session_id: str, user_content: str,
                                   intent: IntentClassifyResult):
        """业务场景：生成结构化 plan，可能含 clarify/approval，最后执行。"""
        scenario_id = intent.scenario_id
        if not scenario_id or scenario_id not in SCENARIOS:
            await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                             payload={"message": f"未知场景 {scenario_id}"},
                             priority_kind="urgent")
            return

        # 迭代生成 plan：如果 needs_clarification，问完再调一次
        history = self._build_history_context(session_id, limit=20)
        messages = history + [{"role": "user", "content": user_content}]
        system = build_scenario_plan_prompt(scenario_id)
        plan: Optional[ScenarioPlanResult] = None

        for clarify_round in range(3):  # 最多 3 次澄清
            try:
                data = await self._llm.chat_json(
                    messages=messages, schema=ScenarioPlanResult,
                    temperature=0.2, system=system,
                )
                plan = ScenarioPlanResult.model_validate(data)
            except Exception as e:
                logger.warning(f"scenario_plan LLM/schema failed: {e}")
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": f"方案生成失败: {e}"},
                                 priority_kind="urgent")
                return

            self._record_event(session_id, "scenario_planned", plan.model_dump())
            logger.info(
                "📋 scenario_plan | sid=%s clarify=%s approval=%s tools=%d",
                plan.scenario_id, plan.needs_clarification, plan.needs_approval,
                len(plan.tools),
            )

            if plan.needs_clarification and plan.clarification_questions:
                # 发 ask_user_question，等用户回答
                msg_id = await self._push(session_id, "tool", "ask_user_question",
                                          "assistant", payload={
                                              "id": _new_call_id(),
                                              "questions": [q.model_dump()
                                                            for q in plan.clarification_questions],
                                              "context": plan.summary,
                                          })
                self._record_event(session_id, "clarification_requested",
                                   {"questions": [q.model_dump() for q in plan.clarification_questions],
                                    "msg_id": msg_id})
                reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question",
                                                  timeout=ASK_USER_TIMEOUT_SEC)
                if reply is None:
                    return  # 超时已发 error
                answers = reply.get("answers", [])
                self._record_event(session_id, "clarification_received",
                                   {"answers": answers})
                # 把答案追加到上下文，再问 LLM
                messages.append({
                    "role": "user",
                    "content": "针对你刚才的问题，我的回答是：" + json.dumps(answers, ensure_ascii=False),
                })
                continue

            break

        if plan is None:
            return

        # 需要审批 → propose_plan 卡片
        if plan.needs_approval:
            plan_msg_id = _new_call_id()
            await self._push(session_id, "tool", "propose_plan", "assistant",
                             payload={
                                 "id": plan_msg_id,
                                 "scenario_id": plan.scenario_id,
                                 "summary": plan.summary,
                                 "permissions": plan.permissions_required,
                                 "estimated_time_seconds": plan.estimated_time_seconds,
                                 "tools": [t.model_dump() for t in plan.tools],
                             })
            self._record_event(session_id, "plan_approval_requested",
                               {"plan_summary": plan.summary,
                                "permissions": plan.permissions_required,
                                "msg_id": plan_msg_id})
            reply = await self._wait_ws_reply(session_id, "tool", "propose_plan",
                                              timeout=ASK_USER_TIMEOUT_SEC)
            if reply is None:
                return
            approved = bool(reply.get("approved", False))
            if not approved:
                self._record_event(session_id, "plan_rejected",
                                   {"reason": reply.get("reason", ""),
                                    "msg_id": plan_msg_id})
                await self._push(session_id, "message", "text", "assistant",
                                 payload={"content": "好的，已取消该计划。如需调整请告诉我。"},
                                 priority_kind="high")
                return
            self._record_event(session_id, "plan_approved", {"msg_id": plan_msg_id})

        # 执行 + 评估循环
        await self._execute_with_evaluation(session_id, user_content, plan)

    async def _execute_with_evaluation(self, session_id: str,
                                        user_content: str,
                                        plan: ScenarioPlanResult):
        """节点 3 (execute_plan) ↔ 节点 4 (evaluate_completion) 循环。"""
        tools_to_run = list(plan.tools)
        all_results: dict[str, Any] = {}

        for iteration in range(MAX_PLAN_ITERATIONS):
            if not tools_to_run:
                break
            # 节点 3: 执行
            results = await self._node_execute_plan(session_id, tools_to_run)
            all_results.update(results)

            # 节点 4: 评估
            eval_res = await self._node_evaluate_completion(
                session_id, user_content, plan, all_results,
            )
            self._record_event(session_id, "completion_evaluated", eval_res.model_dump())

            if eval_res.satisfied:
                # 最终文本回复
                await self._push(session_id, "message", "text", "assistant",
                                 payload={"content": eval_res.final_message or "已完成。"},
                                 priority_kind="high")
                return

            if not eval_res.needs_more_tools:
                # 未满足但也没新工具 → 兜底回复
                await self._push(session_id, "message", "text", "assistant",
                                 payload={"content": eval_res.final_message
                                          or eval_res.reasoning
                                          or "目前还无法完全满足您的需求。"},
                                 priority_kind="high")
                return

            tools_to_run = list(eval_res.needs_more_tools)

        # 达到 MAX_PLAN_ITERATIONS
        logger.warning("Hit MAX_PLAN_ITERATIONS, forcing termination")
        await self._push(session_id, "message", "text", "assistant",
                         payload={"content": "我已经尝试了多轮但仍未完全满足您的需求，请您调整后再试。"},
                         priority_kind="high")

    # ── 节点 2B: inline_reply (chat/meta/unsupported) ──

    async def _node_inline_reply(self, session_id: str, user_content: str,
                                  intent: IntentClassifyResult):
        """非业务请求直接 LLM 回复（流式 thinking + text）。"""
        history = self._build_history_context(session_id, limit=30)
        messages = history + [{"role": "user", "content": user_content}]

        # 流式调用 chat_stream 推 thinking delta
        full_text = ""
        try:
            if hasattr(self._llm, "chat_stream"):
                async for chunk in self._llm.chat_stream(
                    messages=messages, system=INLINE_REPLY_SYSTEM, temperature=0.6,
                ):
                    # chat_stream yields {"type": "text_delta", "text": "..."} 等 dict
                    if isinstance(chunk, dict):
                        if chunk.get("type") != "text_delta":
                            continue
                        delta = chunk.get("text", "")
                    else:
                        # 兼容 mock 直接 yield str
                        delta = str(chunk) if chunk else ""
                    if not delta:
                        continue
                    full_text += delta
                    await self._push(session_id, "message", "thinking", "assistant",
                                     payload={"content": delta, "done": False},
                                     priority_kind="silent")
                # thinking done 信标
                await self._push(session_id, "message", "thinking", "assistant",
                                 payload={"content": "", "done": True},
                                 priority_kind="silent")
            else:
                # 不支持流 → 一次性
                resp = await self._llm.chat(
                    messages=messages, system=INLINE_REPLY_SYSTEM, temperature=0.6,
                )
                full_text = getattr(resp, "content", None) or str(resp)
        except Exception as e:
            logger.warning(f"inline_reply LLM failed: {e}")
            full_text = "抱歉，刚才出了点问题，能再说一次吗？"

        # 最终文本（高优先级 → APNs）
        await self._push(session_id, "message", "text", "assistant",
                         payload={"content": full_text.strip()},
                         priority_kind="high")
        self._record_event(session_id, "inline_reply_sent",
                           {"final_text": full_text[:500]})

    # ── 节点 3: execute_plan ───────────────────────────

    async def _node_execute_plan(self, session_id: str,
                                  tools_to_run: list[ToolCallSpec]) -> dict[str, Any]:
        """按 depends_on 拓扑排序分批，每批 asyncio.gather 并行。"""
        # 简单拓扑：依次找出当前可执行的 calls (depends_on 已满足)
        results: dict[str, Any] = {}
        remaining = {t.call_id: t for t in tools_to_run}

        while remaining:
            # 找出本批可跑的（依赖都已 completed）
            ready = [
                t for t in remaining.values()
                if all(dep in results for dep in t.depends_on)
            ]
            if not ready:
                # 循环依赖或依赖外部 → 强制把全部剩余的都标失败
                logger.warning(f"Unresolvable deps for {list(remaining.keys())}")
                for cid, t in remaining.items():
                    results[cid] = {"error": "unresolvable_dependencies",
                                    "depends_on": t.depends_on}
                break

            # 并发执行本批
            coros = [self._dispatch_one(session_id, t) for t in ready]
            batch_results = await asyncio.gather(*coros, return_exceptions=True)

            for spec, res in zip(ready, batch_results):
                if isinstance(res, Exception):
                    res = {"error": str(res)}
                results[spec.call_id] = res
                remaining.pop(spec.call_id, None)

        return results

    async def _dispatch_one(self, session_id: str, spec: ToolCallSpec) -> Any:
        """根据 kind 把单个 tool call 派发到对应 handler。"""
        self._record_event(session_id, "tool_call_started", {
            "call_id": spec.call_id,
            "kind": spec.kind,
            "name": spec.name,
            "arguments": spec.arguments,
        })
        try:
            if spec.kind == "sub_agent":
                res = await self._handle_sub_agent(session_id, spec)
            elif spec.kind == "ios_tool":
                res = await self._handle_ios_tool(session_id, spec)
            elif spec.kind == "ask_user":
                res = await self._handle_ask_user(session_id, spec)
            else:  # tool
                res = await self._handle_cli_tool(session_id, spec)

            self._record_event(session_id, "tool_call_completed", {
                "call_id": spec.call_id, "result": _truncate(res),
            })
            return res
        except Exception as e:
            logger.warning(f"Tool {spec.name} failed: {e}", exc_info=True)
            self._record_event(session_id, "tool_call_failed", {
                "call_id": spec.call_id, "error": str(e),
            })
            return {"error": str(e)}

    # ── Tool handlers ─────────────────────────────────

    async def _handle_sub_agent(self, session_id: str, spec: ToolCallSpec) -> dict:
        """启动 Celery 子 Agent，订阅 agent:reports:{task_id}，等 agent_done/failed。"""
        agent_type = spec.arguments.get("agent_type", spec.name)
        user_query = (spec.arguments.get("query")
                      or spec.arguments.get("user_query")
                      or spec.arguments.get("description") or "")
        task_id = _new_task_id(agent_type)

        # 启动 Celery
        from .celery_tasks import sub_agent_task
        try:
            sub_agent_task.delay(
                user_query=user_query,
                project_id=task_id,
                agent_task_id=task_id,
            )
        except Exception as e:
            return {"error": f"Failed to dispatch celery: {e}"}

        # 推送 sub_request
        await self._push(session_id, "tool", "sub_request", "assistant", payload={
            "taskId": task_id, "name": agent_type, "label": spec.name,
            "query": user_query, "estimatedStages": 7,
        })

        # 订阅 reports channel
        pubsub = self.redis.pubsub()
        report_channel = f"agent:reports:{task_id}"
        await pubsub.subscribe(report_channel)

        result: dict[str, Any] = {}
        deadline = asyncio.get_event_loop().time() + SUB_AGENT_TIMEOUT_SEC
        last_msg_at = asyncio.get_event_loop().time()
        STALL_SEC = 60

        try:
            while True:
                now = asyncio.get_event_loop().time()
                if now >= deadline:
                    await self._push(session_id, "tool", "sub_result", "system", payload={
                        "taskId": task_id, "name": agent_type, "status": "failed",
                        "summary": "超时", "result": {},
                    })
                    return {"error": "timeout", "task_id": task_id}

                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=min(STALL_SEC, deadline - now),
                    )
                except asyncio.TimeoutError:
                    if now - last_msg_at >= STALL_SEC:
                        # 心跳缺失：推送 stalled 进度，但继续等
                        await self._push(session_id, "tool", "sub_progress", "system",
                                         payload={"taskId": task_id, "name": agent_type,
                                                  "stage": "stalled", "current": 0, "total": 0,
                                                  "message": f"子Agent 心跳缺失 ({STALL_SEC}s)"})
                        last_msg_at = now
                    continue

                if msg is None or msg.get("type") != "message":
                    continue

                last_msg_at = now
                try:
                    data = json.loads(msg["data"])
                except json.JSONDecodeError:
                    continue

                if (data.get("type") == "lifecycle"
                        and data.get("lifecycle") in ("agent_done", "agent_failed")):
                    final_status = "done" if data["lifecycle"] == "agent_done" else "failed"
                    result = data.get("result", {}) or {}
                    await self._push(session_id, "tool", "sub_result", "system", payload={
                        "taskId": task_id, "name": agent_type, "status": final_status,
                        "summary": data.get("summary", ""), "result": result,
                    })
                    if final_status == "failed":
                        return {"error": data.get("error", ""),
                                "task_id": task_id, "result": result}
                    return {"task_id": task_id, "result": result}

                # 否则是 progress
                await self._push(session_id, "tool", "sub_progress", "system", payload={
                    "taskId": task_id, "name": agent_type,
                    "stage": data.get("stage", ""),
                    "current": data.get("paper_index", 0),
                    "total": data.get("paper_total", 0),
                    "message": f"{data.get('stage', '')} {data.get('status', '')}",
                })
        finally:
            try:
                await pubsub.unsubscribe(report_channel)
                await pubsub.close()
            except Exception:
                pass

    async def _handle_cli_tool(self, session_id: str, spec: ToolCallSpec) -> Any:
        """本地 CLI 工具调用。同步函数放到线程池。"""
        if self._registry is None:
            return {"error": "registry not configured"}
        tool = self._registry.get(spec.name)
        if tool is None:
            return {"error": f"Unknown tool: {spec.name}"}

        # StructuredTool 有 .func / .coroutine
        func = getattr(tool, "func", None)
        coroutine = getattr(tool, "coroutine", None)
        if coroutine is not None:
            return await asyncio.wait_for(coroutine(**spec.arguments), timeout=TOOL_TIMEOUT_SEC)
        if func is None:
            return {"error": f"Tool {spec.name} has no callable"}

        import functools
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(func, **spec.arguments)),
            timeout=TOOL_TIMEOUT_SEC,
        )

    async def _handle_ios_tool(self, session_id: str, spec: ToolCallSpec) -> Any:
        """iOS 端工具：发 ios_request → 等 ios_result。"""
        call_id = spec.call_id
        await self._push(session_id, "tool", "ios_request", "assistant", payload={
            "id": call_id, "name": spec.name, "input": spec.arguments,
        })
        reply = await self._wait_ws_reply(session_id, "tool", "ios_result",
                                          timeout=IOS_TIMEOUT_SEC,
                                          match_fn=lambda p: p.get("tool_call_id") == call_id)
        if reply is None:
            return {"error": "ios_tool timeout"}
        return reply.get("content", reply)

    async def _handle_ask_user(self, session_id: str, spec: ToolCallSpec) -> Any:
        """LLM 临时插入的提问（与 needs_clarification 不同：plan 期间不会出现）。"""
        call_id = spec.call_id
        questions = spec.arguments.get("questions", [{
            "id": "q1", "question": spec.arguments.get("question", "请确认"),
            "type": "open", "options": [],
        }])
        await self._push(session_id, "tool", "ask_user_question", "assistant", payload={
            "id": call_id, "questions": questions,
            "context": spec.arguments.get("context", ""),
        })
        reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question",
                                          timeout=ASK_USER_TIMEOUT_SEC)
        if reply is None:
            return {"error": "ask_user timeout"}
        return {"answers": reply.get("answers", [])}

    # ── 节点 4: evaluate_completion ────────────────────

    async def _node_evaluate_completion(self, session_id: str, user_content: str,
                                         plan: ScenarioPlanResult,
                                         results: dict[str, Any]) -> EvaluateCompletionResult:
        """LLM 看到本轮 tool 结果，判断是否满足用户需求。"""
        # 构造 messages
        history = self._build_history_context(session_id, limit=10)
        results_brief = {cid: _truncate(r, 800) for cid, r in results.items()}
        prompt_user = (
            f"用户原始请求: {user_content}\n\n"
            f"本场景 plan summary: {plan.summary}\n\n"
            f"已执行工具结果（JSON）:\n{json.dumps(results_brief, ensure_ascii=False, indent=2)}\n\n"
            "请判断是否满足用户需求。"
        )
        messages = history + [{"role": "user", "content": prompt_user}]

        try:
            data = await self._llm.chat_json(
                messages=messages, schema=EvaluateCompletionResult,
                temperature=0.2, system=EVALUATE_COMPLETION_SYSTEM,
            )
            return EvaluateCompletionResult.model_validate(data)
        except Exception as e:
            logger.warning(f"evaluate_completion LLM/schema failed: {e}")
            return EvaluateCompletionResult(
                satisfied=True, reasoning=f"评估失败 ({e})，按已完成处理",
                needs_more_tools=[], final_message="任务已执行完成。",
            )

    # ── Helpers ────────────────────────────────────────

    def _combine_user_text(self, msg_list: list[dict]) -> str:
        parts = []
        for m in msg_list:
            payload = m.get("payload") or {}
            content = payload.get("content")
            if content:
                parts.append(content)
        return "\n\n".join(parts)

    def _build_history_context(self, session_id: str, limit: int = 20) -> list[dict]:
        """Phase 4: 优先用 MemoryManager.short_term 拿 sliding window；
        若 memory 不可用则 fallback 到 ws_messages 表（用于冷启动或测试）。

        最终拼接：
            [MetaMemory 用户偏好快照 (system)] + [ShortTerm 最近 N 条 chat]
        """
        out: list[dict] = []

        # 1. 注入用户偏好（MetaMemory 偏好 + LongTerm profile）
        if self._memory is not None:
            try:
                pref_lines = []
                # MetaMemory: 用户偏好（带置信度阈值）
                meta = getattr(self._memory, "meta", None)
                if meta and hasattr(meta, "_db"):
                    try:
                        rows = meta._db.conn.execute(
                            "SELECT key, value, confidence FROM user_preferences "
                            "WHERE confidence >= 0.3 ORDER BY confidence DESC LIMIT 20",
                        ).fetchall()
                        for r in rows:
                            try:
                                v = json.loads(r["value"])
                            except (ValueError, TypeError):
                                v = r["value"]
                            pref_lines.append(f"- {r['key']}: {v}")
                    except Exception:
                        pass
                # LongTermMemory: 用户画像字段
                long_term = getattr(self._memory, "long_term", None)
                if long_term and hasattr(long_term, "get_full_profile"):
                    try:
                        profile = long_term.get_full_profile()
                        for k, v in profile.items():
                            pref_lines.append(f"- {k}: {v}")
                    except Exception:
                        pass
                if pref_lines:
                    out.append({
                        "role": "system",
                        "content": "已知用户偏好与画像：\n" + "\n".join(pref_lines),
                    })
            except Exception as e:
                logger.debug(f"meta profile load failed: {e}")

        # 2. ShortTerm 滑动窗口（MemGPT-style）
        if self._memory is not None:
            try:
                short_ctx = self._memory.short_term.get_context(max_tokens=8000)
                if short_ctx:
                    # 触发压缩提示（如果接近上限）
                    try:
                        tokens_est = sum(
                            self._memory.short_term._estimate_tokens(m.get("content", ""))
                            for m in short_ctx
                        )
                        if tokens_est > 8000:
                            out.append({
                                "role": "system",
                                "content": (
                                    f"⚠️ 对话上下文已接近上限 (~{tokens_est} tokens)。"
                                    "请使用 summarize_memory / extract_to_long_term / delete_memory "
                                    "工具来管理记忆。"
                                ),
                            })
                    except Exception:
                        pass
                    out.extend(short_ctx)
                    return out
            except Exception as e:
                logger.debug(f"short_term load failed: {e}")

        # 3. Fallback：从 ws_messages 取
        if self._db is None:
            return out
        try:
            rows = self._db.conn.execute(
                """SELECT * FROM ws_messages
                   WHERE agent_id=? AND session_id=?
                     AND priority_kind != 'silent'
                     AND ((role='user' AND type='message')
                          OR (role='assistant' AND type='message' AND subtype IN ('text','reply')))
                   ORDER BY id DESC LIMIT ?""",
                (self._agent_id, session_id, limit),
            ).fetchall()
        except Exception as e:
            logger.debug(f"build_history_context fallback failed: {e}")
            return out

        for r in reversed(rows):
            try:
                payload = json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            text = payload.get("content", "")
            if not text:
                continue
            role = "user" if r["role"] == "user" else "assistant"
            out.append({"role": role, "content": text})
        return out

    def _write_short_term(self, session_id: str, user_content: str,
                           assistant_text: str = "", tool_results: dict = None):
        """Phase 4: 把当前轮对话写入 MemoryManager.short_term。

        在 turn 收尾时调用 — 每轮一次，避免重复。
        """
        if self._memory is None:
            return
        try:
            if user_content:
                self._memory.short_term.add_message("user", user_content)
            if assistant_text:
                self._memory.short_term.add_message("assistant", assistant_text)
            if tool_results:
                for cid, res in tool_results.items():
                    self._memory.short_term.add_tool_call(cid, {}, res)
            # 触发压缩检查
            self._memory.short_term._maybe_compress()
        except Exception as e:
            logger.debug(f"write_short_term failed: {e}")

    async def _push(self, session_id: str, msg_type: str, sub_type: str,
                    role: str, payload: dict, priority_kind: str = "normal") -> str:
        """统一出口 — 通过 outbox 发送（持久化 + 队列 + APNs 联动）。"""
        envelope = {
            "type": msg_type,
            "subType": sub_type,
            "role": role,
            "agentId": self._agent_id,
            "sessionId": session_id,
            "timestamp": _now(),
            "payload": payload,
            "priorityKind": priority_kind,
        }
        try:
            return await outbox_publish(
                self.redis, self._db, envelope,
                correlation_id=self._correlation_id,
            )
        except Exception as e:
            logger.warning(f"_push outbox failed: {e}")
            return ""

    async def _wait_ws_reply(self, session_id: str, msg_type: str, msg_sub: str,
                              timeout: float = ASK_USER_TIMEOUT_SEC,
                              match_fn=None) -> Optional[dict]:
        """阻塞等用户/iOS 回复指定 type+subType 的消息。

        与 v2 不同：不匹配的消息 LPUSH 到 parked sideband，主循环下轮再合并。
        """
        ws_queue = f"agent:ws:{self._agent_id}"
        parked_queue = f"agent:ws:{self._agent_id}:parked"
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                await self._push(session_id, "error", "TASK_FAILED", "system",
                                 payload={"message": f"等待 {msg_type}/{msg_sub} 超时",
                                          "recoverable": True},
                                 priority_kind="urgent")
                return None
            try:
                raw = await self.redis.brpop(ws_queue,
                                             timeout=int(min(remaining, 30)))
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if raw is None:
                continue
            try:
                msg = json.loads(raw[1])
            except json.JSONDecodeError:
                continue

            got_session = msg.get("_session_id", "main")
            got_type = msg.get("type", "")
            got_sub = msg.get("subType", "")
            payload = msg.get("payload", {})

            matched = (got_session == session_id
                       and got_type == msg_type and got_sub == msg_sub)
            if matched and (match_fn is None or match_fn(payload)):
                return payload

            # 不匹配 → parked
            try:
                await self.redis.lpush(parked_queue, raw[1])
            except Exception:
                pass

    def _record_event(self, session_id: str, event_type: str, payload: dict):
        """Phase 4: 写入 agent_events 表用于 crash recovery。"""
        if self._db is None or not self._correlation_id:
            return
        try:
            self._db.record_agent_event(
                self._agent_id, session_id, self._correlation_id,
                event_type, payload,
            )
        except Exception as e:
            logger.debug(f"record_agent_event failed: {e}")

    # ── Phase 4: Crash Recovery (事件源 replay) ─────────

    async def _recover_pending_turns(self):
        """启动时扫描未完成 turn 并恢复。

        策略:
          - turn_started 但无 turn_completed → pending
          - replay 出最后已知 state.phase 和 waiting_for
          - 根据状态决定下一步（重发提问 / 重跑节点 / 写 completed）
        """
        if self._db is None:
            return
        try:
            pending = self._db.get_pending_correlations(self._agent_id)
        except Exception as e:
            logger.warning(f"get_pending_correlations failed: {e}")
            return
        if not pending:
            logger.info("Recovery: no pending turns")
            return

        logger.info("Recovery: %d pending turn(s) to inspect", len(pending))
        for corr_id in pending:
            try:
                events = self._db.get_events_by_correlation(corr_id)
                state = self._replay(events)
                await self._resume_from_state(corr_id, state)
            except Exception as e:
                logger.warning("Recovery failed for %s: %s", corr_id, e, exc_info=True)
                # 给这一轮兜底写完成（避免下次又被认为 pending）
                self._correlation_id = corr_id
                self._record_event(state.get("session_id", "main") if "state" in locals() else "main",
                                   "turn_completed", {"outcome": "recovery_failed", "error": str(e)})

    @staticmethod
    def _replay(events: list[dict]) -> dict:
        """事件序列重放，重建 MainAgent 内部状态。"""
        state: dict[str, Any] = {
            "correlation_id": None,
            "session_id": None,
            "user_message": None,
            "phase": "initial",
            "intent_result": None,
            "plan_result": None,
            "tool_calls": {},
            "waiting_for": None,
            "pending_message_id": None,
            "results": {},
            "outcome": None,
        }
        for ev in events:
            et = ev["event_type"]
            payload = ev.get("payload") or {}

            if et == "turn_started":
                state["phase"] = "started"
                state["correlation_id"] = ev["correlation_id"]
                state["session_id"] = ev["session_id"]
                state["user_message"] = payload.get("user_message")
            elif et == "intent_classified":
                state["phase"] = "classified"
                state["intent_result"] = payload
            elif et == "scenario_planned":
                state["phase"] = "planned"
                state["plan_result"] = payload
            elif et == "clarification_requested":
                state["waiting_for"] = "clarification"
                state["pending_message_id"] = payload.get("msg_id", "")
            elif et == "clarification_received":
                state["waiting_for"] = None
                state["pending_message_id"] = None
            elif et == "plan_approval_requested":
                state["waiting_for"] = "approval"
                state["pending_message_id"] = payload.get("msg_id", "")
            elif et == "plan_approved":
                state["waiting_for"] = None
                state["phase"] = "executing"
            elif et == "plan_rejected":
                state["waiting_for"] = None
                state["phase"] = "rejected"
            elif et == "tool_call_started":
                cid = payload.get("call_id", "")
                state["tool_calls"][cid] = {
                    "status": "running",
                    "kind": payload.get("kind"),
                    "name": payload.get("name"),
                    "arguments": payload.get("arguments", {}),
                }
            elif et == "tool_call_completed":
                cid = payload.get("call_id", "")
                if cid in state["tool_calls"]:
                    state["tool_calls"][cid]["status"] = "done"
                    state["tool_calls"][cid]["result"] = payload.get("result")
                state["results"][cid] = payload.get("result")
            elif et == "tool_call_failed":
                cid = payload.get("call_id", "")
                if cid in state["tool_calls"]:
                    state["tool_calls"][cid]["status"] = "failed"
                    state["tool_calls"][cid]["error"] = payload.get("error", "")
                state["results"][cid] = {"error": payload.get("error", "")}
            elif et == "completion_evaluated":
                state["phase"] = "evaluating"
                if payload.get("satisfied"):
                    state["phase"] = "done"
            elif et == "inline_reply_sent":
                state["phase"] = "done"
            elif et == "turn_completed":
                state["phase"] = "completed"
                state["outcome"] = payload.get("outcome")
        return state

    async def _resume_from_state(self, correlation_id: str, state: dict):
        """根据 replay 出的 state 决定下一步动作。"""
        session_id = state.get("session_id") or "main"
        phase = state.get("phase")
        waiting = state.get("waiting_for")
        running_tools = [c for c, v in state.get("tool_calls", {}).items()
                          if v.get("status") == "running"]

        # 已完成 → 跳过
        if phase == "completed":
            return

        # 通用：恢复 correlation_id 上下文
        self._correlation_id = correlation_id

        if waiting in ("clarification", "approval"):
            # 仍在等用户回答 → 给 iOS 发个"提醒"消息（高优先级触发 APNs），
            # 但不重发原 ask_user，让用户主动回复后下次 BRPOP 处理。
            logger.info("Recovery: turn %s waiting=%s, sending reminder", correlation_id, waiting)
            await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                              payload={"message": f"上次的对话仍在等待您的{'澄清' if waiting=='clarification' else '批准'}（重启已恢复，请回到 App 中查看待处理项）。",
                                       "recoverable": True,
                                       "recovery": True,
                                       "correlation_id": correlation_id},
                              priority_kind="high")
            # 把 turn 标完成，避免下次又恢复（用户回答时会开新 turn）
            self._record_event(session_id, "turn_completed",
                               {"outcome": "recovered_to_waiting"})
            return

        if running_tools:
            # 有未完成的 tool_call —— 这种情况安全做法是把这轮标失败让用户重发
            logger.info("Recovery: turn %s has running tools=%s, marking failed",
                         correlation_id, running_tools)
            for cid in running_tools:
                self._record_event(session_id, "tool_call_failed",
                                   {"call_id": cid, "error": "interrupted by daemon restart"})
            await self._push(session_id, "error", "TASK_FAILED", "system", payload={
                "message": "进程重启中断了未完成的任务，请重试。",
                "recoverable": True, "correlation_id": correlation_id,
            }, priority_kind="urgent")
            self._record_event(session_id, "turn_completed", {"outcome": "recovered_interrupted"})
            return

        # 其他 phase（started/classified/planned/executing/evaluating） → 直接标完成
        # 因为 LLM 输出是非确定性的，重跑可能产生不一致的结果，
        # 安全策略是放弃这一轮、让用户重发
        logger.info("Recovery: turn %s phase=%s, marking abandoned",
                     correlation_id, phase)
        self._record_event(session_id, "turn_completed", {"outcome": "recovered_abandoned"})


# ── Utilities ─────────────────────────────────────────


def _truncate(obj: Any, max_chars: int = 1500) -> Any:
    """递归截断长字符串/列表，避免 LLM context 爆炸。"""
    if isinstance(obj, str):
        return obj if len(obj) <= max_chars else obj[:max_chars] + "...[truncated]"
    if isinstance(obj, dict):
        return {k: _truncate(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > 50:
            return [_truncate(v, max_chars) for v in obj[:50]] + ["...[list truncated]"]
        return [_truncate(v, max_chars) for v in obj]
    return obj
