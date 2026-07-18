"""v4.0 Celery ReAct 执行器 — 将 MainGraph 的 execute 阶段拆分为独立 Task。

daemon 在 plan_approve 后提交 react_execute Celery task，立即回到 BRPOP。
Worker 执行 ReAct loop（≤8 轮），通过 outbox_publish_sync 推送进度/结果。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from .celery_app import app as celery_app

logger = logging.getLogger(__name__)

REACT_MAX_ROUNDS = int(os.getenv("REACT_MAX_ROUNDS", "8"))


def _get_db():
    from .pgdb import PostgresAgentDB
    return PostgresAgentDB()


def _get_redis():
    import redis
    return redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _get_llm(provider: str = "deepseek"):
    from .llm_client_v2 import get_llm_client
    return get_llm_client(provider=provider)


def _outbox_push(envelope: dict, user_id: str = ""):
    from .outbox import outbox_key

    r = _get_redis()
    try:
        data = json.dumps(envelope, ensure_ascii=False, default=str)
        r.lpush(outbox_key(user_id), data)
    except Exception as e:
        logger.error(f"react_executor: outbox push failed: {e}")


def _build_tool_defs(registry, todos: list[dict]) -> list[dict]:
    """根据 plan 中的 todos 构建可用 tool 定义列表。"""
    tool_names = set()
    for todo in todos:
        tools = todo.get("tools", [])
        if isinstance(tools, list):
            tool_names.update(tools)
    if not tool_names:
        tool_defs = registry.get_all_tool_defs()
    else:
        tool_defs = []
        for name in tool_names:
            td = registry.get_tool_def(name)
            if td:
                tool_defs.append(td)
    return tool_defs


@celery_app.task(bind=True, max_retries=3, acks_late=True)
def react_execute(self, plan_args: dict):
    """Celery ReAct 执行任务。

    plan_args = {
        "plan_id": str,
        "agent_id": str,
        "user_id": str,
        "session_id": str,
        "todos": [{"id": "todo-1", "label": "...", "tools": [...], "status": "pending"}],
        "context": {"document_id": "...", "preferences": {...}, "planning_prompt": "..."},
        "llm_provider": "deepseek",
        "danger_level": "medium",
    }
    """
    try:
        return _run_react(plan_args)
    except Exception as e:
        logger.error(f"react_execute failed: plan_id={plan_args.get('plan_id')}: {e}", exc_info=True)
        user_id = plan_args.get("user_id", "")
        agent_id = plan_args.get("agent_id", "")
        session_id = plan_args.get("session_id", "main")
        _outbox_push({
            "type": "error", "subType": "TASK_FAILED",
            "agentId": agent_id, "sessionId": session_id,
            "payload": {"code": "TASK_FAILED", "message": str(e)[:200]},
        }, user_id=user_id)
        raise


def _run_react(plan_args: dict) -> dict:
    from .tool_registry import ToolRegistry
    from .main_agent_prompts import REACT_SYSTEM_PROMPT

    plan_id = plan_args.get("plan_id", "")
    agent_id = plan_args.get("agent_id", "")
    user_id = plan_args.get("user_id", "")
    session_id = plan_args.get("session_id", "main")
    todos = plan_args.get("todos", [])
    context = plan_args.get("context", {})
    llm_provider = plan_args.get("llm_provider", "deepseek")
    planning_prompt = context.get("planning_prompt", "")
    preferences = context.get("preferences", {})

    db = _get_db()
    llm = _get_llm(llm_provider)
    registry = ToolRegistry.get_instance()

    tool_defs = _build_tool_defs(registry, todos)
    messages = [
        {"role": "system", "content": REACT_SYSTEM_PROMPT},
        {"role": "user", "content": f"执行以下计划:\n{planning_prompt}\n\n待执行步骤:\n{json.dumps(todos, ensure_ascii=False, indent=2)}"},
    ]

    if preferences:
        pref_text = f"\n用户偏好: {json.dumps(preferences, ensure_ascii=False)}"
        messages[1]["content"] += pref_text

    tool_results = []
    round_num = 0

    while round_num < REACT_MAX_ROUNDS:
        round_num += 1

        try:
            result = llm.chat(
                messages=messages,
                tools=tool_defs if tool_defs else None,
                temperature=0.3,
            )
        except Exception as e:
            logger.error(f"react_execute round {round_num}: LLM error: {e}")
            _outbox_push({
                "type": "status",
                "agentId": agent_id, "sessionId": session_id,
                "payload": {"stage": "executing", "message": f"第 {round_num} 轮 LLM 调用失败，重试中..."},
            }, user_id=user_id)
            continue

        raw = getattr(result, "content", "") or ""
        tool_calls = getattr(result, "tool_calls", None) or []

        if not tool_calls:
            _outbox_push({
                "type": "message", "subType": "reply",
                "agentId": agent_id, "sessionId": session_id,
                "priority": "high",
                "payload": {"content": raw or "执行完成"},
            }, user_id=user_id)
            return {"status": "done", "plan_id": plan_id, "rounds": round_num,
                    "summary": raw[:500]}

        executed = []
        for tc in tool_calls:
            tc_id = tc.get("id", f"tc-{round_num}")
            tc_name = tc.get("function", {}).get("name", "") or tc.get("name", "")
            tc_args = tc.get("function", {}).get("arguments", {}) or tc.get("arguments", {})

            _outbox_push({
                "type": "tool_execution",
                "agentId": agent_id, "sessionId": session_id,
                "payload": {
                    "tool_call_id": tc_id, "name": tc_name,
                    "status": "running", "arguments": tc_args,
                },
            }, user_id=user_id)

            try:
                tc_result = registry.execute_tool(tc_name, tc_args)
                tool_results.append({"tool": tc_name, "result": str(tc_result)[:2000]})
                _outbox_push({
                    "type": "tool_execution",
                    "agentId": agent_id, "sessionId": session_id,
                    "payload": {
                        "tool_call_id": tc_id, "name": tc_name,
                        "status": "completed",
                        "result_summary": str(tc_result)[:500],
                    },
                }, user_id=user_id)
            except Exception as e:
                tool_results.append({"tool": tc_name, "error": str(e)})
                _outbox_push({
                    "type": "tool_execution",
                    "agentId": agent_id, "sessionId": session_id,
                    "payload": {
                        "tool_call_id": tc_id, "name": tc_name,
                        "status": "failed", "error": str(e)[:200],
                    },
                }, user_id=user_id)

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"工具执行结果:\n{json.dumps(tool_results[-5:], ensure_ascii=False)}"})

    _outbox_push({
        "type": "message", "subType": "reply",
        "agentId": agent_id, "sessionId": session_id,
        "priority": "high",
        "payload": {"content": f"执行完成（已达最大轮次 {REACT_MAX_ROUNDS}）"},
    }, user_id=user_id)
    return {"status": "max_rounds", "plan_id": plan_id, "rounds": round_num}
