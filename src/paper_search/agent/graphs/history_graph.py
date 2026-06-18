"""HistoryAgent — 历史消息处理子 Agent。

Plan + Execute 双图结构:
  Plan (2 节点):
    analyze → generate_plan
  Execute (4 节点):
    archive → merge → skip → notify

功能:
  - Agent 重启后处理 Redis 中缓存的未处理消息
  - 分析消息集合 → 识别重复/过期/已处理/待处理
  - 合并重复 → 归档过期 → 生成待办列表 → 通知主 Agent
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class HistoryState(TypedDict, total=False):
    messages: list[dict]             # 待处理消息列表
    agent_id: str
    session_id: str

    # Plan 阶段
    analysis: dict                   # 消息分析结果
    plan_actions: list[dict]         # 生成的行动计划

    # Execute 阶段
    archived: list[str]              # 已归档消息 ID
    merged: list[dict]               # 合并后的消息
    skipped: list[str]               # 跳过的消息 ID
    todo_items: list[dict]           # 生成的待办项

    # 输出
    result: Optional[dict]
    notification: Optional[dict]     # 需通知主 Agent 的内容
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# HistoryAgent
# ═══════════════════════════════════════════════════════════════


class HistoryAgent:
    """历史消息处理 Agent — Plan + Execute 双图。

    Plan Graph (2 节点):
      analyze → generate_plan
    Execute Graph (4 节点):
      archive → merge → skip → notify
    """

    def __init__(self, db, memory, llm=None, on_progress=None):
        self._db = db
        self._memory = memory
        self._llm = llm
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        """编译双图结构。"""
        # 使用 Plan + Execute 合并为单图
        builder = StateGraph(HistoryState)

        # Plan 阶段
        builder.add_node("analyze", self._analyze_node)
        builder.add_node("generate_plan", self._generate_plan_node)

        # Execute 阶段
        builder.add_node("archive", self._archive_node)
        builder.add_node("merge", self._merge_node)
        builder.add_node("skip", self._skip_node)
        builder.add_node("notify", self._notify_node)

        builder.add_edge(START, "analyze")
        builder.add_edge("analyze", "generate_plan")
        builder.add_edge("generate_plan", "archive")
        builder.add_edge("archive", "merge")
        builder.add_edge("merge", "skip")
        builder.add_edge("skip", "notify")
        builder.add_edge("notify", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("HistoryAgent not compiled")
        return self._graph

    # ── Plan 节点 ─────────────────────────────────────

    async def _analyze_node(self, state: HistoryState) -> dict:
        """分析消息集合 — 分类每一条消息。"""
        messages = state.get("messages", [])
        await self._notify("分析消息", 1, 6, f"分析 {len(messages)} 条历史消息")

        now = datetime.now(timezone.utc)
        analysis = {
            "total": len(messages),
            "expired": [],
            "duplicates": {},
            "processed": [],
            "pending": [],
        }

        seen_content = {}
        for msg in messages:
            msg_id = msg.get("id", msg.get("seq", ""))
            msg_type = msg.get("type", msg.get("subType", ""))
            content = msg.get("payload", {}).get("text", str(msg.get("content", "")))
            ts = msg.get("timestamp", "")

            # 检查是否过期 (>24h)
            expired = False
            if ts:
                try:
                    msg_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if (now - msg_time).total_seconds() > 86400:
                        expired = True
                except (ValueError, TypeError):
                    pass

            if expired:
                analysis["expired"].append(msg_id)
                continue

            # 检查重复
            content_key = content[:100] if content else str(msg.get("seq", ""))
            if content_key in seen_content:
                analysis["duplicates"].setdefault(content_key, []).append(msg_id)
            else:
                seen_content[content_key] = msg_id
                analysis["pending"].append(msg_id)

        logger.info(f"Analysis: {analysis['total']} total, {len(analysis['expired'])} expired, "
                     f"{sum(len(v) for v in analysis['duplicates'].values())} duplicates, "
                     f"{len(analysis['pending'])} pending")
        return {"analysis": analysis}

    async def _generate_plan_node(self, state: HistoryState) -> dict:
        """生成处理计划。"""
        analysis = state.get("analysis", {})
        await self._notify("生成计划", 2, 6, "生成消息处理计划")

        plan_actions = []

        # 过期消息 → 归档
        if analysis.get("expired"):
            plan_actions.append({
                "action": "archive",
                "targets": analysis["expired"],
                "reason": "消息已过期 (>24h)",
            })

        # 重复消息 → 合并
        duplicates = analysis.get("duplicates", {})
        if duplicates:
            for content_key, msg_ids in duplicates.items():
                plan_actions.append({
                    "action": "merge",
                    "targets": msg_ids,
                    "keep": msg_ids[0] if msg_ids else "",
                    "reason": f"重复消息 (content_key={content_key[:50]})",
                })

        # 已处理消息 → 跳过
        if analysis.get("processed"):
            plan_actions.append({
                "action": "skip",
                "targets": analysis["processed"],
                "reason": "消息已处理",
            })

        # 待处理消息 → 生成 todo
        if analysis.get("pending"):
            plan_actions.append({
                "action": "notify",
                "targets": analysis["pending"],
                "reason": f"{len(analysis['pending'])} 条待处理消息",
            })

        return {"plan_actions": plan_actions}

    # ── Execute 节点 ─────────────────────────────────

    async def _archive_node(self, state: HistoryState) -> dict:
        """归档过期消息。"""
        plan_actions = state.get("plan_actions", [])
        archive_action = next((a for a in plan_actions if a["action"] == "archive"), None)
        if not archive_action:
            return {}

        targets = archive_action.get("targets", [])
        await self._notify("归档", 3, 6, f"归档 {len(targets)} 条过期消息")

        # 写入长期记忆
        for msg_id in targets:
            try:
                self._memory.long_term.add("archived_messages", {"id": msg_id, "archived_at": _now()})
            except Exception:
                pass

        return {"archived": targets}

    async def _merge_node(self, state: HistoryState) -> dict:
        """合并重复消息。"""
        plan_actions = state.get("plan_actions", [])
        merge_actions = [a for a in plan_actions if a["action"] == "merge"]
        await self._notify("合并", 4, 6, f"合并 {len(merge_actions)} 组重复消息")

        merged = []
        for ma in merge_actions:
            merged.append({
                "kept": ma.get("keep", ""),
                "removed": [t for t in ma.get("targets", []) if t != ma.get("keep")],
                "reason": ma.get("reason", "Duplicate"),
            })

        return {"merged": merged}

    async def _skip_node(self, state: HistoryState) -> dict:
        """跳过已处理消息。"""
        plan_actions = state.get("plan_actions", [])
        skip_action = next((a for a in plan_actions if a["action"] == "skip"), None)
        targets = skip_action.get("targets", []) if skip_action else []
        await self._notify("跳过", 5, 6, f"跳过 {len(targets)} 条已处理消息")

        return {"skipped": targets}

    async def _notify_node(self, state: HistoryState) -> dict:
        """通知主 Agent。"""
        plan_actions = state.get("plan_actions", [])
        notify_action = next((a for a in plan_actions if a["action"] == "notify"), None)
        await self._notify("通知", 6, 6, "生成通知摘要")

        pending_count = len(notify_action.get("targets", [])) if notify_action else 0
        archived = len(state.get("archived", []))
        merged_groups = len(state.get("merged", []))
        skipped = len(state.get("skipped", []))

        notification = {
            "title": "历史消息处理完成",
            "body": (f"您离开期间有 {pending_count + archived + sum(len(m.get('removed', [])) for m in state.get('merged', []))} 条新消息，"
                     f"其中 {sum(len(m.get('removed', [])) for m in state.get('merged', []))} 条已合并，"
                     f"{archived} 条已归档，"
                     f"{pending_count} 条待处理"),
            "category": "history_processed",
            "data": {
                "archived": archived,
                "merged_groups": merged_groups,
                "skipped": skipped,
                "pending": pending_count,
            },
        }

        result = {
            "total_processed": archived + skipped + merged_groups,
            "pending": pending_count,
            "notification": notification,
        }

        logger.info(f"HistoryAgent complete: {result}")
        return {"result": result, "notification": notification}

    # ── 辅助 ─────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  History [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
