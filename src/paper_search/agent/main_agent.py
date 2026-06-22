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
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .main_agent_prompts import (
    EVALUATE_COMPLETION_SYSTEM,
    INLINE_REPLY_SYSTEM,
    SCENARIOS,
    EvaluateCompletionResult,
    IntentClassifyResult,
    SafetyResult,
    ScenarioMatch,
    ScenarioPlanResult,
    ToolCallSpec,
    build_intent_classify_prompt,
    build_safety_filter_prompt,
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

# C3: 灰区阈值 — 当所有 scenario.confidence 都低于此值时，触发 ask_user 让用户挑选
# 可通过环境变量 INTENT_ASK_THRESHOLD 覆盖（默认 0.6）
INTENT_ASK_THRESHOLD = float(os.getenv("INTENT_ASK_THRESHOLD", "0.6"))

# C1: 安全前置过滤的 regex 黑名单（毫秒级兜底；命中后再让小 LLM 二次确认）
# 只覆盖最高频的注入/越狱模式，宁缺勿滥避免误杀正常学术提问
_SAFETY_REGEX_PATTERNS: list[tuple[str, re.Pattern]] = [
    # prompt injection
    ("prompt_injection", re.compile(
        r"(忽略|ignore|disregard|forget).{0,15}(前面|above|previous|all|之前).{0,10}(指令|instruction|prompt|rule|规则)",
        re.IGNORECASE)),
    ("prompt_injection", re.compile(
        r"(system\s*:|<\s*system\s*>|你的(系统|底层)\s*(prompt|提示|指令))",
        re.IGNORECASE)),
    ("prompt_injection", re.compile(
        r"(输出|reveal|show|print|dump).{0,20}(完整|整个|全部|full|entire|raw).{0,10}(prompt|提示词|system\s*message)",
        re.IGNORECASE)),
    # jailbreak
    ("jailbreak", re.compile(
        r"(假装|pretend|act\s+as|你现在是|now\s+you\s+are).{0,30}(DAN|不受限|没有限制|no\s+restriction|jailbroken)",
        re.IGNORECASE)),
    # pii / secret leak attempts
    ("pii_leak", re.compile(
        r"(列出|输出|发送|export|list|dump).{0,15}(所有|全部|all)?\s*(API\s*key|token|密钥|\.env|环境变量)",
        re.IGNORECASE)),
    ("pii_leak", re.compile(
        # 中文动宾倒装：把/将 ... API key/密钥 ... 列出/发出/打印/告诉
        r"(把|将|要).{0,20}(API\s*key|token|密钥|\.env|环境变量).{0,20}(列出|发出|输出|告诉|打印|说出|展示)",
        re.IGNORECASE)),
]


def _safety_regex_check(text: str) -> Optional[str]:
    """对 user_content 跑 regex 黑名单。命中返回 risk_kind，否则 None。"""
    for kind, pat in _SAFETY_REGEX_PATTERNS:
        if pat.search(text):
            return kind
    return None


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
        """节点 0 (safety_filter) → 节点 1 (intent_classify) → 分支。"""
        # 节点 0: C1 安全前置过滤（regex 兜底 + 命中时 LLM 二次确认）
        safety = await self._node_safety_filter(session_id, user_content)
        self._record_event(session_id, "safety_checked", safety.model_dump())
        if not safety.safe:
            await self._push(
                session_id, "message", "text", "assistant",
                payload={"content": safety.user_message
                         or "抱歉，这个请求超出我能帮助的范围。"},
                priority_kind="high",
            )
            logger.info("🛡️ safety BLOCKED | risk=%s reason=%s",
                        safety.risk_kind, safety.reasoning[:80])
            return

        # 节点 1: 意图分类（C2: 现在 scenarios 是 list）
        intent = await self._node_intent_classify(session_id, user_content)
        self._record_event(session_id, "intent_classified", intent.model_dump())

        # C3: business 但全部 scenario 都低于阈值 → 询问用户挑选
        if intent.intent_kind == "business":
            intent = await self._maybe_clarify_low_confidence(
                session_id, user_content, intent,
            )

        if intent.intent_kind == "business" and intent.scenarios:
            # 节点 2A: 业务场景规划（C2: 传入 scenarios list）
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

    # ── 节点 0: safety_filter (C1) ─────────────────────

    async def _node_safety_filter(self, session_id: str,
                                   user_content: str) -> SafetyResult:
        """C1: 安全前置过滤 — 仅识别对抗性输入（注入/越狱/PII 提取尝试）。

        策略：regex 黑名单先跑（90%+ 输入秒过且不调 LLM）；
              regex 命中才走小模型二次确认（避免误杀，比如学术讨论 prompt injection 本身）。
        """
        # 1. regex 兜底
        regex_hit = _safety_regex_check(user_content)
        if regex_hit is None:
            return SafetyResult(safe=True, reasoning="regex_pass")

        logger.info("🛡️ safety regex hit: kind=%s, asking LLM to confirm", regex_hit)

        # 2. 命中规则 → 调 LLM 二次确认（避免学术语境的误杀）
        try:
            data = await self._llm.chat_json(
                messages=[{"role": "user", "content": user_content}],
                schema=SafetyResult,
                temperature=0.0,
                system=build_safety_filter_prompt(),
            )
            result = SafetyResult.model_validate(data)
            # LLM 没标 risk_kind 但又说 unsafe → 补 regex 命中的类型
            if not result.safe and not result.risk_kind:
                result.risk_kind = regex_hit  # type: ignore[assignment]
            return result
        except Exception as e:
            # L4 fail-closed：regex 已命中，LLM 又不可用 → 默认按 unsafe 处理
            # （宁可误杀也不放行注入攻击；regex 命中本身就说明文本可疑）
            logger.warning(
                f"safety LLM confirm failed: {e}, FAIL-CLOSED → safe=False (regex hit: {regex_hit})"
            )
            return SafetyResult(
                safe=False,
                risk_kind=regex_hit,  # type: ignore[arg-type]
                reasoning=f"regex_hit_{regex_hit}_llm_unavailable_fail_closed",
                user_message="抱歉，系统暂时无法处理这个请求，请稍后再试或换个表述。",
            )

    # ── 节点 1: intent_classify ────────────────────────

    async def _node_intent_classify(self, session_id: str, user_content: str) -> IntentClassifyResult:
        """LLM 把用户消息分类到 business/chat/meta/unsupported。

        C2 改造：business 时 scenarios 是 list，支持复合意图（一条消息触发多场景）。
        """
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
                intent_kind="chat", scenarios=[],
                overall_confidence=0.0, reasoning=f"LLM error: {e}",
            )

        try:
            result = IntentClassifyResult.model_validate(data)
        except Exception as e:
            logger.warning(f"intent_classify schema invalid: {e}, raw={data}")
            return IntentClassifyResult(
                intent_kind="chat", scenarios=[],
                overall_confidence=0.0, reasoning=f"Invalid schema: {e}",
            )

        # 整体置信度过低 → 降级为 chat（保留旧行为）
        if result.intent_kind == "business" and result.overall_confidence < 0.5:
            logger.info(
                f"Low overall_confidence ({result.overall_confidence}), treating as chat",
            )
            result.intent_kind = "chat"
            result.scenarios = []

        logger.info(
            "🧭 intent_classify | kind=%s scenarios=%s overall=%.2f",
            result.intent_kind,
            [(s.scenario_id, round(s.confidence, 2)) for s in result.scenarios],
            result.overall_confidence,
        )
        return result

    # ── C3: 灰区处理 — 低置信度时让用户挑选 ────────────

    async def _maybe_clarify_low_confidence(
        self, session_id: str, user_content: str,
        intent: IntentClassifyResult,
    ) -> IntentClassifyResult:
        """C3: 当所有 scenario 都低于 INTENT_ASK_THRESHOLD 时，问用户挑选。

        逻辑：
          - 没有 scenario（business 但 list 空）→ 把所有 17 场景列出来让用户挑（不推荐，避免）；
            这里我们直接降级为 chat（让 inline_reply 处理）
          - 至少 1 个 scenario.confidence >= 阈值 → 保留这些高置信度的，过滤掉低的，正常进入 plan
          - 全部 < 阈值（但都 > 0.3，否则 overall_confidence 已经降级过了）→ ask_user，
            列出所有 candidate scenarios + "都不是"选项
        """
        if not intent.scenarios:
            # business 但场景列表空 → 降级为 chat
            logger.info("intent=business 但 scenarios 空，降级为 chat")
            intent.intent_kind = "chat"
            return intent

        high_conf = [s for s in intent.scenarios
                     if s.confidence >= INTENT_ASK_THRESHOLD]
        if high_conf:
            # 有至少一个高置信度场景 → 保留高的，丢弃低的
            if len(high_conf) != len(intent.scenarios):
                dropped = [s.scenario_id for s in intent.scenarios
                           if s.confidence < INTENT_ASK_THRESHOLD]
                logger.info(
                    "C3: 保留高置信场景 %s，过滤低置信 %s",
                    [s.scenario_id for s in high_conf], dropped,
                )
            intent.scenarios = high_conf
            return intent

        # 全部低于阈值 → ask_user 挑选
        return await self._ask_user_pick_scenario(session_id, user_content, intent)

    async def _ask_user_pick_scenario(
        self, session_id: str, user_content: str,
        intent: IntentClassifyResult,
    ) -> IntentClassifyResult:
        """C3: 列出所有 candidate scenarios 让用户挑（可多选）。

        用户回复后，把所选 scenarios 注入 intent.scenarios（confidence 设为 1.0），
        其它分支继续走 _node_scenario_plan。
        """
        options = []
        for sm in intent.scenarios:
            sc = SCENARIOS.get(sm.scenario_id) or {}
            options.append(
                f"{sm.scenario_id}: {sc.get('name', sm.scenario_id)} "
                f"（{sc.get('description', '')[:40]}）"
            )
        options.append("都不是 / 重新描述")

        call_id = _new_call_id()
        question_payload = {
            "id": call_id,
            "questions": [{
                "id": "scenario_pick",
                "question": (
                    f"我对你的请求「{user_content[:60]}」有几种可能的理解，"
                    "请帮我确认是哪一种（可多选）："
                ),
                "type": "multi_choice",
                "options": options,
            }],
            "context": (
                "我会按你选择的场景来规划。"
                f"（参考依据：{intent.reasoning[:80]}）"
            ),
        }
        await self._push(session_id, "tool", "ask_user_question",
                         "assistant", payload=question_payload,
                         priority_kind="high")
        self._record_event(session_id, "intent_clarify_requested",
                           {"candidates": [s.model_dump() for s in intent.scenarios],
                            "msg_id": call_id})
        reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question",
                                          timeout=ASK_USER_TIMEOUT_SEC)
        if reply is None:
            # 超时 → 降级为 chat
            intent.intent_kind = "chat"
            intent.scenarios = []
            return intent

        answers = reply.get("answers", [])
        # 答案形如 [{"id": "scenario_pick", "value": ["S1: 文献调研 ...", "..."]}]
        # 或 [{"id": "scenario_pick", "value": "S1: 文献调研 ..."}]
        chosen_labels: list[str] = []
        for a in answers:
            v = a.get("value") or a.get("answer")
            if isinstance(v, list):
                chosen_labels.extend(str(x) for x in v)
            elif v:
                chosen_labels.append(str(v))

        # 解析回 scenario_id（label 开头格式为 "Sx: ..."）
        chosen_sids: list[str] = []
        for lab in chosen_labels:
            if ":" in lab:
                sid = lab.split(":", 1)[0].strip()
                if sid in SCENARIOS:
                    chosen_sids.append(sid)

        if not chosen_sids:
            # 用户选了"都不是" → 降级为 chat
            logger.info("C3: 用户表示候选场景都不对，降级为 chat")
            intent.intent_kind = "chat"
            intent.scenarios = []
            return intent

        # 注入用户选择，confidence 直接置 1.0（因为用户亲口说的）
        intent.scenarios = [
            ScenarioMatch(scenario_id=sid, confidence=1.0,  # type: ignore[arg-type]
                          reasoning="user_picked")
            for sid in chosen_sids
        ]
        self._record_event(session_id, "intent_clarify_received",
                           {"chosen": chosen_sids})
        logger.info("C3: 用户确认场景 %s", chosen_sids)
        return intent

    # ── 节点 2A: scenario_plan (business) ─────────────

    async def _node_scenario_plan(self, session_id: str, user_content: str,
                                   intent: IntentClassifyResult):
        """业务场景：生成结构化 plan，可能含 clarify/approval，最后执行。

        C2 改造：intent.scenarios 是 list，可能含 1~N 个场景。
          - 单场景：和原有行为一致
          - 多场景：为每个场景生成子 plan，合并 tools[]、permissions、summary 后统一审批+执行
        """
        scenarios = intent.scenarios
        if not scenarios:
            await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                             payload={"message": "无可用场景"},
                             priority_kind="urgent")
            return

        # 校验每个 scenario_id 都合法
        for sm in scenarios:
            if sm.scenario_id not in SCENARIOS:
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": f"未知场景 {sm.scenario_id}"},
                                 priority_kind="urgent")
                return

        # 逐个场景生成子 plan
        sub_plans: list[ScenarioPlanResult] = []
        for idx, sm in enumerate(scenarios):
            sub_plan = await self._plan_one_scenario(
                session_id, user_content, sm.scenario_id,
                scenario_idx=idx,
            )
            if sub_plan is None:
                return  # 已发 error 或超时
            sub_plans.append(sub_plan)

        # 合并多个子 plan → 一个 ScenarioPlanResult
        plan = self._merge_sub_plans(sub_plans)
        logger.info(
            "📋 scenario_plan merged | scenarios=%s tools=%d approval=%s",
            [p.scenario_id for p in sub_plans], len(plan.tools), plan.needs_approval,
        )

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

    async def _plan_one_scenario(
        self, session_id: str, user_content: str, scenario_id: str,
        scenario_idx: int = 0,
    ) -> Optional[ScenarioPlanResult]:
        """为单个场景生成 ScenarioPlanResult（含 clarify 循环）。

        scenario_idx 用于在多场景模式下给 call_id 加前缀，避免不同场景的 call_id 撞车。
        """
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
                logger.warning(f"scenario_plan LLM/schema failed (sid={scenario_id}): {e}")
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": f"方案生成失败 ({scenario_id}): {e}"},
                                 priority_kind="urgent")
                return None

            self._record_event(session_id, "scenario_planned", plan.model_dump())
            logger.info(
                "📋 scenario_plan | sid=%s clarify=%s approval=%s tools=%d",
                plan.scenario_id, plan.needs_clarification, plan.needs_approval,
                len(plan.tools),
            )

            if plan.needs_clarification and plan.clarification_questions:
                msg_id = await self._push(session_id, "tool", "ask_user_question",
                                          "assistant", payload={
                                              "id": _new_call_id(),
                                              "questions": [q.model_dump()
                                                            for q in plan.clarification_questions],
                                              "context": plan.summary,
                                          })
                self._record_event(session_id, "clarification_requested",
                                   {"questions": [q.model_dump() for q in plan.clarification_questions],
                                    "msg_id": msg_id, "scenario_id": scenario_id})
                reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question",
                                                  timeout=ASK_USER_TIMEOUT_SEC)
                if reply is None:
                    return None
                answers = reply.get("answers", [])
                self._record_event(session_id, "clarification_received",
                                   {"answers": answers, "scenario_id": scenario_id})
                messages.append({
                    "role": "user",
                    "content": "针对你刚才的问题，我的回答是：" + json.dumps(answers, ensure_ascii=False),
                })
                continue
            break

        if plan is None:
            return None

        # 在多场景模式下给 call_id 加前缀，避免合并时撞车
        if scenario_idx > 0:
            prefix = f"s{scenario_idx}_"
            rewritten_tools = []
            id_map: dict[str, str] = {}
            for t in plan.tools:
                new_id = prefix + t.call_id
                id_map[t.call_id] = new_id
                rewritten_tools.append(t.model_copy(update={"call_id": new_id}))
            # 修正 depends_on 引用
            for t in rewritten_tools:
                t.depends_on = [id_map.get(dep, dep) for dep in t.depends_on]
            plan = plan.model_copy(update={"tools": rewritten_tools})

        return plan

    def _merge_sub_plans(self, sub_plans: list[ScenarioPlanResult]) -> ScenarioPlanResult:
        """把多个 sub-plan 合并成一个 ScenarioPlanResult。

        - tools: 顺序拼接（call_id 已在 _plan_one_scenario 加前缀防撞车）
        - summary: 多场景时拼接 "场景 Sx: ..." 段落
        - needs_approval: 任一 true → true
        - permissions_required: union（保序去重）
        - estimated_time_seconds: 求和
        - scenario_id: 多场景时拼接 "S1+S12"
        """
        if len(sub_plans) == 1:
            return sub_plans[0]

        merged_tools = []
        for p in sub_plans:
            merged_tools.extend(p.tools)

        summary_parts = []
        for p in sub_plans:
            summary_parts.append(f"【{p.scenario_id}】{p.summary}")
        merged_summary = "\n\n".join(summary_parts)[:300]

        merged_permissions: list = []
        seen = set()
        for p in sub_plans:
            for perm in p.permissions_required:
                if perm not in seen:
                    merged_permissions.append(perm)
                    seen.add(perm)

        return ScenarioPlanResult(
            scenario_id="+".join(p.scenario_id for p in sub_plans),
            summary=merged_summary,
            needs_clarification=False,  # clarify 已在子 plan 阶段解决
            clarification_questions=[],
            needs_approval=any(p.needs_approval for p in sub_plans),
            permissions_required=merged_permissions,  # type: ignore[arg-type]
            estimated_time_seconds=sum(p.estimated_time_seconds for p in sub_plans),
            tools=merged_tools,
        )

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
            # L4 fail-closed：评估器出错 → satisfied=False（不再谎称完成）
            # 上游 _execute_with_evaluation 看到 needs_more_tools=[] 会兜底回一句给用户
            logger.warning(
                f"evaluate_completion LLM/schema failed: {e}, FAIL-CLOSED → satisfied=False"
            )
            return EvaluateCompletionResult(
                satisfied=False,
                reasoning=f"评估器异常 ({e})，无法确认任务是否完成",
                needs_more_tools=[],
                final_message="抱歉，评估环节出现异常，请稍后重试或换个表述。",
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
