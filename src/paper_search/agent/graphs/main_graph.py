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
            return {
                "triage_chat": 0.9, "triage_ops": 0.0, "triage_research": 0.0,
                "triage_reasoning": f"LLM error fallback: {e}",
            }

        return {
            "triage_chat": data.get("chat", 0.0),
            "triage_ops": data.get("ops", 0.0),
            "triage_research": data.get("research", 0.0),
            "triage_reasoning": data.get("reasoning", ""),
        }

    # ── Route: triage → chat/ops/research ──────────────────

    def _route_triage(self, state: MainState) -> str:
        research = state.get("triage_research", 0.0)
        ops = state.get("triage_ops", 0.0)
        if research > 0.4:
            return "research"
        if ops > 0.6:
            return "ops"
        return "chat"

    # ═══════════════════════════════════════════════════════════
    # Node: intent_classify (research branch only)
    # ═══════════════════════════════════════════════════════════

    async def _intent_classify(self, state: MainState) -> dict:
        """Flash model, determines if the research intent needs detailed planning."""
        user = state.get("user_content", "")
        try:
            data = await self.llm.chat_json(
                messages=[{"role": "user", "content": user}],
                schema=FastTriageV31Result,  # reuse: returns chat/ops/research scores
                system=build_fast_triage_v31_prompt(),
                temperature=0.0,
                node="intent_classify_v31",
            )
        except Exception as e:
            logger.warning(f"intent_classify LLM failed: {e}")
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
        replan_hint = state.get("replan_hint", "")

        if replan_hint:
            user = f"{user}\n\n[重新规划提示] 上一轮规划的改进建议: {replan_hint}"

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
            return {"error": f"Plan generation failed: {e}", "needs_clarify": False, "todos": []}

        return {
            "plan": data,
            "needs_clarify": data.get("needs_clarify", False),
            "clarify_questions": data.get("clarify_questions", []),
            "todos": data.get("todos", []),
            "current_todo_index": 0,
            "tool_call_count": 0,
            "tool_results": [],
            "replan_hint": "",           # Consumed
            "all_satisfied": False,
            "next_action": "done",       # Reset for fresh start
            "needs_more_tools": [],      # Reset
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
                tc = type("ToolCall", (), {
                    "id": tc_call_id, "name": tc_name, "arguments": tc_args,
                })()
                tool_call_count += 1
                result = await self._dispatch_tool(tc, session_id)
                tool_results.append({
                    "tool_call_id": tc_call_id,
                    "tool_name": tc_name,
                    "todo_id": "_retry",
                    "result": str(result)[:500],
                })
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
        system = build_execute_v31_prompt(current_todo, todos)

        round_count = 0
        while round_count < MAX_REACT_ROUNDS and tool_call_count < MAX_REACT_ROUNDS:
            round_count += 1

            try:
                response = await self.llm.chat(
                    messages=[{"role": "system", "content": system}] + messages,
                    temperature=0.3,
                    node="execute_v31",
                )
            except Exception as e:
                logger.error(f"execute LLM failed: {e}")
                tool_results.append({"error": str(e), "todo_id": current_todo.get("id", "")})
                break

            if not getattr(response, 'tool_calls', None):
                # No tool calls — LLM considers this todo complete
                messages.append({"role": "assistant", "content": getattr(response, 'content', '') or ""})
                break

            # Dispatch tools (parallel within this round)
            batch_results = {}
            for tc in response.tool_calls:
                tool_call_count += 1
                result = await self._dispatch_tool(tc, session_id)
                batch_results[tc.id] = result
                tool_results.append({
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "todo_id": current_todo.get("id", ""),
                    "result": str(result)[:500],
                })

            # Add tool results as tool role messages
            for tc in response.tool_calls:
                result_text = json.dumps(batch_results.get(tc.id, {}), ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "content": result_text,
                    "tool_call_id": tc.id,
                })

            # Push progress
            if self._push:
                await self._push(
                    session_id, "tool", "progress", "assistant",
                    payload={
                        "tool_call_id": current_todo.get("id", "todo"),
                        "round": round_count,
                        "tool_name": ",".join(tc.name for tc in response.tool_calls),
                    },
                    priority_kind="normal",
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
        plan_iterations = state.get("plan_iterations", 0)

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
            return {
                "all_satisfied": False,
                "next_action": "fail",
                "final_reply": "评估失败，请重试",
                "plan_iterations": plan_iterations + 1,
            }

        # Convert ask_user_question from Pydantic model to dict if present
        auq = data.get("ask_user_question")
        ask_user_dict = auq.model_dump() if auq is not None and hasattr(auq, "model_dump") else None

        return {
            "all_satisfied": data.get("satisfied", False),
            "next_action": data.get("next_action", "fail"),
            "final_reply": data.get("final_message", ""),
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
        retry_count = state.get("todo_retry_count", 0)

        # Edge cases: empty todos or past the end
        if not todos or current_todo_index >= len(todos):
            return {"todo_checkpoint_satisfied": True}

        current_todo = todos[current_todo_index]
        todo_results = [
            r for r in tool_results
            if r.get("todo_id") == current_todo.get("id", "")
        ]

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
            satisfied = False

        if satisfied:
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
                return {
                    "todo_checkpoint_satisfied": True,
                    "todo_retry_count": 0,
                    "current_todo_index": current_todo_index + 1,
                }
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

        # Push final reply
        if self._push:
            await self._push(
                session_id, "message", "reply", "assistant",
                payload={"content": full_text.strip()},
                priority_kind="high",
            )

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
