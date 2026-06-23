"""PlanGraph — 硬编码 17 场景路由表 + 参数抽取 + tools[] 展开.

替代 v9 让 LLM 生成 tools[] 的职责。LLM 只判场景号 + clarity，
PlanGraph 按 scenario_id 查表展开固定的 ToolCallSpec 列表。

设计依据:
  - docs/development/main-agent-v2-design.md §4.5
  - docs/development/plangraph-routing.md §2 (17 场景路由表)

三条硬规则 (plangraph-routing.md §1.2):
  1. scenario_id → 执行体是查表，不是生成
  2. 参数抽取是确定性代码，不是 LLM
  3. danger_level 硬映射 permissions
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .main_agent_prompts import (
    SCENARIOS,
    ClarificationQuestion,
    ScenarioMatch,
    ScenarioPlanResult,
    ToolCallSpec,
)


# ═══════════════════════════════════════════════════════════════
# danger_level → approval 硬映射 (plangraph-routing.md §2.3)
# ═══════════════════════════════════════════════════════════════

DANGER_TO_APPROVAL: dict[str, dict[str, Any]] = {
    "low":    {"needs_approval": False, "priority_kind": "normal", "audit": False},
    "medium": {"needs_approval": True,  "priority_kind": "high",   "audit": False},
    "high":   {"needs_approval": True,  "priority_kind": "high",   "audit": True},
}

_DANGER_RANK = {"low": 0, "medium": 1, "high": 2}


def _max_danger(levels: list[str]) -> str:
    """取复合意图中最高 danger_level。"""
    if not levels:
        return "low"
    return max(levels, key=lambda d: _DANGER_RANK.get(d, 0))


# ═══════════════════════════════════════════════════════════════
# 路由表定义
# ═══════════════════════════════════════════════════════════════


@dataclass
class RouteEntry:
    """单个 scenario 的路由条目。"""

    scenario_id: str
    name: str
    kind: Literal["sub_agent", "tool", "ios_tool"]
    exec_name: str               # sub_agent type 或 tool name
    defaults: dict[str, Any] = field(default_factory=dict)
    danger_level: Literal["low", "medium", "high"] = "low"
    permissions: list[str] = field(default_factory=list)
    estimated_time_seconds: int = 30
    tier: Literal["A", "B", "C"] = "A"
    status: str = ""


# S16 由 ops route 承接，不进 scenario_plan 评估范围 (设计文档 §三)
SCENARIO_ROUTES: dict[str, RouteEntry] = {
    "S1": RouteEntry(
        "S1", "文献调研", "tool", "search_papers",
        defaults={"sources": "arxiv,semantic_scholar", "year_from": 2022, "max_results": 20},
        danger_level="low", permissions=["search"], estimated_time_seconds=30,
        tier="A", status="可用",
    ),
    "S2": RouteEntry(
        "S2", "综述生成", "sub_agent", "ingest",
        defaults={"max_results": 50, "enable_verify": False},
        danger_level="medium", permissions=["search", "download"], estimated_time_seconds=600,
        tier="A", status="可用",
    ),
    "S3": RouteEntry(
        "S3", "前沿追踪", "tool", "create_subscription",
        defaults={"sources": "arxiv,semantic_scholar"},
        danger_level="low", permissions=["subscription", "notification"], estimated_time_seconds=5,
        tier="C", status="缺工具",
    ),
    "S4": RouteEntry(
        "S4", "论文精读", "tool", "read_paper",
        defaults={},
        danger_level="low", permissions=[], estimated_time_seconds=30,
        tier="B", status="extract_knowledge stub",
    ),
    "S5": RouteEntry(
        "S5", "方法对比", "tool", "search_papers",
        defaults={"sources": "arxiv,semantic_scholar", "year_from": 2022, "max_results": 10},
        danger_level="low", permissions=["search"], estimated_time_seconds=60,
        tier="B", status="无 compare 工具",
    ),
    "S6": RouteEntry(
        "S6", "研究空白", "sub_agent", "clustering",
        defaults={"n_clusters": 0},
        danger_level="low", permissions=[], estimated_time_seconds=120,
        tier="C", status="dispatch bug",
    ),
    "S7": RouteEntry(
        "S7", "进度查看", "tool", "paper_status",
        defaults={},
        danger_level="low", permissions=[], estimated_time_seconds=5,
        tier="A", status="可用",
    ),
    "S8": RouteEntry(
        "S8", "聚类全景", "sub_agent", "clustering",
        defaults={"n_clusters": 0},
        danger_level="low", permissions=[], estimated_time_seconds=120,
        tier="C", status="dispatch bug",
    ),
    "S9": RouteEntry(
        "S9", "引用追溯", "sub_agent", "citation_chase",
        defaults={"max_depth": 2, "direction": "both"},
        danger_level="low", permissions=["citation_chase"], estimated_time_seconds=300,
        tier="C", status="dispatch bug",
    ),
    "S10": RouteEntry(
        "S10", "RAG问答", "sub_agent", "rad_query",
        defaults={"top_k": 5, "use_fulltext": True},
        danger_level="low", permissions=[], estimated_time_seconds=60,
        tier="B", status="graph 待接",
    ),
    "S11": RouteEntry(
        "S11", "批量搜索", "tool", "batch_search",
        defaults={"download": False},
        danger_level="medium", permissions=["search"], estimated_time_seconds=300,
        tier="B", status="stub",
    ),
    "S12": RouteEntry(
        "S12", "翻译", "sub_agent", "translation",
        defaults={"action": "translate_query", "direction": "zh2en"},
        danger_level="low", permissions=[], estimated_time_seconds=60,
        tier="C", status="dispatch bug",
    ),
    "S13": RouteEntry(
        "S13", "视频解析", "sub_agent", "video",
        defaults={},
        danger_level="high", permissions=["video_download"], estimated_time_seconds=600,
        tier="C", status="dispatch bug",
    ),
    "S14": RouteEntry(
        "S14", "导出/清理", "tool", "paper_export",
        defaults={"format": "bibtex", "keep_pdfs": True},
        danger_level="low", permissions=[], estimated_time_seconds=10,
        tier="B", status="export stub",
    ),
    "S15": RouteEntry(
        "S15", "iOS自动化", "ios_tool", "ios_calendar_add",
        defaults={},
        danger_level="low", permissions=[], estimated_time_seconds=10,
        tier="A", status="可用",
    ),
    "S17": RouteEntry(
        "S17", "记忆操作", "tool", "search_memory",
        defaults={"top_k": 5},
        danger_level="low", permissions=[], estimated_time_seconds=10,
        tier="A", status="可用",
    ),
}


# ═══════════════════════════════════════════════════════════════
# 跨场景依赖 (plangraph-routing.md §4.2)
# ═══════════════════════════════════════════════════════════════

# 仅当上游场景也在当前 scenarios list 中时才加 depends_on
COMPOUND_DEPS: dict[str, list[str]] = {
    "S12": ["S1", "S2", "S9"],   # 翻译常依赖上游产出
    "S8": ["S1", "S2"],          # 聚类依赖已入库语料
    "S6": ["S1", "S2"],          # 研究空白依赖语料
    "S14": ["S1", "S2", "S9"],   # 导出依赖有数据
    "S17": ["S2", "S4", "S13"],  # 存记忆依赖上游产出
}


# ═══════════════════════════════════════════════════════════════
# 参数抽取（确定性代码，非 LLM）— plangraph-routing.md §5.4
# ═══════════════════════════════════════════════════════════════

# 搜索触发词前缀
_SEARCH_TRIGGERS = re.compile(
    r"^(请|帮我|麻烦|能不能|可以|可否)?\s*"
    r"(找|搜|搜索|查找|查|检索|看看|找点|找些|找几篇|搜一下|查一下|搜索一下)"
    r"(一下|些|点|几篇)?\s*",
    re.IGNORECASE,
)

# 视频 URL（http 链接 或 平台名+口令）
_VIDEO_URL_RE = re.compile(
    r"https?://[^\s，。、）)　]+"
    r"|(?:抖音|TikTok|B站|bilibili|youtube|youtu\.be|小红书|xhslink)[^\s，。、）)　]*",
    re.IGNORECASE,
)

_PAPER_ID_RE = re.compile(
    r"(?:paper[_-]?id|论文\s*(?:id|编号|ID))\s*[:：]\s*([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

_PROJECT_ID_RE = re.compile(
    r"(?:project[_-]?id|项目\s*(?:id|编号|ID))\s*[:：]\s*([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

# "对比 A 和 B" / "比较 A 与 B" / "A vs B"
_METHOD_COMPARE_RE = re.compile(
    r"(?:对比|比较)\s*(.+?)\s*(?:和|与|跟|以及|和|vs\.?|versus)\s*(.+)",
    re.IGNORECASE,
)


def _extract_query(user_msg: str) -> str:
    """从用户消息抽取搜索主题词（去掉触发词）。"""
    text = user_msg.strip()
    text = _SEARCH_TRIGGERS.sub("", text, count=1)
    # 去掉尾部 "的论文/文章/文献" 等
    text = re.sub(
        r"(的)?\s*(论文|文章|文献|paper|papers)\s*[。.!！?？]*\s*$",
        "", text, flags=re.IGNORECASE,
    )
    return text.strip() or user_msg.strip()


def _extract_video_url(user_msg: str) -> str:
    """从消息抽视频分享链接/口令。"""
    m = _VIDEO_URL_RE.search(user_msg)
    return m.group(0) if m else user_msg.strip()


def _extract_paper_ref(user_msg: str, history: list) -> str:
    """从消息或 history 抽取论文引用（paper_id 或 title）。"""
    m = _PAPER_ID_RE.search(user_msg)
    if m:
        return m.group(1)
    # 去掉触发词后剩下的当 title
    text = re.sub(
        r"^(请|帮我|麻烦)?\s*(精读|读|看看|看一下|阅读|分析|解读)\s*",
        "", user_msg.strip(), flags=re.IGNORECASE,
    )
    text = re.sub(r"(这篇|那篇|此篇|这个|那个)\s*", "", text)
    return text.strip() or user_msg.strip()


def _extract_project_id(history: list) -> str:
    """从 history 找最近提到的 project_id。找不到返回空串。"""
    for msg in reversed(history):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        m = _PROJECT_ID_RE.search(content)
        if m:
            return m.group(1)
    return ""


def _extract_method_pair(user_msg: str) -> tuple[str, str]:
    """从 "对比 A 和 B" 抽取两个方法名。"""
    m = _METHOD_COMPARE_RE.search(user_msg)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def _extract_params(sid: str, user_msg: str, history: list) -> dict[str, Any]:
    """按 scenario_id 抽取所需参数，缺省用默认值。"""
    if sid == "S1":
        return {
            "keywords": _extract_query(user_msg),
            "sources": "arxiv,semantic_scholar",
            "year_from": 2022,
            "max_results": 20,
        }
    if sid == "S2":
        return {
            "query": _extract_query(user_msg),
            "user_query": user_msg,
            "max_results": 50,
            "enable_verify": "严格" in user_msg or "校验" in user_msg,
        }
    if sid == "S3":
        kw = _extract_query(user_msg)
        return {
            "keywords": kw,
            "name": kw[:20],
            "sources": "arxiv,semantic_scholar",
        }
    if sid == "S4":
        return {
            "paper_id": _extract_paper_ref(user_msg, history),
        }
    if sid == "S5":
        # S5 特殊：由 _expand_scenario 生成两个并行 search_papers
        a, _b = _extract_method_pair(user_msg)
        return {"keywords": a or _extract_query(user_msg)}
    if sid in ("S6", "S8"):
        return {
            "project_id": _extract_project_id(history),
            "n_clusters": 0,
        }
    if sid == "S7":
        return {"project_id": _extract_project_id(history)}
    if sid == "S9":
        return {
            "seed_title": _extract_paper_ref(user_msg, history),
            "project_id": _extract_project_id(history),
            "max_depth": 2,
            "direction": "both",
        }
    if sid == "S10":
        return {
            "question": user_msg,
            "project_id": _extract_project_id(history),
            "top_k": 5,
            "use_fulltext": True,
        }
    if sid == "S11":
        return {"file_path": user_msg.strip(), "download": False}
    if sid == "S12":
        return {
            "text": user_msg,
            "action": "translate_query",
            "direction": "zh2en",
            "project_id": _extract_project_id(history),
        }
    if sid == "S13":
        return {
            "user_query": user_msg,
            "project_id": f"sub-video-{uuid.uuid4().hex[:12]}",
        }
    if sid == "S14":
        action = "clean" if any(
            k in user_msg for k in ("清理", "清空", "删除", "删掉")
        ) else "export"
        return {
            "project_id": _extract_project_id(history),
            "format": "bibtex",
            "keep_pdfs": True,
            "action": action,
        }
    if sid == "S15":
        return _extract_ios_params(user_msg)
    if sid == "S17":
        return {"query": user_msg, "top_k": 5}
    return {}


def _extract_ios_params(user_msg: str) -> dict[str, Any]:
    """从消息判 iOS 工具名 + 参数。"""
    if "日历" in user_msg or "提醒" in user_msg and "日历" not in user_msg:
        if "日历" in user_msg:
            return {"_ios_tool": "ios_calendar_add", "title": user_msg[:50]}
        return {"_ios_tool": "ios_reminder_add", "title": user_msg[:50]}
    if "通知" in user_msg:
        return {"_ios_tool": "ios_notification_local", "title": user_msg[:30], "body": user_msg}
    if "文件" in user_msg:
        return {"_ios_tool": "ios_file_write", "path": "", "content": user_msg}
    return {"_ios_tool": "ios_device_info"}


# ═══════════════════════════════════════════════════════════════
# expand — 主入口
# ═══════════════════════════════════════════════════════════════


def _new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:10]}"


def _expand_scenario(
    sid: str, idx: int, user_msg: str, history: list,
) -> list[ToolCallSpec]:
    """单个 scenario 展开为 1~N 个 ToolCallSpec。

    大部分场景展开为单 tool；S5 (方法对比) 展开为两个并行 search。
    """
    route = SCENARIO_ROUTES.get(sid)
    if route is None:
        return []

    # S5 特殊：两个并行 search_papers (method A + method B)
    if sid == "S5":
        a, b = _extract_method_pair(user_msg)
        if not a:
            a = _extract_query(user_msg)
        tools: list[ToolCallSpec] = []
        if a:
            tools.append(ToolCallSpec(
                call_id=f"s{idx}_a_{_new_call_id()}",
                kind="tool", name="search_papers",
                arguments={
                    "keywords": a, "sources": "arxiv,semantic_scholar",
                    "year_from": 2022, "max_results": 10,
                },
            ))
        if b:
            tools.append(ToolCallSpec(
                call_id=f"s{idx}_b_{_new_call_id()}",
                kind="tool", name="search_papers",
                arguments={
                    "keywords": b, "sources": "arxiv,semantic_scholar",
                    "year_from": 2022, "max_results": 10,
                },
            ))
        return tools or [ToolCallSpec(
            call_id=f"s{idx}_{_new_call_id()}",
            kind=route.kind, name=route.exec_name,
            arguments={**route.defaults, **_extract_params(sid, user_msg, history)},
        )]

    # 默认：单 tool/sub_agent
    params = _extract_params(sid, user_msg, history)
    return [ToolCallSpec(
        call_id=f"s{idx}_{_new_call_id()}",
        kind=route.kind,
        name=route.exec_name,
        arguments={**route.defaults, **params},
    )]


def expand(
    scenarios: list[ScenarioMatch],
    user_msg: str,
    history: Optional[list] = None,
) -> ScenarioPlanResult:
    """按 scenario_id 查路由表展开 tools[] + permissions + danger_level。

    输入:
      scenarios: 来自 scenario_plan LLM 的场景列表
      user_msg: 原始用户消息（抽参数用）
      history: 滑动窗口（抽 paper_id/project_id 用）

    输出: 完整 ScenarioPlanResult（含 tools[], needs_approval, permissions, ...）
          供下游 _execute_with_evaluation 使用。
    """
    history = history or []
    all_tools: list[ToolCallSpec] = []
    all_permissions: list[str] = []
    danger_levels: list[str] = []
    total_estimated = 0
    summary_parts: list[str] = []
    # 记录每个 scenario 的最后一个 call_id，用于跨场景依赖
    last_call_ids: dict[str, str] = {}

    for idx, sm in enumerate(scenarios):
        sid = sm.scenario_id
        route = SCENARIO_ROUTES.get(sid)
        if route is None:
            # S16 不在此表（由 ops route 承接）；未知 sid 跳过
            continue

        scenario_tools = _expand_scenario(sid, idx, user_msg, history)
        if not scenario_tools:
            continue

        # 跨场景依赖：首个 tool depends_on 上游末个 tool
        upstream = COMPOUND_DEPS.get(sid, [])
        for up_sid in upstream:
            if up_sid in last_call_ids:
                scenario_tools[0].depends_on.append(last_call_ids[up_sid])

        all_tools.extend(scenario_tools)
        last_call_ids[sid] = scenario_tools[-1].call_id

        # 累加 permissions / danger / estimated
        for p in route.permissions:
            if p not in all_permissions:
                all_permissions.append(p)
        danger_levels.append(route.danger_level)
        total_estimated += route.estimated_time_seconds

        sc = SCENARIOS.get(sid, {})
        summary_parts.append(f"【{sid}】{sc.get('name', sid)}")

    # S14 clean action → danger 强制 high
    if any(
        t.name == "paper_clean" or t.arguments.get("action") == "clean"
        for t in all_tools
    ):
        danger_levels.append("high")

    max_danger = _max_danger(danger_levels)
    approval_info = DANGER_TO_APPROVAL[max_danger]

    merged_sid = "+".join(s.scenario_id for s in scenarios) if scenarios else ""
    summary = "；".join(summary_parts)[:300] if summary_parts else "执行计划"

    return ScenarioPlanResult(
        scenarios=list(scenarios),
        clarity=0.8,  # PlanGraph 只在 clarity >= 阈值时被调用
        scenario_id=merged_sid,
        summary=summary,
        needs_clarification=False,
        clarification_questions=[],
        needs_approval=approval_info["needs_approval"],
        permissions_required=all_permissions,  # type: ignore[arg-type]
        estimated_time_seconds=total_estimated,
        tools=all_tools,
    )


def render_summary(scenarios: list[ScenarioMatch]) -> str:
    """按路由表模板拼 summary（供 PlanGraph 输出用）。"""
    parts = []
    for sm in scenarios:
        sc = SCENARIOS.get(sm.scenario_id, {})
        parts.append(f"【{sm.scenario_id}】{sc.get('name', sm.scenario_id)}")
    return "；".join(parts)[:300] if parts else "执行计划"


__all__ = [
    "SCENARIO_ROUTES",
    "COMPOUND_DEPS",
    "DANGER_TO_APPROVAL",
    "RouteEntry",
    "expand",
    "render_summary",
]
