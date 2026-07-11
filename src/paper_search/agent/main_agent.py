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
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .main_agent_prompts import (
    EVALUATE_COMPLETION_SYSTEM,
    FAST_TRIAGE_SYSTEM,
    INLINE_REPLY_SYSTEM,
    LIGHTWEIGHT_PLAN_META_SYSTEM,
    LIGHTWEIGHT_PLAN_OPS_SYSTEM,
    SCENARIOS,
    EvaluateCompletionResult,
    FastTriageResult,
    IntentClassifyResult,
    LightweightPlanResult,
    SafetyResult,
    ScenarioMatch,
    ScenarioPlanResult,
    ToolCallSpec,
    build_fast_triage_prompt,
    build_intent_classify_prompt,
    build_lightweight_plan_prompt,
    build_safety_filter_prompt,
    build_scenario_plan_prompt,
)
from .outbox import outbox_publish
from .plangraph import expand as plangraph_expand

logger = logging.getLogger(__name__)

# 最多 evaluate-execute 迭代次数（避免无限循环）
MAX_PLAN_ITERATIONS = 3
# 单个 tool / sub_agent 调用超时
TOOL_TIMEOUT_SEC = 5 * 60        # CLI 工具默认 5 分钟
SUB_AGENT_TIMEOUT_SEC = 30 * 60  # 子 Agent 默认 30 分钟
IOS_TIMEOUT_SEC = 2 * 60         # iOS 工具默认 2 分钟
ASK_USER_TIMEOUT_SEC = 30 * 60   # 等用户回答最长 30 分钟

# ── 子 Agent 类型路由 ──────────────────────────────────────
# _handle_sub_agent 按 agent_type 分发到对应 graph runner。
# 未知类型不 fallback 到 ingest（那是 P0 bug 的根因）—— 直接报错。
_KNOWN_SUB_AGENT_TYPES = frozenset({
    # v2 agents (保留)
    "ingest", "clustering", "citation_chase",
    "translation", "video", "rad_query",
    # v3 Phase 2 agents (新增)
    "literature", "knowledge", "writing", "glossary", "capture",
})

# 各 agent_type 的预估阶段数（sub_request 推给 iOS 的 estimatedStages）
_SUB_AGENT_STAGES = {
    # v2
    "ingest": 7,
    "clustering": 5,
    "citation_chase": 7,
    "translation": 1,
    "video": 8,
    "rad_query": 5,
    # v3 Phase 2
    "literature": 5,   # search→evaluate→download→convert→extract_metadata
    "knowledge": 4,    # chunk→embed→dedup→rank
    "writing": 4,      # survey→template→citation→ai_flavor
    "glossary": 4,     # collect→search→verify→evolve
    "capture": 8,      # video download + transcribe + summarize
}


def _check_sub_agent_args(agent_type: str, args: dict, user_query: str) -> list[str]:
    """校验 sub_agent 必需参数。返回缺失参数名列表（空 = 通过）。

    各 agent_type 的必需参数：
      - ingest: 不强制（保留旧行为；空 query 会搜不到论文，由 sub_agent_task 自然返回）
      - clustering: project_id（必须基于已入库项目）
      - citation_chase: seed_title / seed_doi / seed_paper / query 至少一个
      - translation: text / query 至少一个
      - video: query（含链接的 user_query）
      - rad_query: question / query 至少一个
    """
    if agent_type == "ingest":
        return []
    if agent_type == "clustering":
        if not (args.get("project_id") or args.get("scope")):
            return ["project_id"]
        return []
    if agent_type == "citation_chase":
        if not (args.get("seed_title") or args.get("seed_doi")
                or args.get("seed_paper") or args.get("query")):
            return ["seed_title|seed_doi"]
        return []
    if agent_type == "translation":
        if not (args.get("text") or args.get("query")):
            return ["text"]
        return []
    if agent_type == "video":
        if not user_query:
            return ["query"]
        return []
    if agent_type == "rad_query":
        if not (args.get("question") or args.get("query")):
            return ["question"]
        return []
    # v3 Phase 2 agents
    if agent_type == "literature":
        if not user_query:
            return ["query"]
        return []
    if agent_type == "knowledge":
        if not (args.get("project_id") or args.get("paper_ids")):
            return ["project_id"]
        return []
    if agent_type == "writing":
        if not (args.get("project_id") or user_query):
            return ["project_id"]
        return []
    if agent_type == "glossary":
        if not (args.get("project_id") or args.get("paper_ids")):
            return ["project_id"]
        return []
    if agent_type == "capture":
        if not (user_query or args.get("url")):
            return ["url"]
        return []
    # 未知类型由调用方先拦截，这里兜底
    return [f"unknown_agent_type:{agent_type}"]

# C3: 灰区阈值 — 当所有 scenario.confidence 都低于此值时，触发 ask_user 让用户挑选
# 可通过环境变量 INTENT_ASK_THRESHOLD 覆盖（默认 0.6）。
# 设计为函数而非模块级常量，确保运行时修改 env 后生效，且测试可以 patch os.environ
# 而无须关心 import 顺序。
def _intent_ask_threshold() -> float:
    return float(os.getenv("INTENT_ASK_THRESHOLD", "0.6"))


# 向后兼容：暴露当前默认值作模块常量（不参与运行时分支判断；分支用 _intent_ask_threshold()）
INTENT_ASK_THRESHOLD = 0.6

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


# ═══════════════════════════════════════════════════════════════
# v2: fast triage 规则层词典 (main-agent-v2-design.md §4.1)
# ═══════════════════════════════════════════════════════════════

# 高频 chat 词典 — 命中即短路（<10ms，不调 LLM）
_CHAT_KEYWORDS: frozenset[str] = frozenset({
    "你好", "您好", "哈喽", "hello", "hi", "hey",
    "谢谢", "感谢", "thanks", "thank you", "多谢",
    "好的", "好", "嗯", "ok", "okay", "收到", "了解", "明白",
    "再见", "bye", "拜拜", "晚安", "早上好", "下午好",
    "在吗", "在不在",
})

# 高频 unsupported 词典 — 命中即短路
_UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset({
    "写代码", "写程序", "写python", "写java", "编程",
    "做菜", "菜谱", "做饭", "烹饪",
    "写诗", "写小说", "写故事", "创作",
    "聊电影", "聊娱乐", "聊八卦", "陪聊", "陪我聊",
    "玩游戏", "打游戏",
})

# ops 触发词 — 命中 route=ops（不短路，走轻量规划）
_OPS_KEYWORDS: tuple[str, ...] = (
    "重启", "restart", "重载", "reload",
    "pip install", "pip3 install", "npm install",
    "docker", "systemctl", "服务状态", "service_status",
    "apt install", "apt-get",
    "bash", "shell", "执行命令",
)

# meta 触发词 — 命中 route=meta（不短路，走轻量规划）
_META_KEYWORDS: tuple[str, ...] = (
    "我的偏好", "记不记得", "还记得", "search_memory",
    "你记得我", "我研究啥", "我研究什么",
    "get_user_preference", "上次对话", "历史记录",
)

# fast triage 短路阈值：仅 chat/unsupported 且 confidence >= 此值才短路
_FAST_TRIAGE_SHORTCUT_THRESHOLD = 0.85


# ═══════════════════════════════════════════════════════════════
# v2: 轻量规划 risk 校验 (main-agent-v2-design.md §4.3)
# ═══════════════════════════════════════════════════════════════

# ops 命令黑名单 — 匹配则强制 risk_level=high（覆盖 LLM 判断）
_OPS_COMMAND_BLACKLIST: list[re.Pattern] = [
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{", re.IGNORECASE),             # fork bomb
    re.compile(r"chmod\s+777", re.IGNORECASE),
    re.compile(r">\s*/dev/sd", re.IGNORECASE),
    re.compile(r"\bpip\s+install\b", re.IGNORECASE),
    re.compile(r"\bapt(-get)?\s+install\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+install\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+(run|exec|rm|rmi)\b", re.IGNORECASE),
    re.compile(r"\bcurl\s+.*\|\s*(bash|sh)", re.IGNORECASE),
    re.compile(r"\bwget\s+.*\|\s*(bash|sh)", re.IGNORECASE),
]

# meta 只读白名单 tool — 任何写操作拒绝
_META_TOOL_WHITELIST: frozenset[str] = frozenset({
    "log_view", "health_check", "search_memory",
    "get_user_preference", "list_sources", "read_paper",
    "list_collections", "search_library",
})


def _ops_command_risk(command: str) -> Optional[str]:
    """检查 ops 命令是否匹配黑名单。命中返回 'high'，否则 None。"""
    for pat in _OPS_COMMAND_BLACKLIST:
        if pat.search(command):
            return "high"
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

    # ── 其他类型透传（status / error / pong / sync_complete / 已 v10 命名的）──
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
        # v3 Phase 1: 从 agent_id 提取 user_id（格式: agent-{user_id}）
        self._user_id = "default"
        if agent_id.startswith("agent-") and agent_id != "agent-001":
            self._user_id = agent_id[6:]

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
        """v2 链路: safety_filter → fast_triage → 分支。

        chat/unsupported (conf>=0.85) → inline_reply → END
        ops → lightweight_plan(ops) → execute/confirm → END
        meta → lightweight_plan(meta) → execute → END
        business → intent_classify → scenario_plan (并行) → PlanGraph → execute → evaluate → final_reply
        """
        # v2: 收到消息后立即推 status{received}（协议 v10 要求 <200ms ack）
        await self._push_status(session_id, "received", "收到，正在分析...", level="user")

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

        # 节点 1: v2 fast triage（规则 <10ms → 未命中 → 小模型 ~500ms）
        await self._push_status(session_id, "analyzing", "正在判断请求类型...")
        triage = await self._node_fast_triage(session_id, user_content)
        self._record_event(session_id, "fast_triaged", triage.model_dump())
        logger.info(
            "⚡ fast_triage | route=%s conf=%.2f",
            triage.route, triage.confidence,
        )

        # 保守短路：仅 chat/unsupported 且 confidence>=0.85 才短路到 inline_reply
        if (triage.route in ("chat", "unsupported")
                and triage.confidence >= _FAST_TRIAGE_SHORTCUT_THRESHOLD):
            intent = IntentClassifyResult(
                intent_kind=triage.route,  # type: ignore[arg-type]
                scenarios=[],
                overall_confidence=triage.confidence,
                reasoning=triage.reasoning,
            )
            await self._node_inline_reply(session_id, user_content, intent)
            return

        # ops → 轻量规划节点
        if triage.route == "ops":
            await self._push_status(session_id, "planning", "正在规划运维操作...")
            await self._node_lightweight_plan(session_id, user_content, "ops")
            return

        # meta → 轻量规划节点
        if triage.route == "meta":
            await self._push_status(session_id, "planning", "正在处理元操作...")
            await self._node_lightweight_plan(session_id, user_content, "meta")
            return

        # business (或 fallback) → intent_classify → scenario_plan → PlanGraph → execute
        # intent_classify 保留为 business 路径的 scenario matcher (向后兼容)
        intent = await self._node_intent_classify(session_id, user_content)
        self._record_event(session_id, "intent_classified", intent.model_dump())

        # v3 Phase 1: 冷启动检测 — 知识库为空且非纯闲聊时引导文献调研
        if triage.route == "business" and await self._check_cold_start(session_id, user_content):
            intent = IntentClassifyResult(
                intent_kind="business",
                scenarios=[ScenarioMatch(
                    scenario_id="S1", confidence=1.0,
                    reasoning="冷启动引导：知识库为空，引导用户做首次文献调研",
                )],
                overall_confidence=1.0,
                reasoning="cold_start_onboarding",
            )
            logger.info("🧊 Cold start: guiding user to literature survey")

        # C3: business 但全部 scenario 都低于阈值 → 询问用户挑选
        if intent.intent_kind == "business":
            intent = await self._maybe_clarify_low_confidence(
                session_id, user_content, intent,
            )

        if intent.intent_kind == "business" and intent.scenarios:
            # 节点 2A: 业务场景规划（C2: 传入 scenarios list）
            await self._push_status(session_id, "planning", "正在规划执行方案...")
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

    # ── 节点 1: v2 fast triage ────────────────────────

    async def _node_fast_triage(self, session_id: str,
                                user_content: str) -> FastTriageResult:
        """v2 fast triage — 规则层 (<10ms) → 未命中 → 小模型 (~500ms)。

        保守短路：仅 chat/unsupported 且 confidence>=0.85 才短路到 inline_reply。
        ops/meta 不论置信度都进正式流程（轻量规划）。
        失败 fallback：business（走完整 17 场景评估兜底）。
        """
        # 1. 规则层（<10ms）
        rule_route = self._fast_triage_rule(user_content)
        if rule_route is not None:
            return FastTriageResult(
                route=rule_route,
                confidence=1.0,
                all={rule_route: 1.0},
                reasoning=f"rule_hit:{rule_route}",
            )

        # 2. 小模型层
        history = self._build_history_context(session_id, limit=10)
        messages = history + [{"role": "user", "content": user_content}]
        try:
            data = await self._llm.chat_json(
                messages=messages,
                schema=FastTriageResult,
                temperature=0.0,
                system=build_fast_triage_prompt(),
                node="fast_triage",
            )
            result = FastTriageResult.model_validate(data)
            # 补全 all dict（LLM 可能漏填）
            for k in ("chat", "meta", "unsupported", "ops", "business"):
                result.all.setdefault(k, 0.0)
            return result
        except Exception as e:
            # 失败 fallback：business（走完整 17 场景评估，最全链路兜底）
            logger.warning(f"fast_triage LLM failed: {e}, fallback to business")
            return FastTriageResult(
                route="business",
                confidence=0.5,
                all={"business": 0.5},
                reasoning=f"LLM_error_fallback_business: {e}",
            )

    @staticmethod
    def _fast_triage_rule(user_content: str) -> Optional[str]:
        """规则层：高频词典命中即返回 route，否则 None。

        chat / unsupported 命中 → 短路（confidence=1.0 >= 0.85）
        ops / meta 命中 → 返回 route 但不短路（走轻量规划）
        """
        text = user_content.strip().lower()
        if not text:
            return "chat"

        # chat 词典（精确匹配短消息）
        if text in _CHAT_KEYWORDS:
            return "chat"
        # 去标点后再判
        text_clean = re.sub(r"[。.!！?？~～,\s]+", "", text)
        if text_clean in _CHAT_KEYWORDS:
            return "chat"

        # unsupported 词典
        for kw in _UNSUPPORTED_KEYWORDS:
            if kw in text:
                return "unsupported"

        # ops 触发词
        for kw in _OPS_KEYWORDS:
            if kw in text:
                return "ops"

        # meta 触发词
        for kw in _META_KEYWORDS:
            if kw in text:
                return "meta"

        return None

    # ── v3 Phase 1: 冷启动检测 ──────────────────────────

    async def _check_cold_start(self, session_id: str, user_content: str) -> bool:
        """检测是否为冷启动用户（知识库为空）。

        返回 True 表示需要引导到文献调研流程。
        仅当用户消息不是纯闲聊/问候语时才触发。
        """
        try:
            paper_count = self._db.count_user_papers(self._user_id)
        except Exception as e:
            logger.warning(f"count_user_papers failed: {e}")
            return False

        if paper_count > 0:
            return False

        # 简单的闲聊关键词检测：避免对"你好"/"谢谢"触发冷启动
        chat_keywords = {"你好", "hello", "hi", "谢谢", "thanks", "你是谁", "who are you",
                         "帮助", "help", "再见", "bye", "好的", "ok", "嗯", "哦"}
        lower = user_content.strip().lower()
        if any(kw in lower for kw in chat_keywords) and len(lower) < 20:
            logger.info(f"Cold start suppressed: chat-like message '{user_content[:50]}'")
            return False

        logger.info(f"Cold start triggered: {paper_count} papers for user={self._user_id}")
        return True

    # ── 节点 1b: intent_classify (business 路径保留) ───

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

        threshold = _intent_ask_threshold()
        high_conf = [s for s in intent.scenarios
                     if s.confidence >= threshold]
        if high_conf:
            # 有至少一个高置信度场景 → 保留高的，丢弃低的
            if len(high_conf) != len(intent.scenarios):
                dropped = [s.scenario_id for s in intent.scenarios
                           if s.confidence < threshold]
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
        # 候选 sid 列表（按 options 出现顺序），用于把"用户回了整数索引"映射回 scenario_id
        candidate_sids = [sm.scenario_id for sm in intent.scenarios]
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
                # 显式 values 与 options 一一对应，让 iOS 可以直接回这个 id 而非文本标签
                "values": candidate_sids + ["__none__"],
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
        # 答案可能是：
        #   - 整数索引 [0, 2]
        #   - 字符串 "S1" / "S1: ..." / "S1：..." (全角冒号) / "0" / 已去前缀的标题
        #   - 上面的混合数组
        raw_values: list = []
        for a in answers:
            v = a.get("value")
            if v is None:
                v = a.get("answer")
            if isinstance(v, list):
                raw_values.extend(v)
            elif v is not None:
                raw_values.append(v)

        chosen_sids: list[str] = []
        parse_failed = False
        for val in raw_values:
            sid = self._parse_scenario_choice(val, options, candidate_sids)
            if sid == "__none__":
                # 用户明确选了"都不是"
                continue
            if sid is None:
                parse_failed = True
                continue
            if sid in SCENARIOS and sid not in chosen_sids:
                chosen_sids.append(sid)

        if parse_failed and not chosen_sids:
            # 明确区分"协议解析失败"vs"用户选都不是"，避免审计日志看不出 bug
            logger.warning(
                "C3: 无法解析用户的场景选择 raw=%s options=%s",
                raw_values, options,
            )
            self._record_event(session_id, "intent_clarify_parse_failed",
                               {"raw_values": [str(v)[:80] for v in raw_values]})
            intent.intent_kind = "chat"
            intent.scenarios = []
            return intent

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

    @staticmethod
    def _parse_scenario_choice(
        value, options: list[str], candidate_sids: list[str],
    ) -> Optional[str]:
        """把用户对 scenario_pick 的一项回答解析成 scenario_id。

        支持多种 iOS 端协议形态：
          - int 0..N-1：直接当 options 索引（最后一项是 __none__）
          - str "S1" / "s1"：直接当 scenario_id（大小写不敏感）
          - str "0" / "1"：数字字符串，当索引
          - str "S1: ..." 或 "S1：..."（全角冒号）：取冒号前 token
          - 完整 option 字面量：等值匹配 options 数组找索引
          - 完整 "都不是 / 重新描述"：返回 "__none__"
        返回 None 表示协议解析失败（调用方据此区分"用户选都不是"和"客户端协议错"）。
        """
        # 数字索引
        if isinstance(value, bool):
            return None  # 防止 True/False 被 int() 吞掉
        if isinstance(value, int):
            if 0 <= value < len(candidate_sids):
                return candidate_sids[value]
            if value == len(candidate_sids):
                return "__none__"
            return None

        if not isinstance(value, str):
            value = str(value)
        v = value.strip()
        if not v:
            return None

        # 全字面量等值（先看是不是命中"都不是"）
        if v in ("__none__", "都不是 / 重新描述", "都不是", "都不對", "都不对"):
            return "__none__"

        # 全字面量等值匹配 options
        for i, opt in enumerate(options):
            if v == opt:
                if i < len(candidate_sids):
                    return candidate_sids[i]
                return "__none__"

        # 纯数字字符串
        if v.isdigit():
            return MainAgent._parse_scenario_choice(int(v), options, candidate_sids)

        # 先按冒号（半角/全角）切首段
        for sep in (":", "：", " "):
            if sep in v:
                head = v.split(sep, 1)[0].strip()
                up = head.upper()
                if up in SCENARIOS:
                    return up
                break

        # 整串大写后是否是 scenario_id
        up = v.upper()
        if up in SCENARIOS:
            return up

        return None

    # ── 节点 2A: scenario_plan (business) ─────────────

    async def _node_scenario_plan(self, session_id: str, user_content: str,
                                   intent: IntentClassifyResult):
        """v2 业务场景规划：并行多场景 → PlanGraph 展开 → 审批 → 执行。

        v2 改造点 (items 2+5):
          - 串行 for → asyncio.gather 并行（省 (N-1)×LLM 延迟）
          - LLM 不再生成 tools[]（瘦身后只给 clarity + clarify_questions）
          - PlanGraph.expand 按 scenario_id 查路由表展开 tools[] + permissions + danger_level
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

        # v2: 并行规划多场景（asyncio.gather 替代串行 for）
        # 失败隔离：return_exceptions=True 让单个 plan 失败不影响其他
        coros = [
            self._plan_one_scenario(
                session_id, user_content, sm.scenario_id,
                scenario_idx=idx,
            )
            for idx, sm in enumerate(scenarios)
        ]
        batch_results = await asyncio.gather(*coros, return_exceptions=True)

        sub_plans: list[ScenarioPlanResult] = []
        failed_sids: list[str] = []
        for sm, res in zip(scenarios, batch_results):
            if isinstance(res, Exception):
                logger.warning(f"plan_one_scenario raised for {sm.scenario_id}: {res}")
                failed_sids.append(sm.scenario_id)
                continue
            if res is None:
                failed_sids.append(sm.scenario_id)
                continue
            sub_plans.append(res)

        if not sub_plans:
            # 全部失败 → 整体放弃（_plan_one_scenario 已对每个失败 push 过 error）
            return

        if failed_sids and len(scenarios) > 1:
            # 部分失败、部分成功 → 告知用户哪些没规划上，再继续执行成功的
            await self._push(
                session_id, "message", "text", "assistant",
                payload={"content": (
                    f"部分场景规划失败（{', '.join(failed_sids)}），"
                    f"将只执行成功的 {', '.join(p.scenario_id for p in sub_plans)} 部分。"
                )},
                priority_kind="high",
            )
            self._record_event(session_id, "scenario_partial_planned",
                               {"failed": failed_sids,
                                "succeeded": [p.scenario_id for p in sub_plans]})

        # 合并多个子 plan → 一个 ScenarioPlanResult（v2 瘦身：此时 tools[] 为空）
        plan = self._merge_sub_plans(sub_plans)

        # 合并后仍 needs_clarification（任一 sub-plan 3 轮 clarify 后 LLM 还在问）
        # → 显式告知用户并终止，避免在欠规约的 plan 上盲跑工具
        if plan.needs_clarification:
            unresolved = "；".join(
                q.question if hasattr(q, "question") else str(q)
                for q in (plan.clarification_questions or [])
            )[:500]
            await self._push(
                session_id, "message", "text", "assistant",
                payload={"content": (
                    "我对你的需求还有不清楚的地方，请补充说明后再发一次：\n"
                    + (unresolved or "（请提供更多细节）")
                )},
                priority_kind="high",
            )
            self._record_event(session_id, "plan_unresolved_clarification",
                               {"unresolved": unresolved})
            return

        # v2: PlanGraph 硬编码路由表展开 tools[]（替代 LLM 生成）
        # 用合并后的 scenarios list（用户确认 + LLM 确认的）查路由表
        history = self._build_history_context(session_id, limit=20)
        confirmed_scenarios = plan.scenarios or [
            ScenarioMatch(scenario_id=p.scenario_id, confidence=1.0, reasoning="merged")
            for p in sub_plans if p.scenario_id
        ]
        plan = plangraph_expand(confirmed_scenarios, user_content, history)
        logger.info(
            "📋 PlanGraph expanded | scenarios=%s tools=%d approval=%s danger=%s",
            [s.scenario_id for s in confirmed_scenarios],
            len(plan.tools), plan.needs_approval,
            plan.permissions_required,
        )
        self._record_event(session_id, "plangraph_expanded", {
            "scenarios": [s.scenario_id for s in confirmed_scenarios],
            "tools_count": len(plan.tools),
            "needs_approval": plan.needs_approval,
        })

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
                    node="scenario_plan",
                )
                plan = ScenarioPlanResult.model_validate(data)
            except Exception as e:
                logger.warning(f"scenario_plan LLM/schema failed (sid={scenario_id}): {e}")
                await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                                 payload={"message": f"方案生成失败 ({scenario_id}): {e}"},
                                 priority_kind="urgent")
                return None

            self._record_event(session_id, "scenario_planned", plan.model_dump())
            # v2: 确保 scenario_id / scenarios 字段被填充（瘦身后 LLM 可能不填顶层 scenario_id）
            if not plan.scenario_id:
                plan.scenario_id = scenario_id
            if not plan.scenarios:
                plan.scenarios = [ScenarioMatch(
                    scenario_id=scenario_id,  # type: ignore[arg-type]
                    confidence=plan.clarity,
                    reasoning="confirmed_by_plan",
                )]
            # v2: 从 clarity 推导 needs_clarification（瘦身后 LLM 只给 clarity + clarify_questions）
            threshold = _intent_ask_threshold()
            if plan.clarity < threshold and plan.clarification_questions:
                plan.needs_clarification = True
            elif plan.clarity >= threshold:
                plan.needs_clarification = False
            logger.info(
                "📋 scenario_plan | sid=%s clarity=%.2f clarify=%s tools=%d",
                plan.scenario_id, plan.clarity, plan.needs_clarification,
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

    # ── v3 Phase 3: 并行调度 ───────────────────────────

    def _build_parallel_groups(self, tools: list[ToolCallSpec]) -> list[list[ToolCallSpec]]:
        """构建并行执行组 — 分析 tools[] 的 depends_on 图。

        规则:
          - depends_on=[] 或未指定的工具 → 可并行
          - 依赖其他工具结果的 → 等依赖完成后再执行
          - 同组内工具并发执行（asyncio.gather）

        返回分好组的列表，每组内工具可安全并行。
        """
        if len(tools) <= 1:
            return [tools] if tools else []

        groups: list[list[ToolCallSpec]] = []
        current_group: list[ToolCallSpec] = []
        prev_names: set[str] = set()

        for tool in tools:
            deps = getattr(tool, 'depends_on', []) or []
            if not deps or all(d in prev_names for d in deps):
                current_group.append(tool)
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [tool]
            prev_names.add(tool.name)

        if current_group:
            groups.append(current_group)

        return groups

    async def _execute_single_tool(self, session_id: str, spec: ToolCallSpec) -> dict:
        """执行单个工具 — 抽取出来供并行调度使用。"""
        if spec.kind == "sub_agent":
            return await self._handle_sub_agent(session_id, spec)
        elif spec.kind == "ios_tool":
            return await self._handle_ios_tool(session_id, spec)
        else:
            return await self._handle_normal_tool(session_id, spec)

    def _merge_sub_plans(self, sub_plans: list[ScenarioPlanResult]) -> ScenarioPlanResult:
        """把多个 sub-plan 合并成一个 ScenarioPlanResult。

        - tools: 顺序拼接（call_id 已在 _plan_one_scenario 加前缀防撞车）
        - summary: 多场景时拼接 "场景 Sx: ..." 段落
        - needs_approval: 任一 true → true
        - needs_clarification: 任一 true → true（澄清问题 union；下游应再问一轮）
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

        # 任一 sub-plan 三轮 clarify 后仍 needs_clarification=True → 合并保留信号，
        # 让上游能据此再问一轮或显式中止，避免在欠规约的 plan 上盲跑工具。
        merged_clarify_qs: list = []
        for p in sub_plans:
            if p.needs_clarification:
                merged_clarify_qs.extend(p.clarification_questions or [])

        return ScenarioPlanResult(
            scenario_id="+".join(p.scenario_id for p in sub_plans),
            summary=merged_summary,
            needs_clarification=any(p.needs_clarification for p in sub_plans),
            clarification_questions=merged_clarify_qs,
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

        # v3 Phase 3: 并行调度 — 将可并行工具分组执行
        parallel_groups = self._build_parallel_groups(tools_to_run)

        for iteration in range(MAX_PLAN_ITERATIONS):
            if not tools_to_run:
                break

            # v3: 并行执行同组工具
            if len(parallel_groups) > 0:
                group = parallel_groups.pop(0)
                if len(group) > 1:
                    logger.info(f"Parallel execution: {len(group)} tools in group")
                    tasks = [self._execute_single_tool(session_id, t) for t in group]
                    group_results = await asyncio.gather(*tasks, return_exceptions=True)
                    for t, res in zip(group, group_results):
                        if isinstance(res, Exception):
                            all_results[t.name] = {"error": str(res)}
                        else:
                            all_results[t.name] = res
                    tools_to_run = [t for t in tools_to_run if t not in group]
                    continue

            # 节点 3: 执行 (单工具路径)
            await self._push_status(session_id, "executing",
                                    f"正在执行 {len(tools_to_run)} 个任务...")
            results = await self._node_execute_plan(session_id, tools_to_run)
            all_results.update(results)

            # 节点 4: 评估
            await self._push_status(session_id, "evaluating", "正在评估完成度...")
            eval_res = await self._node_evaluate_completion(
                session_id, user_content, plan, all_results,
            )
            self._record_event(session_id, "completion_evaluated", eval_res.model_dump())

            if eval_res.satisfied:
                # v2: 独立 final reply 节点（替代 v9 复用 evaluate 的 final_message）
                await self._node_final_reply(
                    session_id, user_content, plan, all_results, eval_res,
                )
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
                    node="inline_reply",
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
                    node="inline_reply",
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

    # ── 节点 2C: v2 lightweight_plan (ops + meta 共用) ──

    async def _node_lightweight_plan(self, session_id: str, user_content: str,
                                      route: str):
        """v2 轻量规划节点 — ops + meta 共用，按 route 切 prompt。

        ops: 命令生成用 doubao-seed-2.0-code；risk 双判（LLM + 黑名单强制升级 high）
        meta: 规划用 doubao-seed-2.0-lite；限只读白名单 tool
        high → ask(kind=confirm) 二次确认 → 执行；low → 直接执行
        """
        # ops 超管校验（MVP 默认超管，代码留 is_admin 检查点）
        if route == "ops":
            is_admin = self._is_admin(session_id)
            if not is_admin:
                await self._push(session_id, "error", "PERMISSION_DENIED", "system",
                                 payload={"message": "运维操作需要超管权限。",
                                          "audit": True, "route": "ops"},
                                 priority_kind="urgent")
                self._record_event(session_id, "ops_admin_denied",
                                   {"user_content": user_content[:200]})
                return

        history = self._build_history_context(session_id, limit=10)
        messages = history + [{"role": "user", "content": user_content}]
        node_name = "lightweight_plan_ops" if route == "ops" else "lightweight_plan_meta"
        system = build_lightweight_plan_prompt(route)

        try:
            data = await self._llm.chat_json(
                messages=messages,
                schema=LightweightPlanResult,
                temperature=0.1,
                system=system,
                node=node_name,
            )
            plan = LightweightPlanResult.model_validate(data)
        except Exception as e:
            logger.warning(f"lightweight_plan ({route}) LLM failed: {e}")
            await self._push(session_id, "error", "INTERNAL_ERROR", "system",
                             payload={"message": f"规划失败: {e}"},
                             priority_kind="urgent")
            return

        self._record_event(session_id, "lightweight_planned", {
            "route": route, "need_tool": plan.need_tool,
            "tool_name": plan.tool_name, "risk_level": plan.risk_level,
        })
        logger.info(
            "🔧 lightweight_plan | route=%s need_tool=%s tool=%s risk=%s",
            route, plan.need_tool, plan.tool_name, plan.risk_level,
        )

        # 不需要工具 → 直接回复
        if not plan.need_tool:
            await self._push(session_id, "message", "text", "assistant",
                             payload={"content": plan.reply_if_no_tool or "已完成。"},
                             priority_kind="high")
            return

        # meta 白名单校验
        if route == "meta" and plan.tool_name not in _META_TOOL_WHITELIST:
            await self._push(session_id, "error", "PERMISSION_DENIED", "system",
                             payload={"message": f"meta 路径只允许只读工具，{plan.tool_name} 不在白名单。"},
                             priority_kind="high")
            self._record_event(session_id, "meta_tool_rejected",
                               {"tool_name": plan.tool_name})
            return

        # ops risk 双判：LLM 给 risk_level + 系统黑名单强制升级 high
        if route == "ops":
            forced = _ops_command_risk(plan.query_or_command)
            if forced == "high":
                plan.risk_level = "high"
                logger.info("ops risk upgraded to high (blacklist match): %s",
                            plan.tool_name)

        # high → ask(kind=confirm) 二次确认
        if plan.risk_level == "high":
            call_id = _new_call_id()
            await self._push(session_id, "tool", "ask_user_question", "assistant",
                             payload={
                                 "id": call_id,
                                 "questions": [{
                                     "id": "confirm_exec",
                                     "question": (
                                         f"即将执行 {plan.tool_name}：\n"
                                         f"{plan.query_or_command[:200]}\n"
                                         "确认执行吗？"
                                     ),
                                     "type": "single_choice",
                                     "options": ["确认执行", "取消"],
                                 }],
                                 "context": f"risk_level=high, route={route}",
                             })
            self._record_event(session_id, "lightweight_confirm_requested",
                               {"tool_name": plan.tool_name, "risk": "high"})
            reply = await self._wait_ws_reply(session_id, "tool", "ask_user_question",
                                              timeout=ASK_USER_TIMEOUT_SEC)
            if reply is None:
                return
            answers = reply.get("answers", [])
            confirmed = False
            for a in answers:
                v = a.get("value") or a.get("answer", "")
                if isinstance(v, str) and ("确认" in v or v == "0"):
                    confirmed = True
                    break
            if not confirmed:
                await self._push(session_id, "message", "text", "assistant",
                                 payload={"content": "已取消执行。"},
                                 priority_kind="high")
                return
            self._record_event(session_id, "lightweight_confirmed",
                               {"tool_name": plan.tool_name})

        # 执行（复用现有 _handle_cli_tool）
        await self._push_status(session_id, "executing",
                                f"正在执行 {plan.tool_name}...")
        # 构造 ToolCallSpec 让 _handle_cli_tool 处理
        spec = ToolCallSpec(
            call_id=_new_call_id(),
            kind="ios_tool" if plan.tool_name.startswith("ios_") else "tool",
            name=plan.tool_name,
            arguments=self._parse_lightweight_args(plan),
        )
        result = await self._dispatch_one(session_id, spec)

        # 最终回复
        result_text = self._format_lightweight_result(plan, result)
        await self._push(session_id, "message", "text", "assistant",
                         payload={"content": result_text},
                         priority_kind="high")
        self._record_event(session_id, "lightweight_executed", {
            "tool_name": plan.tool_name, "route": route,
            "result_brief": str(result)[:200],
        })

    def _is_admin(self, session_id: str) -> bool:
        """ops 超管校验。MVP 阶段当前用户即超管，代码保留检查点供未来扩展。"""
        # MVP: 默认超管。未来多用户时按 session_id → user_id → role 查 DB
        return True

    @staticmethod
    def _parse_lightweight_args(plan: LightweightPlanResult) -> dict:
        """从 LightweightPlanResult 解析工具参数。"""
        args: dict[str, Any] = {}
        cmd = plan.query_or_command.strip()
        # 常见 ops 工具参数解析
        if plan.tool_name == "bash_exec":
            args["command"] = cmd
        elif plan.tool_name in ("pip_install", "apt_install"):
            args["packages"] = cmd.split()
        elif plan.tool_name in ("service_start", "service_stop", "service_status"):
            args["service"] = cmd
        elif plan.tool_name == "log_view":
            args["lines"] = 50
            if cmd.isdigit():
                args["lines"] = int(cmd)
        elif plan.tool_name == "env_config":
            args["key"] = cmd
        elif plan.tool_name in ("search_memory", "search_library"):
            args["query"] = cmd
            args["top_k"] = 5
        elif plan.tool_name == "get_user_preference":
            args["key"] = cmd
        elif plan.tool_name == "read_paper":
            args["paper_id"] = cmd
        else:
            # 通用：把 query_or_command 当 keywords/query
            if cmd:
                args["keywords"] = cmd
                args["query"] = cmd
        return args

    @staticmethod
    def _format_lightweight_result(plan: LightweightPlanResult, result: Any) -> str:
        """格式化轻量规划执行结果为用户可读文本。"""
        if isinstance(result, dict) and result.get("error"):
            return f"执行 {plan.tool_name} 失败：{result['error']}"
        if isinstance(result, str):
            return result[:2000]
        return f"{plan.tool_name} 执行完成。\n\n{str(result)[:1000]}"

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
        """启动 Celery 子 Agent，订阅 agent:reports:{task_id}，等 agent_done/failed。

        按 agent_type 分发到对应 graph runner：
          - ingest         → IngestAgent 7 阶段 (DEPRECATED, 拆分到 literature+knowledge)
          - literature     → LiteratureAgent 5 节点 (v3 Phase 2)
          - knowledge      → KnowledgeAgent 4 节点 (v3 Phase 2)
          - writing        → WritingAgent 4 节点 (v3 Phase 2)
          - glossary       → GlossaryAgent 4 节点 (v3 Phase 2)
          - capture        → VideoAgent 8 节点 (v3 Phase 2, 原 video)
          - clustering     → ClusteringAgent 5 节点
          - citation_chase → CitationChaseAgent 7 节点
          - translation    → TranslationAgent
          - video          → VideoAgent (DEPRECATED, use capture)
          - rad_query      → RADQueryAgent (DEPRECATED, merged into knowledge)

        未知 agent_type / 缺关键参数 → 推 error，**不 fallback 到 ingest**
        （fallback 正是历史 P0 bug 的根因：S6/S8/S9/S12/S13 静默跑 ingest）。
        """
        agent_type = spec.arguments.get("agent_type", spec.name)

        # 校验 agent_type — 未知直接报错，不静默 fallback 到 ingest
        if agent_type not in _KNOWN_SUB_AGENT_TYPES:
            err = (f"unknown sub_agent type: {agent_type!r} "
                   f"(known: {sorted(_KNOWN_SUB_AGENT_TYPES)})")
            logger.warning("_handle_sub_agent: %s", err)
            task_id = _new_task_id(str(agent_type))
            await self._push(session_id, "tool", "sub_result", "system", payload={
                "taskId": task_id, "name": agent_type, "status": "failed",
                "summary": f"不支持的子 Agent 类型: {agent_type}",
                "result": {"error": err},
            }, priority_kind="high")
            return {"error": err, "task_id": task_id}

        user_query = (spec.arguments.get("query")
                      or spec.arguments.get("user_query")
                      or spec.arguments.get("description") or "")

        # 校验关键参数 — 缺则报错而非用默认值瞎跑
        missing = _check_sub_agent_args(agent_type, spec.arguments, user_query)
        if missing:
            err = f"sub_agent {agent_type} missing required args: {missing}"
            logger.warning("_handle_sub_agent: %s", err)
            task_id = _new_task_id(agent_type)
            await self._push(session_id, "tool", "sub_result", "system", payload={
                "taskId": task_id, "name": agent_type, "status": "failed",
                "summary": f"缺少必需参数: {', '.join(missing)}",
                "result": {"error": err, "missing": missing},
            }, priority_kind="high")
            return {"error": err, "task_id": task_id, "missing": missing}

        task_id = _new_task_id(agent_type)

        # 启动 Celery — 把 agent_type + 完整 arguments 传过去，由 sub_agent_task 分发
        from .celery_tasks import sub_agent_task
        try:
            sub_agent_task.delay(
                agent_type=agent_type,
                user_query=user_query,
                project_id=task_id,
                agent_task_id=task_id,
                arguments=spec.arguments,
            )
        except Exception as e:
            return {"error": f"Failed to dispatch celery: {e}"}

        # 推送 sub_request — estimatedStages 按 agent_type
        await self._push(session_id, "tool", "sub_request", "assistant", payload={
            "taskId": task_id, "name": agent_type, "label": spec.name,
            "query": user_query,
            "estimatedStages": _SUB_AGENT_STAGES.get(agent_type, 5),
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
                node="evaluate_completion",
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
                next_action="fail",  # v2: 显式 fail 出口
                truth_confidence=0.0,
                reasoning=f"评估器异常 ({e})，无法确认任务是否完成",
                needs_more_tools=[],
                final_message="抱歉，评估环节出现异常，请稍后重试或换个表述。",
            )

    # ── 节点 5: v2 final reply (独立节点) ──────────────

    async def _node_final_reply(self, session_id: str, user_content: str,
                                plan: ScenarioPlanResult,
                                results: dict[str, Any],
                                eval_res: EvaluateCompletionResult):
        """v2 final reply — satisfied=true 后单次 LLM 生成完整 Markdown 回复。

        与 v9 区别：v9 复用 evaluate 的 final_message；v2 拆出独立节点，
        让 evaluate 只管判断、final reply 专注生成高质量回复。
        模型: glm-5.2 (降级 deepseek-v4-pro), node="final_reply"
        """
        await self._push_status(session_id, "done", "正在生成最终回复...")

        history = self._build_history_context(session_id, limit=10)
        results_brief = {cid: _truncate(r, 800) for cid, r in results.items()}
        prompt_user = (
            f"用户原始请求: {user_content}\n\n"
            f"执行方案: {plan.summary}\n\n"
            f"工具执行结果（JSON）:\n{json.dumps(results_brief, ensure_ascii=False, indent=2)}\n\n"
            f"评估结论: {eval_res.reasoning}\n"
            f"评估草稿: {eval_res.final_message}\n\n"
            "请基于以上信息，生成完整的中文 Markdown 回复给用户。"
            "回复应包含：执行了什么、找到了什么结果、关键发现。不要提及内部工具名。"
        )
        messages = history + [{"role": "user", "content": prompt_user}]

        final_text = ""
        try:
            resp = await self._llm.chat(
                messages=messages,
                system="你是 Paper Agent v3 的最终回复生成节点。基于工具执行结果生成高质量中文 Markdown 回复。",
                temperature=0.4,
                node="final_reply",
            )
            final_text = getattr(resp, "content", None) or str(resp)
        except Exception as e:
            logger.warning(f"final_reply LLM failed: {e}, using eval final_message")
            final_text = eval_res.final_message or "已完成。"

        final_text = final_text.strip() or "已完成。"
        await self._push(session_id, "message", "text", "assistant",
                         payload={"content": final_text},
                         priority_kind="high")
        self._record_event(session_id, "final_reply_sent",
                           {"final_text": final_text[:500]})

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
            # v10: subType 仅 tool/error 用；其余去除
            "subType": new_sub,
            "role": role,                         # v10 删除 role，但过渡期保留为可选向后兼容
            "agentId": self._agent_id,
            "sessionId": session_id,
            "timestamp": _now(),
            "payload": new_payload,
            # v10: 字段名 priority；同时保留 priorityKind 兼容旧客户端 / 旧库逻辑
            "priority": priority_kind,
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

    async def _wait_ws_reply(self, session_id: str, msg_type: str, msg_sub: str,
                              timeout: float = ASK_USER_TIMEOUT_SEC,
                              match_fn=None) -> Optional[dict]:
        """阻塞等用户/iOS 回复指定 type+subType 的消息。

        v10 兼容：调用方继续传 v9 (msg_type, msg_sub)，本函数同时接受 v10 入站类型：
          - ("tool", "ask_user_question") → 也匹配 v10 type=="ask_reply"
          - ("tool", "propose_plan")      → 也匹配 v10 type=="ask_reply"
          - ("tool", "ios_result")        → 也匹配 v10 type=="tool_result"
        v10 payload 会被归一化成 v9 形态返回（answers / approved / content 等），
        让调用方解析逻辑无需改动。

        不匹配的消息 LPUSH 到 parked sideband，主循环下轮再合并。
        """
        ws_queue = f"agent:ws:{self._agent_id}"
        parked_queue = f"agent:ws:{self._agent_id}:parked"
        deadline = asyncio.get_event_loop().time() + timeout

        # v10 ↔ v9 入站类型别名
        # v10 type → v9 (type, subType) 期望值集合
        v10_aliases: dict[str, tuple[str, str]] = {}
        if (msg_type, msg_sub) in (("tool", "ask_user_question"),
                                    ("tool", "propose_plan")):
            v10_aliases["ask_reply"] = (msg_type, msg_sub)
        if (msg_type, msg_sub) == ("tool", "ios_result"):
            v10_aliases["tool_result"] = (msg_type, msg_sub)

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
            payload = msg.get("payload", {}) or {}

            # v9 直接匹配
            matched_v9 = (got_session == session_id
                          and got_type == msg_type and got_sub == msg_sub)
            if matched_v9 and (match_fn is None or match_fn(payload)):
                return payload

            # v10 入站匹配（归一化为 v9 形态再返回）
            if got_session == session_id and got_type in v10_aliases:
                normalized = self._normalize_v10_inbound_payload(
                    got_type, msg_type, msg_sub, payload,
                )
                if match_fn is None or match_fn(normalized):
                    return normalized

            # 不匹配 → parked
            try:
                await self.redis.lpush(parked_queue, raw[1])
            except Exception:
                pass

    @staticmethod
    def _normalize_v10_inbound_payload(
        v10_type: str, expected_v9_type: str, expected_v9_sub: str,
        payload: dict,
    ) -> dict:
        """把 v10 入站 payload 归一化成 v9 调用方期望的形态。

        - ask_reply  + 期望 propose_plan       → {approved: bool, reason}
        - ask_reply  + 期望 ask_user_question  → {answers: [...]}
        - tool_result + 期望 ios_result        → {tool_call_id, content, status}
        """
        if v10_type == "ask_reply":
            ask_id = payload.get("ask_id", "")
            value = payload.get("value")
            reason = payload.get("reason", "")
            if (expected_v9_type, expected_v9_sub) == ("tool", "propose_plan"):
                return {
                    "id": ask_id,
                    "tool_call_id": ask_id,
                    "approved": bool(value) if not isinstance(value, str) else value.lower() in ("true", "yes", "approve", "approved", "1"),
                    "reason": reason,
                }
            # ask_user_question: wrap value into answers[]
            return {
                "id": ask_id,
                "tool_call_id": ask_id,
                "answers": [{"id": ask_id, "value": value, "answer": value}],
                "reason": reason,
            }

        if v10_type == "tool_result":
            return {
                "tool_call_id": payload.get("tool_call_id", ""),
                "status": payload.get("status", "done"),
                "content": payload.get("content"),
            }

        return payload

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
