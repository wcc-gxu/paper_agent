"""MainAgent — v3.1 Agent 主循环。

接收 iOS/Web 端 WebSocket 消息，经过 C1 安全过滤后委托给 LangGraph StateGraph 处理。
Graph 内部: fast_triage → chat/ops/research → plan(todos) → execute(ReAct) → evaluate(5出口)。

所有出站消息走 outbox（持久化 + Redis List → outbox_poller → WS / APNs）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .main_agent_prompts import (
    SafetyResult,
    build_safety_filter_prompt,
)
from .outbox import outbox_publish

logger = logging.getLogger(__name__)


def _summarize_error_for_user(error_msg: str) -> str:
    """将内部错误摘要转换为用户可读的友好消息。

    确保 turn 终止时用户收到 message/reply（协议要求），而不是只有 error/* 系统消息。
    """
    msg_lower = error_msg.lower()

    if "400" in msg_lower and ("bad request" in msg_lower or "http" in msg_lower):
        return "抱歉，处理请求时遇到内部错误（LLM 请求格式异常）。工程师已收到详细日志，请稍后重试。"
    if "429" in msg_lower or "rate" in msg_lower:
        return "抱歉，当前请求频率过高，请稍等片刻后再试。"
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return "抱歉，请求处理超时。请尝试简化您的问题后重试。"
    if "connection" in msg_lower or "network" in msg_lower:
        return "抱歉，LLM 服务暂时无法连接，请稍后重试。"
    if "知识库中没有" in error_msg or "no vector results" in msg_lower:
        return error_msg  # 已经是对用户友好的消息
    if "top_k" in msg_lower or "nonetype" in msg_lower:
        return "抱歉，检索参数异常，已自动修复。请重试您的问题。"
    if len(error_msg) > 200:
        return f"抱歉，处理请求时遇到内部错误。详细信息: {error_msg[:200]}…"
    return f"抱歉，处理请求时遇到内部错误。详细信息: {error_msg}"


# _wait_ws_reply 等待用户回答的最长时间
ASK_USER_TIMEOUT_SEC = 30 * 60   # 等用户回答最长 30 分钟

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


def _normalize_for_safety(text: str) -> str:
    """对用户输入做 NFKC 归一化 + 去零宽 + 折叠空白，避免 homoglyph / 全角 / 零宽空格绕过 regex。

    - NFKC 把全角 'ｉｇｎｏｒｅ' / 罗马数字 'Ⅰ' / 兼容字符折成 ASCII 形态
    - 删除 U+200B/200C/200D/FEFF 等零宽 + U+00AD 软连字符
    - 同时输出一个"无空白"版本拼回原串末尾，让"忽 略前面的指令"（中间插空格的）也能被
      `忽略` 触发命中
    """
    if not text:
        return text
    normed = unicodedata.normalize("NFKC", text)
    stripped = "".join(
        ch for ch in normed
        if ch not in ("​", "‌", "‍", "﻿", "­")
        and (ch >= " " or ch in ("\n", "\t"))
    )
    # 把所有空白塞掉再拼到末尾 — 一次 regex 同时覆盖"原文"和"折叠版"两个视角
    no_space = re.sub(r"\s+", "", stripped)
    return stripped + "\n" + no_space


def _safety_regex_check(text: str) -> Optional[str]:
    """对 user_content 跑 regex 黑名单。命中返回 risk_kind，否则 None。

    输入会先做 NFKC 归一化 + 去零宽，避免攻击者用全角字符 / 零宽空格绕过。
    """
    probe = _normalize_for_safety(text)
    for kind, pat in _SAFETY_REGEX_PATTERNS:
        if pat.search(probe):
            return kind
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex[:16]}"


def _new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:10]}"


# ═══════════════════════════════════════════════════════════════
# v9 → v10 协议映射
# ═══════════════════════════════════════════════════════════════
#
# _push 调用方继续传 v9 类型名（"message"/"text" 等），本映射函数把它们
# 在 envelope 落地前翻译成 v10 类型名 + payload 字段名。这样不用改 N 个
# 调用点，回滚也只动这一处。
#
# 删除类（map 到 None,None,None）：("message", "thinking")
# 保持类（透传）：status / pong / sync_complete / 已是 v10 名的

# v9 (type, subType) → v10 (type, subType) 简单映射
_V9_TO_V10_TYPE: dict[tuple[str, str], tuple[str, str]] = {
    ("message", "text"):              ("message", "reply"),
    ("tool",    "ios_request"):       ("tool",    "call"),
    ("tool",    "sub_request"):       ("tool",    "start"),
    ("tool",    "sub_progress"):      ("tool",    "progress"),
    ("tool",    "sub_result"):        ("tool",    "result"),
    # ask_user_question / propose_plan 走单独 payload 重构分支，不在这里
}

# 按 permissions_required 硬映射 danger_level（LLM 不参与；与协议 §4.4 对齐）
_HIGH_DANGER_PERMS = frozenset({"shell_exec", "package_install", "video_download"})
_MEDIUM_DANGER_PERMS = frozenset({"search", "download", "citation_chase", "subscription"})


def _hard_map_danger_level(permissions: list[str], summary: str = "") -> str:
    """按权限+summary 硬映射 danger_level。"""
    perms = set(permissions or [])
    text = (summary or "").lower()
    if perms & _HIGH_DANGER_PERMS or "delete" in text or "删除" in text or "rm " in text:
        return "high"
    if perms & _MEDIUM_DANGER_PERMS:
        return "medium"
    return "low"


def _ask_kind_from_question_type(qtype: str) -> str:
    """ClarificationQuestion.type → v10 ask kind。"""
    return {
        "single_choice": "choice",
        "multi_choice":  "multi_choice",
        "open":          "text",
    }.get(qtype, "choice")


def _build_ask_options(question: dict) -> list[dict]:
    """把 v9 question 的 options/values 转成 v10 [{value,label,hint?}]。"""
    raw_options = question.get("options") or []
    raw_values = question.get("values") or []
    out: list[dict] = []
    for i, opt in enumerate(raw_options):
        v = raw_values[i] if i < len(raw_values) else opt
        out.append({"value": v, "label": opt})
    return out


def _convert_ask_user_question_payload(payload: dict) -> dict:
    """v9 tool/ask_user_question payload → v10 ask payload。

    v9: {id, questions:[{id,question,type,options,values?,...}], context}
    v10: {ask_id, kind, prompt, options?, context, questions?, danger_level}

    多题模式：kind 取首题 type，options 由首题 options 转换，
    questions[] 完整保留（v10 文档示例支持 prompt+questions 多题模式）。
    """
    ask_id = payload.get("id") or payload.get("ask_id") or _new_call_id()
    questions = payload.get("questions") or []
    context = payload.get("context", "")

    if not questions:
        return {
            "ask_id": ask_id,
            "kind": "text",
            "prompt": payload.get("question", "请补充信息"),
            "context": context,
            "danger_level": "low",
        }

    first = questions[0]
    kind = _ask_kind_from_question_type(first.get("type", "single_choice"))
    new_payload: dict = {
        "ask_id": ask_id,
        "kind": kind,
        "prompt": first.get("question", "请确认"),
        "context": context,
        "danger_level": "low",
    }
    if kind in ("choice", "multi_choice"):
        new_payload["options"] = _build_ask_options(first)
    elif kind == "text":
        new_payload["placeholder"] = first.get("placeholder", "")
    # 多题模式：保留完整 questions 数组
    if len(questions) > 1:
        new_payload["questions"] = questions
    return new_payload


def _convert_propose_plan_payload(payload: dict) -> dict:
    """v9 tool/propose_plan payload → v10 ask(kind=plan) payload。

    v9: {id, scenario_id, summary, permissions, estimated_time_seconds, tools}
    v10: {ask_id, kind:"plan", prompt, danger_level, plan:{scenario_id,summary,
          permissions, estimated_seconds, steps:[{label,detail},...]}}

    steps 由 tools[] 转换：每个 tool 一个 step；label=tool.name，
    detail=参数摘要前 80 字。
    """
    ask_id = payload.get("id") or payload.get("ask_id") or _new_call_id()
    summary = payload.get("summary", "")
    permissions = list(payload.get("permissions") or [])
    estimated = int(payload.get("estimated_time_seconds") or 0)
    scenario_id = payload.get("scenario_id", "")
    tools = payload.get("tools") or []

    steps: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("kind") or "step"
        args = t.get("arguments") or {}
        try:
            detail = json.dumps(args, ensure_ascii=False, default=str)[:80]
        except Exception:
            detail = str(args)[:80]
        steps.append({"label": name, "detail": detail})

    danger = _hard_map_danger_level(permissions, summary)
    return {
        "ask_id": ask_id,
        "kind": "plan",
        "prompt": summary,
        "danger_level": danger,
        "plan": {
            "scenario_id": scenario_id,
            "summary": summary,
            "permissions": permissions,
            "estimated_seconds": estimated,
            "steps": steps,
        },
    }


def _convert_tool_call_payload(payload: dict) -> dict:
    """v9 tool/ios_request payload → v10 tool/call payload。

    v9: {id, name, input}
    v10: {tool_call_id, name, input}
    """
    new = dict(payload)
    if "id" in new and "tool_call_id" not in new:
        new["tool_call_id"] = new.pop("id")
    return new


def _convert_tool_start_payload(payload: dict) -> dict:
    """v9 tool/sub_request payload → v10 tool/start payload。

    v9: {taskId, name, label, query, estimatedStages}
    v10: {tool_call_id, name, label, total_steps?, can_cancel?}
    """
    new = dict(payload)
    if "taskId" in new and "tool_call_id" not in new:
        new["tool_call_id"] = new.pop("taskId")
    if "estimatedStages" in new and "total_steps" not in new:
        new["total_steps"] = new.pop("estimatedStages")
    new.setdefault("can_cancel", False)
    return new


def _convert_tool_progress_payload(payload: dict) -> dict:
    """v9 tool/sub_progress payload → v10 tool/progress payload。

    v9: {taskId, name, stage, current, total, message}
    v10: {tool_call_id, step, total, stage, message}
    """
    new = dict(payload)
    if "taskId" in new and "tool_call_id" not in new:
        new["tool_call_id"] = new.pop("taskId")
    if "current" in new and "step" not in new:
        new["step"] = new.pop("current")
    return new


def _convert_tool_result_payload(payload: dict) -> dict:
    """v9 tool/sub_result payload → v10 tool/result payload。

    v9: {taskId, name, status, summary, result}
    v10: {tool_call_id, status, summary, data?}
    """
    new = dict(payload)
    if "taskId" in new and "tool_call_id" not in new:
        new["tool_call_id"] = new.pop("taskId")
    if "result" in new and "data" not in new:
        new["data"] = new.pop("result")
    return new


def _v9_to_v10_envelope(
    msg_type: str, sub_type: str, payload: dict,
) -> tuple[Optional[str], str, dict]:
    """v9 (type, subType, payload) → v10 (type, subType, payload)。

    返回 (None, "", {}) 表示该消息在 v10 被删除，应静默丢弃（如 message/thinking）。
    """
    # ── 协议删除类 ──
    if (msg_type, sub_type) == ("message", "thinking"):
        return (None, "", {})

    # ── ask 系列（合并 ask_user_question + propose_plan）──
    if (msg_type, sub_type) == ("tool", "ask_user_question"):
        return ("ask", "", _convert_ask_user_question_payload(payload or {}))
    if (msg_type, sub_type) == ("tool", "propose_plan"):
        return ("ask", "", _convert_propose_plan_payload(payload or {}))

    # ── tool 系列（payload 字段重命名）──
    if (msg_type, sub_type) == ("tool", "ios_request"):
        return ("tool", "call", _convert_tool_call_payload(payload or {}))
    if (msg_type, sub_type) == ("tool", "sub_request"):
        return ("tool", "start", _convert_tool_start_payload(payload or {}))
    if (msg_type, sub_type) == ("tool", "sub_progress"):
        return ("tool", "progress", _convert_tool_progress_payload(payload or {}))
    if (msg_type, sub_type) == ("tool", "sub_result"):
        return ("tool", "result", _convert_tool_result_payload(payload or {}))

    # ── message/text → message/reply（payload 不变）──
    if (msg_type, sub_type) == ("message", "text"):
        return ("message", "reply", payload or {})

    # ── v3.1 Plan Review + Execution Transparency (透传，已是 v10 格式) ──
    if msg_type in ("plan_review", "plan_todo_update", "tool_execution"):
        return (msg_type, sub_type, payload or {})

    # ── 控制/协议消息（不应走 outbox → WebSocket）──
    if msg_type in ("sync_ack", "sync_complete", "sync_request"):
        return (None, "", {})

    # ── 其他类型透传（status / error / pong / 已 v10 命名的）──
    return (msg_type, sub_type, payload or {})


# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# MainAgent
# ═══════════════════════════════════════════════════════════════


class MainAgent:
    """新主 Agent — 替代 v1 (AgentRunLoop+PlanGraph) 和 v2 (AgentLoop)。"""

    def __init__(
        self,
        agent_id: str = "agent-001",
        redis_url: str = "redis://localhost:6379/0",
        user_id: str = "default",
        llm=None,
        db=None,
        memory=None,
        registry=None,
        graph=None,  # v3.1: compiled LangGraph StateGraph
        system_prompt: str = "",
    ):
        self._agent_id = agent_id
        self._redis_url = redis_url
        self._llm = llm
        self._db = db
        self._memory = memory       # Phase 4 接入 MemoryManager
        self._registry = registry   # ToolRegistry
        self._graph = graph         # v3.1: compiled MainGraph
        self._system_prompt = system_prompt  # v3.2: per-agent
        self._redis = None
        # 当前正在处理的 correlation_id（每轮 BRPOP 重置）
        self._correlation_id: str = ""
        # v3.2: _user_id now passed explicitly by AgentManager — no derivation
        # 从 agent_id 提取 user_id（格式: agent-{user_id}）
        self._user_id = user_id        # v3.2: explicit, no longer derived from agent_id
        # Track current session for debug callback
        self._current_session_id: str = "main"
        # AgentError 队列：子 agent / tool 通过 Reporter Redis Pub/Sub 上报的错误
        self._error_queue: asyncio.Queue[dict] = asyncio.Queue()
        # 当前 turn 内累积的 AgentError（在 _run_turn 开始时清空）
        self._pending_agent_errors: list[dict] = []
        self._vector_store = None  # Lazy: PgVectorStore for message embedding
        # Debug mode: wire LLM raw events → WS debug status messages
        self._wire_llm_debug()

    # ── Redis (惰性) ─────────────────────────────────────

    @property
    def redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    # ── Vector Store (惰性) ──────────────────────────────

    @property
    def vector_store(self):
        """PgVectorStore 惰性初始化（用于 message embedding / recall）。"""
        if self._vector_store is None:
            from .pgvector_store import PgVectorStore
            self._vector_store = PgVectorStore(user_id=self._user_id)
        return self._vector_store

    def _embed_reply_async(self, session_id: str, content: str):
        """Fire-and-forget: 将 assistant reply 嵌入向量数据库。"""
        import asyncio
        async def _do():
            try:
                vs = self.vector_store
                # 生成一个简单的 msg_id 标识（实际 msg_id 在 outbox_publish 已生成）
                msg_id = f"reply-{hashlib.md5(content[:200].encode()).hexdigest()[:16]}"
                vs.add_message_embedding(session_id, msg_id, content, self._user_id)
            except Exception:
                pass  # 静默失败，不影响主流程
        try:
            asyncio.create_task(_do())
        except Exception:
            pass

    def _recall_history_async(self, session_id: str, query_text: str):
        """Fire-and-forget: 检索与用户消息相关的历史对话。"""
        import asyncio
        async def _do():
            try:
                vs = self.vector_store
                threshold = float(os.environ.get("VECTOR_SIMILARITY_THRESHOLD", "0.75"))
                results = vs.search_similar_messages(
                    query_text, self._user_id, threshold=threshold, limit=5,
                )
                if results:
                    logger.info("📚 向量召回 %d 条相关历史 | threshold=%.2f",
                                len(results), threshold)
                    # TODO: 将召回结果注入到后续 LLM 上下文（Store / system prompt）
            except Exception:
                pass
        try:
            asyncio.create_task(_do())
        except Exception:
            pass
        return self._redis

    # ── AgentError 处理 ─────────────────────────────────

    def push_agent_error(self, error_data: dict) -> None:
        """接收来自 Redis Pub/Sub consumer 的 AgentError。

        由 daemon.py 的 _consume_agent_reports 协程调用，
        将错误推入队列供当前 turn 检查。
        """
        try:
            self._error_queue.put_nowait(error_data)
        except asyncio.QueueFull:
            logger.warning("AgentError queue full, dropping error: %s",
                          error_data.get("error", {}).get("message", "")[:100])

    async def drain_pending_errors(self, session_id: str) -> int:
        """清空错误队列，将累积的 AgentError 推送给用户。

        Returns:
            清空的错误数量。
        """
        count = 0
        # Drain from queue
        while not self._error_queue.empty():
            try:
                err = self._error_queue.get_nowait()
                self._pending_agent_errors.append(err)
                count += 1
            except asyncio.QueueEmpty:
                break

        # Push accumulated errors to user
        for err_data in self._pending_agent_errors:
            error_obj = err_data.get("error", err_data)
            if isinstance(error_obj, dict):
                msg = error_obj.get("message", str(error_obj))
                agent = error_obj.get("agent", "unknown")
                node = error_obj.get("node", "unknown")
                error_type = error_obj.get("error_type", "Exception")
                user_msg = f"[{agent}.{node}] {error_type}: {msg}"
            else:
                user_msg = str(error_obj)

            await self._push(
                session_id, "error", "TASK_FAILED", "system",
                payload={
                    "message": user_msg,
                    "recoverable": False,
                    "agent_error": error_obj if isinstance(error_obj, dict) else str(error_obj),
                },
                priority_kind="urgent",
            )
            logger.error(
                "AgentError pushed to user: agent=%s node=%s type=%s msg=%s",
                error_obj.get("agent", "?") if isinstance(error_obj, dict) else "?",
                error_obj.get("node", "?") if isinstance(error_obj, dict) else "?",
                error_obj.get("error_type", "?") if isinstance(error_obj, dict) else "?",
                error_obj.get("message", str(error_obj))[:200] if isinstance(error_obj, dict) else str(error_obj)[:200],
            )

        self._pending_agent_errors.clear()
        if count:
            logger.info("Drained %d AgentError(s), pushed to user", count)
        return count

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
        """v3.1: safety_filter → delegating to LangGraph StateGraph.

        Graph nodes: fast_triage → chat/ops/research →
                     research → intent_classify → plan(todos) → execute(ReAct) → evaluate
                     ops → ops_confirm → execute(ReAct)
                     chat → inline_reply → END
        """
        self._current_session_id = session_id

        # Drain any pending AgentErrors from previous async operations
        await self.drain_pending_errors(session_id)

        # 收到消息后立即推 status{received}（协议 v10 要求 <200ms ack）
        await self._push_status(session_id, "received", "收到，正在分析...", level="user")

        # ── Phase 4: fire-and-forget 向量召回相关历史 ──
        self._recall_history_async(session_id, user_content)

        # C1 安全前置过滤（regex 兜底 + 命中时 LLM 二次确认）
        safety = await self._node_safety_filter(session_id, user_content)
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

        # v3.1: 委托给 LangGraph StateGraph
        if self._graph is not None:
            await self._push_status(session_id, "analyzing", "正在分析...")
            initial_state: dict[str, Any] = {
                "user_content": user_content,
                "session_id": session_id,
                "correlation_id": self._correlation_id,
                "plan_id": "",
                "plan_approved": False,
                "plan_feedback": "",
                "plan_iterations": 0,
            }
            try:
                final_state = await self._graph.ainvoke(
                    initial_state,
                    config={"configurable": {"thread_id": session_id}},
                )
                final_reply = final_state.get("final_reply", "")
                reply_pushed = final_state.get("_reply_pushed", False)
                error = final_state.get("error")

                # Push final_reply if the graph generated one but didn't push it inline
                # (e.g., evaluate→done path stores final_reply in state but never sends it)
                if final_reply and not reply_pushed:
                    await self._push(session_id, "message", "text", "assistant",
                                     payload={"content": final_reply},
                                     priority_kind="high")

                if error:
                    # Push user-facing error explanation BEFORE status/done
                    error_msg = str(error)
                    user_friendly = _summarize_error_for_user(error_msg)
                    await self._push(session_id, "message", "reply", "assistant",
                                     payload={"content": user_friendly},
                                     priority_kind="high")
                    await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                     payload={"message": error_msg, "recoverable": True},
                                     priority_kind="urgent")
                    logger.error(f"Graph error: {error}")

                # Always signal turn completion — client needs this to know the turn is done
                await self._push_status(session_id, "done", "处理完成")

                logger.info("✅ TURN done | corr=%s reply_len=%d",
                            self._correlation_id, len(final_reply or ""))
            except Exception as e:
                logger.error(f"Graph invoke failed: {e}", exc_info=True)
                error_user_msg = _summarize_error_for_user(str(e))
                await self._push(session_id, "message", "reply", "assistant",
                                 payload={"content": error_user_msg},
                                 priority_kind="high")
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": f"Agent 处理失败: {e}", "recoverable": True},
                                 priority_kind="urgent")
                await self._push_status(session_id, "done", "处理异常终止")
            return

        # Fallback: if no graph, use old inline_reply
        await self._push_status(session_id, "analyzing", "正在回复...")
        full_text = ""
        try:
            if hasattr(self._llm, "chat_stream"):
                async for chunk in self._llm.chat_stream(
                    messages=[{"role": "user", "content": user_content}],
                    temperature=0.6,
                    node="inline_reply",
                ):
                    if isinstance(chunk, dict) and chunk.get("type") == "text_delta":
                        full_text += chunk.get("text", "")
            else:
                resp = await self._llm.chat(
                    messages=[{"role": "user", "content": user_content}],
                    temperature=0.6, node="inline_reply",
                )
                full_text = getattr(resp, "content", "") or str(resp)
        except Exception as e:
            logger.warning(f"fallback inline_reply failed: {e}")
            full_text = f"抱歉，LLM 调用出错：{e}"

        await self._push(session_id, "message", "reply", "assistant",
                         payload={"content": full_text.strip()},
                         priority_kind="high")

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
                node="safety_filter",
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

    # ── Helpers ────────────────────────────────────────

    def _combine_user_text(self, msg_list: list[dict]) -> str:
        parts = []
        for m in msg_list:
            payload = m.get("payload") or {}
            content = payload.get("content")
            if content:
                parts.append(content)
        return "\n\n".join(parts)

    async def _push(self, session_id: str, msg_type: str, sub_type: str,
                    role: str, payload: dict, priority_kind: str = "normal") -> str:
        """统一出口 — 通过 outbox 发送（持久化 + 队列 + APNs 联动）。

        v10 迁移：本函数在内部做 v9→v10 类型映射，调用方继续传旧名（"message"/"text",
        "tool"/"sub_request" 等），envelope 写出去的是 v10。映射规则见
        _v9_to_v10_envelope()。
        """
        new_type, new_sub, new_payload = _v9_to_v10_envelope(msg_type, sub_type, payload)
        if new_type is None:
            # 协议明确要求删除的类型（如 message/thinking）→ 静默丢弃
            return ""

        envelope = {
            "type": new_type,
            "subType": new_sub,
            "agentId": self._agent_id,
            "sessionId": session_id,
            "timestamp": _now(),
            "payload": new_payload,
            "priority": priority_kind,
        }
        try:
            result = await outbox_publish(
                self.redis, self._db, envelope,
                correlation_id=self._correlation_id,
            )
            # ── Phase 4: fire-and-forget embed assistant replies ──
            if new_type == "message" and new_sub == "reply":
                content = new_payload.get("content", "") if isinstance(new_payload, dict) else ""
                if content:
                    self._embed_reply_async(session_id, content)
            return result
        except Exception as e:
            logger.warning(f"_push outbox failed: {e}")
            return ""

    def _wire_llm_debug(self):
        """Wire LLM raw events → WS debug status messages (DEBUG_PROTOCOL=1)."""
        import os
        if os.environ.get("DEBUG_PROTOCOL", "") != "1" or not self._llm:
            return
        if not hasattr(self._llm, 'on_raw_event'):
            return

        async def _push_debug(event_type: str, data: dict):
            try:
                # Use the last known session_id from the current turn
                sid = getattr(self, '_current_session_id', 'main')
                await self._push_status(sid, f"llm:{event_type}",
                    str(data)[:500], level="debug")
            except Exception:
                pass

        self._llm.on_raw_event = _push_debug

    async def _push_status(self, session_id: str, stage: str,
                           message: str, level: str = "info") -> None:
        """v2: 推 status 消息（协议 v10 新增类型，给用户阶段反馈）。

        stage 取值: received / analyzing / planning / searching /
                    executing / evaluating / done / error
        level:   info / user / warning
        """
        await self._push(
            session_id, "status", stage, "system",
            payload={"stage": stage, "message": message, "level": level},
            priority_kind="silent",  # status 不触发 APNs
        )

    def _record_event(self, session_id: str, event_type: str, payload: dict):
        """Phase 4: 写入 event_logs 表用于 crash recovery。"""
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
