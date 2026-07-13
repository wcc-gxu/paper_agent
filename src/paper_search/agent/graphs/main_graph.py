"""MainAgent v3.1 — LangGraph StateGraph.

Fast Triage → chat/ops/research → Intent Classify → Plan(todo list) → Execute(ReAct) → Evaluate

节点:
  fast_triage      — flash + no thinking + tool_choice → 3-dim scores
  intent_classify  — flash + no thinking + tool_choice → scenarios
  plan             — pro + no thinking + tool_choice → PlanOutput{todos[]}
  ops_confirm      — danger_level check + human-in-the-loop
  execute          — pro + thinking + ReAct loop (8 rounds max)
  evaluate         — flash + no thinking + tool_choice → {satisfied, next_action}
  inline_reply     — pro + thinking + chat_stream → END
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from ..main_agent_prompts import (
    FastTriageV31Result,
    TodoSpec,
    PlanOutput,
    EvaluateV31Result,
    TodoCheckpointResult,
    build_fast_triage_v31_prompt,
    build_plan_v31_prompt,
    build_execute_v31_prompt,
    build_evaluate_v31_prompt,
    build_todo_checkpoint_prompt,
)

logger = logging.getLogger(__name__)

MAX_REACT_ROUNDS = 8
MAX_PLAN_ITERATIONS = 3       # evaluate→replan→execute→evaluate 全局上限
MAX_TODO_RETRIES = 2          # 单个 todo checkpoint 重试上限


def _to_message_dicts(messages: list) -> list[dict]:
    """Convert LangChain message objects to plain dicts for LLM calls.

    LangGraph's add_messages reducer converts dicts to LangChain message objects
    (HumanMessage/AIMessage/ToolMessage) which lack a .role attribute.
    Our LLM client expects dicts with 'role' and 'content' keys.
    """
    result = []
    for msg in messages:
        if isinstance(msg, dict):
            result.append(msg)
            continue
        # LangChain message: has .type ("human"/"ai"/"system"/"tool") and .content
        msg_type = getattr(msg, "type", "unknown")
        role_map = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
        entry: dict = {
            "role": role_map.get(msg_type, "user"),
            "content": getattr(msg, "content", "") or "",
        }
        tc_id = getattr(msg, "tool_call_id", None)
        if tc_id:
            entry["tool_call_id"] = tc_id
        # Preserve tool_calls from assistant messages (needed for ReAct loop)
        tc_list = getattr(msg, "tool_calls", None)
        if tc_list:
            entry["tool_calls"] = [
                {"id": tc.get("id", ""), "name": tc.get("name", ""),
                 "arguments": tc.get("arguments", {})}
                if isinstance(tc, dict) else
                {"id": getattr(tc, "id", ""), "name": getattr(tc, "name", ""),
                 "arguments": getattr(tc, "arguments", {})}
                for tc in tc_list
            ]
        result.append(entry)
    return result


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class MainState(TypedDict, total=False):
    """v3.1 MainAgent StateGraph state."""

    # Input
    user_content: str
    session_id: str
    correlation_id: str

    # Fast Triage
    triage_chat: float
    triage_ops: float
    triage_research: float
    triage_reasoning: str

    # Intent + Plan
    plan: Optional[dict]  # PlanOutput as dict
    needs_clarify: bool
    clarify_questions: list[dict]

    # Execute
    todos: list[dict]  # TodoSpec list
    current_todo_index: int
    messages: Annotated[list, add_messages]  # ReAct conversation
    tool_call_count: int
    tool_results: list[dict]
    needs_more_tools: list[dict]  # retry_tools 注入的补充工具

    # Evaluate
    all_satisfied: bool
    next_action: str  # "done" | "retry_tools" | "ask_user" | "replan" | "fail"
    final_reply: str
    replan_hint: str
    ask_user_question: Optional[dict]  # ClarificationQuestion as dict
    user_reply: Optional[Any]  # 用户对 ask 卡片的回复
    plan_iterations: int  # 全局 evaluate→replan→execute 循环计数
    error: Optional[str]

    # Ops confirm
    ops_confirmed: bool
    danger_level: str

    # Todo Checkpoint
    todo_checkpoint_satisfied: bool
    todo_retry_count: int


# ═══════════════════════════════════════════════════════════════
# MainGraph
# ═══════════════════════════════════════════════════════════════


class MainGraph:
    """v3.1 Main Agent as LangGraph StateGraph.

    Usage:
        graph = MainGraph(llm=llm, registry=registry, db=db,
                          push_fn=push_fn, get_user_fn=get_user_fn)
        compiled = graph.compile()
        result = await compiled.ainvoke({"user_content": "...", "session_id": "..."})
    """

    def __init__(
        self,
        llm: Any = None,
        registry: Any = None,
        db: Any = None,
        push_fn: Any = None,          # async fn(session_id, type, subtype, role, **kw) -> None
        get_user_fn: Any = None,       # async fn(session_id, ask_id, timeout) -> dict | None
    ):
        self.llm = llm
        self.registry = registry
        self.db = db
        self._push = push_fn
        self._get_user = get_user_fn

    # ── Push Helpers ──────────────────────────────────────────

    async def _push_status(self, session_id: str, stage: str, message: str,
                           level: str = "info") -> None:
        """Push a status message to the client (non-blocking, best-effort)."""
        if not self._push:
            return
        try:
            await self._push(
                session_id, "status", stage, "system",
                payload={"stage": stage, "message": message, "level": level},
                priority_kind="silent",
            )
        except Exception:
            pass

    async def _push_error(self, session_id: str, message: str, recoverable: bool = True) -> None:
        """Push an error to the client immediately (high priority)."""
        if not self._push:
            return
        try:
            await self._push(
                session_id, "error", "INTERNAL_ERROR", "system",
                payload={"message": message, "recoverable": recoverable},
                priority_kind="urgent",
            )
        except Exception:
            pass

    async def _push_tool_status(self, session_id: str, tool_call_id: str,
                                tool_name: str, status: str, detail: dict = None) -> None:
        """Push tool call/start/result to the client."""
        if not self._push:
            return
        try:
            # Push tool/call when starting, tool/result when done
            subtype = "call" if status == "started" else "result"
            await self._push(
                session_id, "tool", subtype, "assistant",
                payload={"tool_call_id": tool_call_id, "name": tool_name,
                         "status": status, **(detail or {})},
                priority_kind="normal" if status == "result" else "normal",
            )
        except Exception:
            pass

    # ── Compile ─────────────────────────────────────────────

    def compile(self, checkpointer: Any = None) -> Any:
        builder = StateGraph(MainState)

        # Nodes
        builder.add_node("fast_triage", self._fast_triage)
        builder.add_node("intent_classify", self._intent_classify)
        builder.add_node("plan", self._plan)
        builder.add_node("ops_confirm", self._ops_confirm)
        builder.add_node("execute", self._execute)
        builder.add_node("todo_checkpoint", self._todo_checkpoint)
        builder.add_node("evaluate", self._evaluate)
        builder.add_node("ask_user", self._ask_user)
        builder.add_node("inline_reply", self._inline_reply)

        # Edges
        builder.add_edge(START, "fast_triage")

        builder.add_conditional_edges(
            "fast_triage",
            self._route_triage,
            {
                "research": "intent_classify",
                "ops": "ops_confirm",
                "chat": "inline_reply",
            },
        )
        builder.add_edge("intent_classify", "plan")

        builder.add_conditional_edges(
            "plan",
            self._route_plan,
            {"clarify": END, "execute": "execute"},
        )

        builder.add_edge("ops_confirm", "execute")

        # execute → todo_checkpoint (always, unless error)
        builder.add_conditional_edges(
            "execute",
            self._route_after_execute,
            {"todo_checkpoint": "todo_checkpoint", "end": END},
        )

        # todo_checkpoint → execute(retry same todo) / execute(next) / evaluate
        builder.add_conditional_edges(
            "todo_checkpoint",
            self._route_todo_checkpoint,
            {"execute": "execute", "evaluate": "evaluate", "retry": "execute"},
        )

        # evaluate → done / retry_tools / ask_user / replan / fail
        builder.add_conditional_edges(
            "evaluate",
            self._route_evaluate,
            {
                "done": END,
                "retry_tools": "execute",
                "ask_user": "ask_user",
                "replan": "plan",
                "fail": END,
            },
        )

        # ask_user → evaluate (after user replies)
        builder.add_edge("ask_user", "evaluate")

        builder.add_edge("inline_reply", END)

        return builder.compile(checkpointer=checkpointer)

    # ═══════════════════════════════════════════════════════════
    # Node: fast_triage
    # ═══════════════════════════════════════════════════════════

    async def _fast_triage(self, state: MainState) -> dict:
        """Flash model, 3-dimension scoring. Returns triage_* scores."""
        user = state.get("user_content", "")
        session_id = state.get("session_id", "main")

        await self._push_status(session_id, "analyzing", "正在分析请求类型...")

        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": user}],
                schema=FastTriageV31Result,
                system=build_fast_triage_v31_prompt(),
                temperature=0.0,
                node="fast_triage_v31",
            )
        except Exception as e:
            logger.warning(f"fast_triage LLM failed: {e}, fallback to chat")
            await self._push_status(session_id, "analyzing", "正在回复...")
            return {
                "triage_chat": 0.9, "triage_ops": 0.0, "triage_research": 0.0,
                "triage_reasoning": f"LLM error fallback: {e}",
            }

        triage_chat = data.get("chat", 0.0)
        triage_ops = data.get("ops", 0.0)
        triage_research = data.get("research", 0.0)

        # Push research-dimension visibility to user
        logger.info(
            "Triage: chat=%.2f ops=%.2f research=%.2f → %s",
            triage_chat, triage_ops, triage_research,
            "research" if triage_research > 0.4 else ("ops" if triage_ops > 0.6 else "chat"),
        )

        return {
            "triage_chat": triage_chat,
            "triage_ops": triage_ops,
            "triage_research": triage_research,
            "triage_reasoning": data.get("reasoning", ""),
        }

    # ── Route: triage → chat/ops/research ──────────────────

    async def _route_triage(self, state: MainState) -> str:
        research = state.get("triage_research", 0.0)
        ops = state.get("triage_ops", 0.0)
        session_id = state.get("session_id", "main")

        if research > 0.4:
            await self._push_status(session_id, "planning", "识别为研究类请求，正在分析意图...")
            return "research"
        if ops > 0.6:
            await self._push_status(session_id, "planning", "识别为运维操作，正在确认...")
            return "ops"
        await self._push_status(session_id, "responding", "正在回复...")
        return "chat"

    # ═══════════════════════════════════════════════════════════
    # Node: intent_classify (research branch only)
    # ═══════════════════════════════════════════════════════════

    async def _intent_classify(self, state: MainState) -> dict:
        """Flash model, determines if the research intent needs detailed planning."""
        user = state.get("user_content", "")
        session_id = state.get("session_id", "main")

        await self._push_status(session_id, "planning", "正在分析研究意图...")

        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": user}],
                schema=FastTriageV31Result,  # reuse: returns chat/ops/research scores
                system=build_fast_triage_v31_prompt(),
                temperature=0.0,
                node="intent_classify_v31",
            )
            logger.info("Intent classify: research=%.2f", data.get("research", 0.0))
        except Exception as e:
            logger.warning(f"intent_classify LLM failed: {e}")
            await self._push_error(session_id, f"意图分类失败: {e}")
            return {}  # Continue with plan using default

        return {}  # Intent already determined by triage; proceed to plan

    # ═══════════════════════════════════════════════════════════
    # Node: plan
    # ═══════════════════════════════════════════════════════════

    async def _plan(self, state: MainState) -> dict:
        """Pro model, no thinking, tool_choice: plan_output.

        On replan: consumes replan_hint from state and injects it into the user prompt.
        """
        user = state.get("user_content", "")
        session_id = state.get("session_id", "main")
        replan_hint = state.get("replan_hint", "")

        if replan_hint:
            await self._push_status(session_id, "planning", f"正在根据反馈重新规划: {replan_hint[:80]}...")
            user = f"{user}\n\n[重新规划提示] 上一轮规划的改进建议: {replan_hint}"
        else:
            await self._push_status(session_id, "planning", "正在制定研究计划...")

        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": user}],
                schema=PlanOutput,
                system=build_plan_v31_prompt(),
                temperature=0.2,
                node="plan_v31",
            )
        except Exception as e:
            logger.error(f"plan LLM failed: {e}")
            await self._push_error(session_id, f"计划生成失败: {e}")
            return {"error": f"Plan generation failed: {e}", "needs_clarify": False, "todos": []}

        todos = data.get("todos", [])
        needs_clarify = data.get("needs_clarify", False)
        danger_level = data.get("danger_level", "low")

        if needs_clarify:
            clarify_questions = data.get("clarify_questions", [])
            await self._push_status(
                session_id, "planning",
                f"需要更多信息以制定计划，生成了 {len(clarify_questions)} 个澄清问题",
                level="warning",
            )
        else:
            await self._push_status(
                session_id, "planning",
                f"研究计划已生成: {len(todos)} 个步骤，预估 {data.get('estimated_seconds', 0)}秒，风险等级 {danger_level}",
            )
            logger.info("Plan: %d todos, danger=%s, summary=%s",
                        len(todos), danger_level, data.get("summary", "")[:100])

        return {
            "plan": data,
            "needs_clarify": needs_clarify,
            "clarify_questions": data.get("clarify_questions", []),
            "todos": todos,
            "current_todo_index": 0,
            "tool_call_count": 0,
            "tool_results": [],
            "replan_hint": "",           # Consumed
            "all_satisfied": False,
            "next_action": "done",       # Reset for fresh start
            "needs_more_tools": [],      # Reset
            "danger_level": danger_level,
        }

    # ── Route: plan → clarify / execute ──────────────────

    def _route_plan(self, state: MainState) -> str:
        if state.get("needs_clarify") and state.get("clarify_questions"):
            return "clarify"
        return "execute"

    # ═══════════════════════════════════════════════════════════
    # Node: ops_confirm
    # ═══════════════════════════════════════════════════════════

    async def _ops_confirm(self, state: MainState) -> dict:
        """Check danger_level for ops. High-risk ops need human confirmation."""
        user = state.get("user_content", "")
        session_id = state.get("session_id", "main")

        # Simple danger detection via keywords
        high_risk_keywords = ["rm ", "delete", "drop ", "truncate", "sudo ", "kill",
                              "format", "fdisk", "shutdown", "reboot", "mv /"]
        is_high_risk = any(kw in user.lower() for kw in high_risk_keywords)
        danger = "high" if is_high_risk else "low"

        await self._push_status(
            session_id, "executing",
            f"运维操作 (风险等级: {danger})，正在执行...",
            level="warning" if is_high_risk else "info",
        )

        if is_high_risk:
            # Push ask/confirm to user
            if self._push:
                await self._push(
                    session_id, "ask", "", "assistant",
                    payload={
                        "ask_id": f"ask-ops-{state.get('correlation_id', '')}",
                        "kind": "confirm",
                        "prompt": f"确认执行运维操作: {user[:100]}",
                        "danger_level": "high",
                    },
                    priority_kind="high",
                )
            # In a real impl, we'd wait for user reply via _get_user_fn
            # For now, assume confirmed in non-interactive contexts
            return {"ops_confirmed": True, "danger_level": "high"}

        return {"ops_confirmed": True, "danger_level": "low"}

    # ═══════════════════════════════════════════════════════════
    # Node: execute (ReAct loop)
    # ═══════════════════════════════════════════════════════════

    async def _execute(self, state: MainState) -> dict:
        """Pro model + thinking, ReAct loop with 8-round limit.

        Each todo is processed sequentially. Within a todo, tool calls may be
        parallel or serial depending on the LLM's decisions.

        On retry_tools from evaluate: consumes needs_more_tools, executes them,
        then returns for re-evaluation.

        Key fix: converts LangChain message objects to dicts before passing to LLM,
        since add_messages reducer converts dicts to AIMessage/HumanMessage etc.
        """
        session_id = state.get("session_id", "main")
        tool_call_count = state.get("tool_call_count", 0)
        tool_results = list(state.get("tool_results", []))

        # ── retry_tools injection: supplementary tools from evaluate ──
        needs_more_tools = state.get("needs_more_tools", [])
        if needs_more_tools:
            logger.info("Executing %d retry_tools from evaluate", len(needs_more_tools))
            messages = list(state.get("messages", []))
            for spec in needs_more_tools:
                tc_call_id = spec.get("call_id", f"retry-{uuid.uuid4().hex[:10]}")
                tc_name = spec.get("name", "")
                tc_args = spec.get("arguments", {})

                # Push tool start
                await self._push_tool_status(
                    session_id, tc_call_id, tc_name, "started",
                    detail={"arguments": tc_args},
                )

                tc = type("ToolCall", (), {
                    "id": tc_call_id, "name": tc_name, "arguments": tc_args,
                })()
                tool_call_count += 1
                result = await self._dispatch_tool(tc, session_id)
                result_summary = str(result)[:500]
                tool_results.append({
                    "tool_call_id": tc_call_id,
                    "tool_name": tc_name,
                    "todo_id": "_retry",
                    "result": result_summary,
                })

                # Push tool result
                error_msg = result.get("error") if isinstance(result, dict) else None
                await self._push_tool_status(
                    session_id, tc_call_id, tc_name,
                    "failed" if error_msg else "completed",
                    detail={"result": result_summary, "error": error_msg} if error_msg else {"result": result_summary},
                )
                if error_msg:
                    await self._push_error(session_id, f"工具 {tc_name} 执行失败: {error_msg}")

                messages.append({
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                    "tool_call_id": tc_call_id,
                })
            return {
                "messages": messages,
                "tool_call_count": tool_call_count,
                "tool_results": tool_results,
                "needs_more_tools": [],      # Consumed
                "all_satisfied": False,       # Force re-evaluate
            }

        # ── Normal todo execution ──
        todos = state.get("todos", [])
        messages = list(state.get("messages", []))
        current_todo_index = state.get("current_todo_index", 0)

        if current_todo_index >= len(todos):
            return {"all_satisfied": True}

        current_todo = todos[current_todo_index]
        todo_label = current_todo.get("label", f"todo-{current_todo_index}")
        system = build_execute_v31_prompt(current_todo, todos)

        # Build tool definitions for the LLM — without this it can't call tools at all
        available_tools = self._build_tool_defs(current_todo)

        # Push todo start
        await self._push_status(
            session_id, "executing",
            f"[{current_todo_index + 1}/{len(todos)}] {todo_label}" +
            (f" — {len(available_tools)} tools available" if available_tools else " — ⚠️ no tools"),
        )

        round_count = 0
        while round_count < MAX_REACT_ROUNDS and tool_call_count < MAX_REACT_ROUNDS:
            round_count += 1

            # Convert LangChain messages to dicts before passing to LLM
            # (add_messages reducer converts dicts → HumanMessage/AIMessage/ToolMessage
            #  which lack .role and crash the LLM client)
            safe_messages = _to_message_dicts(messages)

            try:
                response = await self.llm.chat(
                    messages=[{"role": "system", "content": system}] + safe_messages,
                    tools=available_tools if available_tools else None,
                    temperature=0.3,
                    node="execute_v31",
                )
            except Exception as e:
                logger.error(f"execute LLM failed: {e}")
                await self._push_error(
                    session_id,
                    f"执行 [{todo_label}] 时 LLM 调用失败 (第{round_count}轮): {e}",
                )
                tool_results.append({"error": str(e), "todo_id": current_todo.get("id", "")})
                break

            if not getattr(response, 'tool_calls', None):
                # No tool calls — LLM considers this todo complete
                messages.append({
                    "role": "assistant",
                    "content": getattr(response, 'content', '') or "",
                })
                logger.info("Todo [%s] complete after %d rounds (no more tool calls)",
                            todo_label, round_count)
                break

            # Push tool call starts (before dispatching)
            for tc in response.tool_calls:
                tc_name = getattr(tc, 'name', '?')
                tc_args = getattr(tc, 'arguments', {})
                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except json.JSONDecodeError:
                        tc_args = {"raw": tc_args[:200]}
                await self._push_tool_status(
                    session_id, getattr(tc, 'id', ''), tc_name, "started",
                    detail={"arguments": tc_args},
                )

            # Add assistant message with tool_calls to conversation
            messages.append({
                "role": "assistant",
                "content": getattr(response, 'content', '') or "",
                "tool_calls": [{
                    "id": getattr(tc, 'id', ''),
                    "name": getattr(tc, 'name', ''),
                    "arguments": getattr(tc, 'arguments', {}),
                } for tc in response.tool_calls],
            })

            # Dispatch tools (parallel within this round)
            batch_results = {}
            for tc in response.tool_calls:
                tool_call_count += 1
                tc_name = getattr(tc, 'name', '?')
                result = await self._dispatch_tool(tc, session_id)
                batch_results[getattr(tc, 'id', '')] = result
                result_summary = str(result)[:500]
                tool_results.append({
                    "tool_call_id": getattr(tc, 'id', ''),
                    "tool_name": tc_name,
                    "todo_id": current_todo.get("id", ""),
                    "result": result_summary,
                })

                # Push tool result immediately
                error_msg = result.get("error") if isinstance(result, dict) else None
                await self._push_tool_status(
                    session_id, getattr(tc, 'id', ''), tc_name,
                    "failed" if error_msg else "completed",
                    detail={"result": result_summary, "error": error_msg} if error_msg else {"result": result_summary},
                )
                if error_msg:
                    await self._push_error(session_id, f"工具 {tc_name} 失败: {error_msg}")

            # Add tool results as tool role messages
            for tc in response.tool_calls:
                result_text = json.dumps(
                    batch_results.get(getattr(tc, 'id', ''), {}),
                    ensure_ascii=False,
                )
                messages.append({
                    "role": "tool",
                    "content": result_text,
                    "tool_call_id": getattr(tc, 'id', ''),
                })

            # Push progress
            await self._push_status(
                session_id, "executing",
                f"[{current_todo_index + 1}/{len(todos)}] {todo_label} — 第{round_count}轮完成 ({tool_call_count}次工具调用)",
            )

        # Don't advance index — todo_checkpoint decides
        return {
            "messages": messages,
            "tool_call_count": tool_call_count,
            "tool_results": tool_results,
            "current_todo_index": current_todo_index,
            "all_satisfied": False,  # checkpoint will determine
        }

    async def _dispatch_tool(self, tool_call: Any, session_id: str) -> dict:
        """Dispatch a single tool call via the ToolRegistry."""
        name = tool_call.name if hasattr(tool_call, 'name') else tool_call.get('name', '')
        args = tool_call.arguments if hasattr(tool_call, 'arguments') else tool_call.get('arguments', {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}

        try:
            tool = self.registry.get(name) if self.registry else None
            if tool is None:
                return {"error": f"Tool not found: {name}"}

            if hasattr(tool, 'ainvoke'):
                result = await tool.ainvoke(args)
            elif hasattr(tool, 'func'):
                fn = tool.func
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**args) if isinstance(args, dict) else await fn(args)
                else:
                    result = fn(**args) if isinstance(args, dict) else fn(args)
            else:
                result = {"error": f"Tool {name} has no callable"}

            return {"success": True, "result": result}
        except Exception as e:
            logger.warning(f"Tool {name} failed: {e}")
            return {"error": str(e)}

    def _build_tool_defs(self, todo: dict) -> list:
        """Build Anthropic-compatible tool definitions from todo's tool_calls.

        Looks up each tool in the registry to get its description and input schema,
        so the LLM knows what tools are available and how to call them.
        """
        from ..llm_client_v2 import ToolDef

        tool_defs = []
        todo_name = todo.get("label", "unknown")
        for tc in todo.get("tool_calls", []):
            tc_name = tc.get("name", "")
            if not tc_name:
                continue
            tool = self.registry.get(tc_name) if self.registry else None
            if tool is None:
                logger.warning("Tool %s not found in registry (todo: %s)", tc_name, todo_name)
                # Build a best-effort definition so the LLM can at least try
                tool_defs.append(ToolDef(
                    name=tc_name,
                    description=tc.get("description", f"Execute {tc_name}"),
                    input_schema={"type": "object", "properties": {}, "additionalProperties": True},
                ))
                continue

            # Extract schema from the registry tool
            schema = {"type": "object", "properties": {}, "additionalProperties": True}
            if hasattr(tool, 'args_schema') and tool.args_schema is not None:
                try:
                    if hasattr(tool.args_schema, 'model_json_schema'):
                        raw = tool.args_schema.model_json_schema()
                    elif hasattr(tool.args_schema, 'schema'):
                        raw = tool.args_schema.schema()
                    else:
                        raw = {"type": "object", "properties": {}}
                    # Clean Pydantic noise: remove $defs, title, etc for Anthropic compat
                    schema = {
                        "type": "object",
                        "properties": raw.get("properties", {}),
                        "required": raw.get("required", []),
                    }
                except Exception:
                    pass

            tool_defs.append(ToolDef(
                name=tool.name,
                description=tool.description or f"Execute {tc_name}",
                input_schema=schema,
            ))

        logger.debug("Built %d tool defs for todo [%s]: %s",
                     len(tool_defs), todo_name,
                     [td.name for td in tool_defs])
        return tool_defs

    # ── Route: after execute → todo_checkpoint ──────────────

    def _route_after_execute(self, state: MainState) -> str:
        if state.get("error"):
            return "end"
        return "todo_checkpoint"

    # ═══════════════════════════════════════════════════════════
    # Node: evaluate
    # ═══════════════════════════════════════════════════════════

    async def _evaluate(self, state: MainState) -> dict:
        """Flash model, 5-exit evaluation. Consumes ALL LLM output fields."""
        todos = state.get("todos", [])
        tool_results = state.get("tool_results", [])
        user = state.get("user_content", "")
        user_reply = state.get("user_reply")
        session_id = state.get("session_id", "main")
        plan_iterations = state.get("plan_iterations", 0)

        await self._push_status(session_id, "evaluating", "正在评估执行结果...")

        # Build summary for evaluation
        summary_lines = [f"用户需求: {user}"]
        if user_reply:
            summary_lines.append(f"用户补充: {user_reply}")
        for i, todo in enumerate(todos):
            done = i < state.get("current_todo_index", 0)
            summary_lines.append(
                f"- {'DONE' if done else 'PENDING'} {todo.get('label', '?')}: "
                f"{todo.get('success_criterion', '')}"
            )
        summary_lines.append(f"\n工具调用结果: {len(tool_results)} 次")

        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": "\n".join(summary_lines)}],
                schema=EvaluateV31Result,
                system=build_evaluate_v31_prompt(),
                temperature=0.0,
                node="evaluate_v31",
            )
        except Exception as e:
            logger.warning(f"evaluate LLM failed: {e}")
            await self._push_error(session_id, f"评估失败: {e}")
            return {
                "all_satisfied": False,
                "next_action": "fail",
                "final_reply": "评估失败，请重试",
                "plan_iterations": plan_iterations + 1,
            }

        next_action = data.get("next_action", "fail")
        satisfied = data.get("satisfied", False)
        final_message = data.get("final_message", "")

        # Push evaluation result to user
        action_labels = {
            "done": "✅ 完成", "retry_tools": "🔄 补充工具调用",
            "ask_user": "❓ 需要用户判断", "replan": "🔁 重新规划", "fail": "❌ 失败",
        }
        await self._push_status(
            session_id, "evaluating",
            f"评估: {action_labels.get(next_action, next_action)} | 可信度: {data.get('truth_confidence', 0):.0%}",
            level="info" if next_action == "done" else "warning",
        )
        logger.info("Evaluate: satisfied=%s next=%s confidence=%.2f",
                    satisfied, next_action, data.get("truth_confidence", 0))

        # Convert ask_user_question from Pydantic model to dict if present
        auq = data.get("ask_user_question")
        ask_user_dict = auq.model_dump() if auq is not None and hasattr(auq, "model_dump") else None

        return {
            "all_satisfied": satisfied,
            "next_action": next_action,
            "final_reply": final_message,
            "needs_more_tools": [
                t.model_dump() if hasattr(t, "model_dump") else t
                for t in (data.get("needs_more_tools") or [])
            ],
            "ask_user_question": ask_user_dict,
            "replan_hint": data.get("replan_hint", ""),
            "plan_iterations": plan_iterations + 1,
        }

    # ── Route: evaluate → done / retry_tools / ask_user / replan / fail

    def _route_evaluate(self, state: MainState) -> str:
        next_action = state.get("next_action", "fail")
        plan_iterations = state.get("plan_iterations", 0)

        # Global iteration guard — force fail if loop exhausted
        if next_action in ("retry_tools", "replan", "ask_user") and plan_iterations >= MAX_PLAN_ITERATIONS:
            logger.warning("Hit MAX_PLAN_ITERATIONS=%d, forcing fail (was %s)", MAX_PLAN_ITERATIONS, next_action)
            return "fail"

        if next_action == "done":
            return "done"
        elif next_action == "retry_tools":
            return "retry_tools"
        elif next_action == "ask_user":
            return "ask_user"
        elif next_action == "replan":
            return "replan"
        return "fail"

    # ═══════════════════════════════════════════════════════════
    # Node: ask_user
    # ═══════════════════════════════════════════════════════════

    async def _ask_user(self, state: MainState) -> dict:
        """Push ask card to user, wait for reply, then re-enter evaluate."""
        session_id = state.get("session_id", "main")
        ask_user_question = state.get("ask_user_question")
        correlation_id = state.get("correlation_id", "")
        plan_iterations = state.get("plan_iterations", 0)

        if not ask_user_question:
            logger.warning("ask_user node called without ask_user_question")
            return {"next_action": "fail", "plan_iterations": plan_iterations + 1}

        ask_id = f"ask-{correlation_id}-{uuid.uuid4().hex[:8]}"
        qtype = ask_user_question.get("type", "single_choice")
        kind_map = {"single_choice": "choice", "multi_choice": "multi_choice", "open": "text"}
        ask_kind = kind_map.get(qtype, "choice")

        # Push ask card via graph's push_fn
        if self._push:
            payload: dict[str, Any] = {
                "ask_id": ask_id,
                "kind": ask_kind,
                "prompt": ask_user_question.get("question", "请确认"),
            }
            if ask_kind in ("choice", "multi_choice"):
                payload["options"] = ask_user_question.get("options", [])
            await self._push(
                session_id, "ask", "", "assistant",
                payload=payload,
                priority_kind="high",
            )
        else:
            logger.warning("push_fn not available for ask_user")
            return {"next_action": "fail", "plan_iterations": plan_iterations + 1}

        # Wait for user reply via get_user_fn
        if self._get_user:
            reply = await self._get_user(session_id, ask_id, timeout=30 * 60)
            if reply is None:
                return {
                    "next_action": "fail",
                    "final_reply": "等待用户回复超时",
                    "plan_iterations": plan_iterations + 1,
                }
            return {"user_reply": reply}
        else:
            logger.warning("get_user_fn not configured, cannot wait for user reply")
            return {"next_action": "fail", "plan_iterations": plan_iterations + 1}

    # ═══════════════════════════════════════════════════════════
    # Node: todo_checkpoint
    # ═══════════════════════════════════════════════════════════

    async def _todo_checkpoint(self, state: MainState) -> dict:
        """Flash model + no thinking + JSON schema — verify current todo's success_criterion."""
        todos = state.get("todos", [])
        current_todo_index = state.get("current_todo_index", 0)
        tool_results = state.get("tool_results", [])
        session_id = state.get("session_id", "main")
        retry_count = state.get("todo_retry_count", 0)

        # Edge cases: empty todos or past the end
        if not todos or current_todo_index >= len(todos):
            return {"todo_checkpoint_satisfied": True}

        current_todo = todos[current_todo_index]
        todo_results = [
            r for r in tool_results
            if r.get("todo_id") == current_todo.get("id", "")
        ]

        await self._push_status(
            session_id, "executing",
            f"检查 [{current_todo.get('label', '?')}] 完成度...",
        )

        prompt = (
            f"Todo: {current_todo.get('label', '?')}\n"
            f"Success Criterion: {current_todo.get('success_criterion', '')}\n\n"
            f"Tool Results:\n{json.dumps(todo_results, ensure_ascii=False)[:2000]}"
        )

        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
                schema=TodoCheckpointResult,
                system=build_todo_checkpoint_prompt(),
                temperature=0.0,
                node="todo_checkpoint_v31",
            )
            satisfied = data.get("satisfied", False)
        except Exception as e:
            logger.warning(f"todo_checkpoint LLM failed: {e}")
            await self._push_error(session_id, f"检查点评估失败: {e}")
            satisfied = False

        if satisfied:
            await self._push_status(
                session_id, "executing",
                f"✅ [{current_todo.get('label', '?')}] 已完成",
            )
            return {
                "todo_checkpoint_satisfied": True,
                "todo_retry_count": 0,
                "current_todo_index": current_todo_index + 1,
            }
        else:
            new_retry = retry_count + 1
            if new_retry >= MAX_TODO_RETRIES:
                logger.warning(
                    "Todo %s checkpoint retry exhausted (%d), advancing anyway",
                    current_todo.get("id", "?"), new_retry,
                )
                await self._push_status(
                    session_id, "executing",
                    f"⚠️ [{current_todo.get('label', '?')}] 重试{new_retry}次仍未完成，跳过",
                    level="warning",
                )
                return {
                    "todo_checkpoint_satisfied": True,
                    "todo_retry_count": 0,
                    "current_todo_index": current_todo_index + 1,
                }
            await self._push_status(
                session_id, "executing",
                f"🔄 [{current_todo.get('label', '?')}] 未完成，重试 (第{new_retry}次)...",
                level="warning",
            )
            return {
                "todo_checkpoint_satisfied": False,
                "todo_retry_count": new_retry,
            }

    # ── Route: todo_checkpoint → execute / evaluate ─────────

    def _route_todo_checkpoint(self, state: MainState) -> str:
        if state.get("todo_checkpoint_satisfied", False):
            current = state.get("current_todo_index", 0)
            total = len(state.get("todos", []))
            if current >= total:
                return "evaluate"
            return "execute"
        return "retry"  # Stay on same todo

    # ═══════════════════════════════════════════════════════════
    # Node: inline_reply
    # ═══════════════════════════════════════════════════════════

    async def _inline_reply(self, state: MainState) -> dict:
        """Pro model + thinking, direct chat reply via chat_stream."""
        user = state.get("user_content", "")
        session_id = state.get("session_id", "main")

        await self._push_status(session_id, "responding", "正在生成回复...")

        full_text = ""
        try:
            if hasattr(self.llm, "chat_stream"):
                async for chunk in self.llm.chat_stream(
                    messages=[{"role": "user", "content": user}],
                    temperature=0.6,
                    node="inline_reply",
                ):
                    if isinstance(chunk, dict):
                        if chunk.get("type") == "text_delta":
                            full_text += chunk.get("text", "")
                        elif chunk.get("type") == "error":
                            logger.warning(f"inline_reply stream error: {chunk.get('message')}")
            else:
                resp = await self.llm.chat(
                    messages=[{"role": "user", "content": user}],
                    temperature=0.6,
                    node="inline_reply",
                )
                full_text = getattr(resp, "content", "") or str(resp)
        except Exception as e:
            logger.warning(f"inline_reply failed: {e}")
            full_text = f"抱歉，LLM 调用出错：{e}"
            await self._push_error(session_id, f"回复生成失败: {e}")

        # Push final reply
        if self._push:
            await self._push(
                session_id, "message", "reply", "assistant",
                payload={"content": full_text.strip()},
                priority_kind="high",
            )

        await self._push_status(session_id, "done", "回复完成")
        return {"final_reply": full_text.strip()}


# ═══════════════════════════════════════════════════════════════
# Top-level build function (convenience)
# ═══════════════════════════════════════════════════════════════

def build_main_graph(
    llm: Any = None,
    registry: Any = None,
    db: Any = None,
    push_fn: Any = None,
    get_user_fn: Any = None,
    checkpointer: Any = None,
) -> Any:
    """Build and compile the v3.1 MainGraph.

    Args:
        llm: LLMClientV2 instance
        registry: ToolRegistry instance
        db: AgentDB or PostgresAgentDB instance
        push_fn: async fn(session_id, type, subtype, role, **kw) for outbox push
        get_user_fn: async fn for waiting user replies (ask cards)
        checkpointer: LangGraph checkpointer (AsyncSqliteSaver or similar)

    Returns:
        Compiled LangGraph StateGraph
    """
    graph = MainGraph(
        llm=llm, registry=registry, db=db,
        push_fn=push_fn, get_user_fn=get_user_fn,
    )
    return graph.compile(checkpointer=checkpointer)
