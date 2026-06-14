"""Agentic Loop 引擎 — Plan-then-Execute 决策模型 + 状态机.

核心设计:
  Plan Phase: 需求解析 → 澄清 → 方案生成 → 用户确认 → Todolist
  Execute Phase: 逐步执行 → 指标检查 → LLM 质量评估 → 验收 → 下一/重试/求助

状态机:
  START → EXECUTE → VERIFY → PASS? ─Yes→ NEXT_STEP → ... → COMPLETE
                    │                        │
                    └No→ RETRY(≤2) ──仍No→ HELP_USER

收敛保证:
  - 步数上限: 每 task 硬性限制 max_steps
  - 重试上限: 每 step 最多自动重试 2 次
  - 全局收敛: 所有 steps done/failed → 汇总报告

使用方式:
    from paper_search.agent.agent_loop import AgentLoop

    loop = AgentLoop(db, llm_client, memory, tool_registry)

    # Plan-then-Execute
    task = await loop.plan(user_query="研究自动驾驶安全性的最新进展")
    # → 生成 plan.json + plan.md → 等待用户确认

    await loop.execute(task_id)
    # → 逐步执行 + 验收

    # 用户控制
    await loop.pause(task_id)
    await loop.resume(task_id)
    await loop.cancel(task_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from .llm_client_v2 import (
    ChatMessage,
    LLMClientV2,
    ToolCall,
    ToolDef,
)
from .memory import MemoryManager, ShortTermMemory
from .tool_registry import RegisteredTool, ToolRegistry, registry as default_registry

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# State & Types
# ═══════════════════════════════════════════════════════════════


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_USER = "needs_user"


@dataclass
class TaskStep:
    """单个任务步骤."""
    index: int
    name: str
    description: str
    action: str  # "search" | "download" | "convert" | "index" | "evaluate" | "analyze" | ...
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    acceptance_criteria: dict = field(default_factory=dict)  # {"metric": "relevance_score", "min": 0.7, "min_count": 10}
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    metrics: dict = field(default_factory=dict)  # 指标结果
    llm_assessment: str = ""  # LLM 质量评估
    retry_count: int = 0
    max_retries: int = 2
    started_at: str = ""
    completed_at: str = ""


@dataclass
class TaskPlan:
    """任务方案."""
    goal: str
    user_query: str
    sub_tasks: list[dict] = field(default_factory=list)  # [{"name": "...", "description": "...", ...}]
    search_strategy: dict = field(default_factory=dict)
    expected_output: str = ""
    risks: list[str] = field(default_factory=list)
    steps: list[TaskStep] = field(default_factory=list)
    max_steps: int = 50


# ═══════════════════════════════════════════════════════════════
# Plan System Prompt
# ═══════════════════════════════════════════════════════════════

PLAN_SYSTEM_PROMPT = """你是一个学术研究方案生成器。根据用户澄清后的研究需求，生成详细的执行方案。

你需要输出一个完整的 JSON 方案:

{
  "goal": "一句话总结研究目标",
  "sub_tasks": [
    {
      "name": "子任务名称",
      "description": "详细描述",
      "search_keywords": ["关键词1", "关键词2"],
      "sources": ["arxiv", "semantic_scholar"],
      "year_from": 2022,
      "year_to": 2026,
      "acceptance_criteria": {
        "metric": "relevant_papers_count",
        "min": 10,
        "description": "至少找到10篇高相关论文"
      }
    }
  ],
  "search_strategy": {
    "approach": "breadth_first",
    "max_rounds": 3,
    "note": "广度优先: 先覆盖所有子方向，再深入重点方向"
  },
  "expected_output": "预期的最终产出描述",
  "risks": ["可能的风险1", "可能的风险2"]
}

原则:
- 每个子任务应聚焦一个明确的搜索方向
- 关键词应具体且覆盖主要的同义表达
- 验收标准应量化（论文数量、相关性阈值等）
- 考虑来源的互补性（arXiv覆盖CS/AI, IEEE覆盖工程, PubMed覆盖医学等）

输出纯 JSON（不要 markdown 代码块）。"""


# ═══════════════════════════════════════════════════════════════
# Step Verification Prompt
# ═══════════════════════════════════════════════════════════════

VERIFY_SYSTEM_PROMPT = """你是一个学术搜索质量评估器。给定一个任务步骤的执行结果，判断是否达标。

评估维度:
1. 数量: 结果数量是否满足预期？
2. 质量: 结果的相关性和准确度如何？
3. 覆盖: 是否覆盖了该子方向的主要方面？
4. 完整性: 是否有明显的遗漏？

输出纯 JSON:
{
  "pass": true,
  "score": 0.85,
  "reason": "该步骤找到12篇论文，其中10篇高相关(≥0.7)，覆盖了该方向的3个主要子领域",
  "issues": [],
  "suggestions": []
}

如果不达标:
{
  "pass": false,
  "score": 0.3,
  "reason": "只找到3篇论文，其中仅1篇相关，搜索词可能需要调整",
  "issues": ["搜索结果太少", "关键词可能太窄"],
  "suggestions": ["尝试更宽泛的关键词", "扩展到IEEE/Springer等其他来源", "去掉年份限制"]
}"""


# ═══════════════════════════════════════════════════════════════
# Agent Loop
# ═══════════════════════════════════════════════════════════════


class AgentLoop:
    """Agentic Loop 引擎 — Plan-then-Execute 核心.

    协调 LLM、工具注册中心、记忆系统，驱动完整的 Agent 工作流。
    """

    def __init__(
        self,
        db,
        llm_client: LLMClientV2 = None,
        memory: MemoryManager = None,
        tool_registry: ToolRegistry = None,
        max_steps_default: int = 50,
        max_auto_retries: int = 2,
    ):
        self._db = db
        self._llm = llm_client or LLMClientV2()
        self._memory = memory or MemoryManager(db)
        self._registry = tool_registry or default_registry
        self.max_steps_default = max_steps_default
        self.max_auto_retries = max_auto_retries

        # 运行中的任务
        self._running_tasks: dict[str, asyncio.Task] = {}

    # ── Plan Phase ─────────────────────────────────────────

    async def clarify(self, user_query: str) -> list[dict]:
        """阶段1: 分析需求并生成澄清问题.

        Args:
            user_query: 用户原始自然语言需求

        Returns:
            澄清问题列表 [{"question": "...", "context": "..."}]
        """
        result = await self._llm.chat_json(
            messages=[ChatMessage(role="user", content=user_query)],
            system="""你是一个学术研究需求分析师。分析用户的搜索需求，识别模糊点和缺失信息，
生成精准的澄清问题。

输出纯 JSON:
{
  "domain": "识别到的研究领域",
  "ambiguities": ["模糊点1", "模糊点2"],
  "missing_info": ["缺失信息1", "缺失信息2"],
  "clarification_questions": [
    {"question": "具体问题？", "context": "为什么需要澄清这个", "type": "single_choice|multi_choice|open"}
  ]
}

原则:
- 问题应具体而非泛泛
- 优先澄清对搜索结果影响最大的模糊点
- 每次最多3-4个问题，避免用户疲劳
- 如果是教育式引导，提供模板和示例""",
        )

        return result.get("clarification_questions", [])

    async def plan(self, user_query: str, clarified_context: dict = None) -> TaskPlan:
        """阶段2: 生成执行方案.

        Args:
            user_query: 原始或澄清后的用户需求
            clarified_context: 澄清对话的上下文

        Returns:
            TaskPlan with steps
        """
        # 构建 plan 生成消息
        context_str = json.dumps(clarified_context, ensure_ascii=False) if clarified_context else ""
        user_msg = (
            f"用户研究需求: {user_query}\n"
            + (f"澄清后的上下文: {context_str}\n" if context_str else "")
            + "\n请生成详细的执行方案。"
        )

        result = await self._llm.chat_json(
            messages=[ChatMessage(role="user", content=user_msg)],
            system=PLAN_SYSTEM_PROMPT,
        )

        if "error" in result:
            # 降级: 使用简单的默认方案
            return self._fallback_plan(user_query)

        # 构建 TaskPlan
        steps = []
        for i, st in enumerate(result.get("sub_tasks", [])):
            step = TaskStep(
                index=i,
                name=st.get("name", f"step_{i}"),
                description=st.get("description", ""),
                action="search",
                tool_name="search_papers",
                tool_args={
                    "keywords": " AND ".join(st.get("search_keywords", [user_query])),
                    "sources": ",".join(st.get("sources", ["arxiv", "semantic_scholar"])),
                    "year_from": st.get("year_from"),
                    "year_to": st.get("year_to"),
                    "max_results": 20,
                },
                acceptance_criteria=st.get("acceptance_criteria", {}),
            )
            steps.append(step)

        return TaskPlan(
            goal=result.get("goal", user_query),
            user_query=user_query,
            sub_tasks=result.get("sub_tasks", []),
            search_strategy=result.get("search_strategy", {}),
            expected_output=result.get("expected_output", ""),
            risks=result.get("risks", []),
            steps=steps,
            max_steps=self.max_steps_default,
        )

    def _fallback_plan(self, user_query: str) -> TaskPlan:
        """降级方案: 单步搜索."""
        return TaskPlan(
            goal=user_query,
            user_query=user_query,
            steps=[TaskStep(
                index=0,
                name="搜索论文",
                description=f"搜索: {user_query}",
                action="search",
                tool_name="search_papers",
                tool_args={"keywords": user_query, "sources": "arxiv,semantic_scholar", "max_results": 20},
            )],
        )

    def plan_to_json(self, plan: TaskPlan) -> dict:
        """将 TaskPlan 序列化为 JSON."""
        return {
            "goal": plan.goal,
            "user_query": plan.user_query,
            "sub_tasks": plan.sub_tasks,
            "search_strategy": plan.search_strategy,
            "expected_output": plan.expected_output,
            "risks": plan.risks,
            "max_steps": plan.max_steps,
            "steps": [
                {
                    "index": s.index,
                    "name": s.name,
                    "description": s.description,
                    "action": s.action,
                    "tool_name": s.tool_name,
                    "tool_args": s.tool_args,
                    "acceptance_criteria": s.acceptance_criteria,
                }
                for s in plan.steps
            ],
        }

    def plan_to_markdown(self, plan: TaskPlan) -> str:
        """将 TaskPlan 渲染为 Markdown."""
        lines = [
            f"# 研究方案: {plan.goal}",
            "",
            f"**用户需求**: {plan.user_query}",
            "",
            "## 子任务",
        ]
        for st in plan.sub_tasks:
            lines.append(f"### {st.get('name', '')}")
            lines.append(f"- 描述: {st.get('description', '')}")
            lines.append(f"- 关键词: {', '.join(st.get('search_keywords', []))}")
            lines.append(f"- 来源: {', '.join(st.get('sources', []))}")
            criteria = st.get("acceptance_criteria", {})
            if criteria:
                lines.append(f"- 验收标准: {criteria.get('description', '')}")
            lines.append("")

        lines.append("## 搜索策略")
        strategy = plan.search_strategy
        lines.append(f"- 方式: {strategy.get('approach', 'N/A')}")
        lines.append(f"- 最大轮次: {strategy.get('max_rounds', 'N/A')}")
        lines.append("")

        lines.append("## 执行步骤")
        for s in plan.steps:
            lines.append(f"### Step {s.index + 1}: {s.name}")
            lines.append(f"- 操作: {s.action}")
            lines.append(f"- 工具: {s.tool_name}")
            accept = s.acceptance_criteria
            if accept:
                lines.append(f"- 验收: {accept.get('description', '')} (指标: {accept.get('metric', '')} ≥ {accept.get('min', '')})")
            lines.append("")

        if plan.risks:
            lines.append("## 风险提示")
            for r in plan.risks:
                lines.append(f"- ⚠️ {r}")
            lines.append("")

        lines.append(f"**总步数限制**: {plan.max_steps}")
        return "\n".join(lines)

    # ── Execute Phase ──────────────────────────────────────

    async def execute(self, task_id: str, steps: list[TaskStep] = None,
                      on_progress: Callable = None) -> dict:
        """阶段3: 逐步执行方案.

        Args:
            task_id: Agent 任务 ID
            steps: 要执行的步骤列表 (如果不提供, 从 DB 加载)
            on_progress: 进度回调 async fn(step_index, total, status, detail)

        Returns:
            执行汇总 {"total": N, "done": N, "failed": N, "results": [...]}
        """
        if steps is None:
            # 从 DB 恢复
            task = self._db.get_agent_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            db_steps = self._db.get_task_steps(task_id)
            steps = [self._db_row_to_step(s) for s in db_steps]

        if not steps:
            raise ValueError("No steps to execute")

        self._db.update_agent_task(task_id, status="executing", total_steps=len(steps), current_step=0)
        self._memory.mid_term.record_action(task_id, "execute_start", f"开始执行 {len(steps)} 个步骤")

        total = len(steps)
        results = []

        for step in steps:
            # 检查是否被取消/暂停
            task = self._db.get_agent_task(task_id)
            if task and task.get("status") in ("cancelled", "paused"):
                logger.info(f"Task {task_id} {task['status']}")
                break

            # 检查步数上限
            if step.index >= self.max_steps_default:
                logger.warning(f"Task {task_id}: reached max steps ({self.max_steps_default})")
                step.status = StepStatus.SKIPPED
                step.llm_assessment = "达到步数上限"
                results.append(self._step_to_result(step))
                continue

            # 执行
            step.status = StepStatus.IN_PROGRESS
            step.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._db.update_agent_task(task_id, current_step=step.index + 1)
            self._db.add_task_step(task_id, step.index, step.name, step.action,
                                   step.tool_name, step.tool_args)

            if on_progress:
                await on_progress(step.index, total, "executing", step.name)

            try:
                # 执行工具调用
                tool = self._registry.get(step.tool_name)
                if tool:
                    step.result = await tool.execute(**step.tool_args)
                else:
                    step.result = {"error": f"Tool not found: {step.tool_name}"}

                # 采集指标
                step.metrics = self._collect_metrics(step)

                # 验收
                step.status = StepStatus.VERIFYING
                if on_progress:
                    await on_progress(step.index, total, "verifying", step.name)

                verification = await self._verify(step)
                step.llm_assessment = verification.get("reason", "")

                if verification.get("pass"):
                    step.status = StepStatus.DONE
                    step.completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                elif step.retry_count < step.max_retries:
                    # 自动重试
                    step.retry_count += 1
                    step.status = StepStatus.RETRYING
                    step.llm_assessment += f" | 自动重试 {step.retry_count}/{step.max_retries}"

                    if on_progress:
                        await on_progress(step.index, total, "retrying",
                                          f"重试 {step.retry_count}/{step.max_retries}")

                    # 调整策略后重试
                    adjusted_args = self._adjust_strategy(step, verification)
                    step.tool_args = adjusted_args
                    tool = self._registry.get(step.tool_name)
                    if tool:
                        step.result = await tool.execute(**adjusted_args)
                    step.metrics = self._collect_metrics(step)

                    # 再次验证
                    verification2 = await self._verify(step)
                    step.llm_assessment += f" | 重试评估: {verification2.get('reason', '')}"

                    if verification2.get("pass"):
                        step.status = StepStatus.DONE
                    else:
                        step.status = StepStatus.NEEDS_USER
                        step.llm_assessment += " | 自动重试后仍不达标，需要用户决策"
                else:
                    # 已达重试上限
                    step.status = StepStatus.NEEDS_USER
                    step.llm_assessment += f" | 已达最大重试次数({step.max_retries})，需要用户决策"

                step.completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            except Exception as e:
                logger.error(f"Step {step.index} failed: {e}")
                step.status = StepStatus.FAILED
                step.llm_assessment = f"执行异常: {str(e)}"
                step.completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # 持久化
            self._db.update_task_step(task_id, step.index,
                                      status=step.status.value,
                                      result_summary=json.dumps(self._summarize_result(step.result), ensure_ascii=False),
                                      metrics=json.dumps(step.metrics, ensure_ascii=False),
                                      llm_assessment=step.llm_assessment,
                                      retry_count=step.retry_count,
                                      completed_at=step.completed_at)

            # 检查点
            self._memory.mid_term.save_checkpoint(
                task_id, step.index, step.name,
                state={"step_index": step.index, "status": step.status.value},
                results={"step_result": self._step_to_result(step)},
            )
            self._memory.mid_term.record_action(task_id, f"step_{step.index}_complete",
                                                f"{step.status.value}: {step.name}")

            results.append(self._step_to_result(step))

            if on_progress:
                await on_progress(step.index, total, step.status.value, step.name)

        # 汇总
        done = sum(1 for r in results if r["status"] == "done")
        failed = sum(1 for r in results if r["status"] == "failed")
        needs_user = sum(1 for r in results if r["status"] == "needs_user")

        final_status = "completed" if failed == 0 else "completed_with_issues"
        self._db.update_agent_task(task_id, status=final_status)

        return {
            "task_id": task_id,
            "total": total,
            "done": done,
            "failed": failed,
            "needs_user": needs_user,
            "results": results,
        }

    async def _verify(self, step: TaskStep) -> dict:
        """验证步骤结果."""
        criteria_str = json.dumps(step.acceptance_criteria, ensure_ascii=False)
        result_str = json.dumps(self._summarize_result(step.result), ensure_ascii=False, default=str)
        metrics_str = json.dumps(step.metrics, ensure_ascii=False)

        return await self._llm.chat_json(
            messages=[ChatMessage(role="user", content=(
                f"步骤: {step.name}\n"
                f"描述: {step.description}\n"
                f"验收标准: {criteria_str}\n"
                f"执行结果: {result_str}\n"
                f"定量指标: {metrics_str}\n"
                f"重试次数: {step.retry_count}/{step.max_retries}"
            ))],
            system=VERIFY_SYSTEM_PROMPT,
        )

    def _collect_metrics(self, step: TaskStep) -> dict:
        """从步骤结果中提取定量指标."""
        result = step.result or {}
        metrics = {}

        if step.action == "search":
            if isinstance(result, dict):
                metrics["papers_found"] = result.get("total_found", 0)
                papers = result.get("papers", [])
                metrics["papers_count"] = len(papers)

        elif step.action == "evaluate":
            if isinstance(result, dict):
                metrics["evaluated"] = result.get("evaluated", 0)
                metrics["relevant"] = result.get("relevant", 0)

        elif step.action == "download":
            if isinstance(result, dict):
                metrics["success"] = 1 if result.get("success") else 0

        return metrics

    def _adjust_strategy(self, step: TaskStep, verification: dict) -> dict:
        """根据验证结果调整策略."""
        args = dict(step.tool_args)
        suggestions = verification.get("suggestions", [])

        for s in suggestions:
            s_lower = s.lower()
            if "关键词" in s or "keyword" in s_lower or "搜索词" in s:
                # 放宽关键词
                pass
            if "来源" in s or "source" in s_lower:
                current = args.get("sources", "")
                if "ieee" not in current:
                    args["sources"] = current + ",ieee"
            if "年份" in s or "year" in s_lower:
                args["year_from"] = max(2018, (args.get("year_from", 2022) or 2022) - 2)

        return args

    def _summarize_result(self, result: Any, max_len: int = 500) -> Any:
        """摘要结果用于存储."""
        if isinstance(result, dict):
            summary = {}
            for k, v in result.items():
                if isinstance(v, str) and len(v) > max_len:
                    summary[k] = v[:max_len] + "..."
                elif isinstance(v, list) and len(v) > 5:
                    summary[k] = v[:5]
                    summary[f"{k}_total"] = len(v)
                else:
                    summary[k] = v
            return summary
        if isinstance(result, str) and len(result) > max_len:
            return result[:max_len] + "..."
        return result

    def _step_to_result(self, step: TaskStep) -> dict:
        return {
            "index": step.index,
            "name": step.name,
            "status": step.status.value,
            "metrics": step.metrics,
            "llm_assessment": step.llm_assessment,
            "retry_count": step.retry_count,
        }

    def _db_row_to_step(self, row: dict) -> TaskStep:
        return TaskStep(
            index=row.get("step_index", 0),
            name=row.get("step_name", ""),
            description="",
            action=row.get("action", "search"),
            tool_name=row.get("tool_name", ""),
            tool_args=json.loads(row.get("tool_args", "{}")) if isinstance(row.get("tool_args"), str) else (row.get("tool_args") or {}),
            status=StepStatus(row.get("status", "pending")),
            metrics=json.loads(row.get("metrics", "{}")) if isinstance(row.get("metrics"), str) else (row.get("metrics") or {}),
            llm_assessment=row.get("llm_assessment") or "",
            retry_count=row.get("retry_count", 0),
        )

    # ── User Control ───────────────────────────────────────

    async def pause(self, task_id: str):
        """暂停任务."""
        self._db.update_agent_task(task_id, status="paused")
        self._memory.mid_term.record_action(task_id, "paused", "用户暂停")

    async def resume(self, task_id: str):
        """恢复任务."""
        task = self._db.get_agent_task(task_id)
        if task is None or task.get("status") != "paused":
            raise ValueError(f"Task {task_id} is not paused")

        steps_data = self._db.get_task_steps(task_id)
        steps = [self._db_row_to_step(s) for s in steps_data]

        # 找到未完成的步骤
        pending = [s for s in steps if s.status in (StepStatus.PENDING, StepStatus.NEEDS_USER)]

        self._db.update_agent_task(task_id, status="executing")

        if pending:
            return await self.execute(task_id, steps=pending)
        else:
            self._db.update_agent_task(task_id, status="completed")
            return {"message": "All steps already completed"}

    async def cancel(self, task_id: str):
        """取消任务."""
        self._db.update_agent_task(task_id, status="cancelled")
        self._memory.mid_term.record_action(task_id, "cancelled", "用户取消")

        # 取消正在运行的 asyncio 任务
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]

    # ── Full Pipeline ──────────────────────────────────────

    async def run_full_pipeline(
        self,
        user_query: str,
        session_id: str = None,
        clarified_answers: list[dict] = None,
        on_progress: Callable = None,
    ) -> dict:
        """完整的 Plan-then-Execute 流水线.

        Args:
            user_query: 用户原始需求
            session_id: 会话 ID (不提供则自动生成)
            clarified_answers: 用户对澄清问题的回答 (跳过澄清阶段)
            on_progress: 进度回调

        Returns:
            {"task_id": "...", "plan": {...}, "results": [...]}
        """
        sid = session_id or str(uuid.uuid4())[:8]
        tid = str(uuid.uuid4())[:8]

        # 创建会话和任务
        self._memory.create_session(sid)
        self._db.create_agent_task(tid, user_query, session_id=sid)

        # Stage 1-2: Clarify + Plan
        self._db.update_agent_task(tid, status="planning")

        if clarified_answers is None:
            questions = await self.clarify(user_query)
            if questions:
                # 返回澄清问题，等待用户回答
                return {
                    "task_id": tid,
                    "session_id": sid,
                    "stage": "clarify",
                    "questions": questions,
                    "message": "请回答以下问题以帮助我更好地理解您的需求",
                }

        # 生成方案
        plan = await self.plan(user_query, clarified_answers)
        plan_json = self.plan_to_json(plan)
        plan_md = self.plan_to_markdown(plan)

        self._db.update_agent_task(tid,
                                   plan_json=json.dumps(plan_json, ensure_ascii=False),
                                   plan_markdown=plan_md,
                                   total_steps=len(plan.steps),
                                   status="awaiting_confirmation")

        # 记录到中期记忆
        self._memory.short_term.add_message("user", user_query)
        self._memory.short_term.add_message("assistant", f"方案已生成: {plan.goal}")

        return {
            "task_id": tid,
            "session_id": sid,
            "stage": "plan_ready",
            "plan_json": plan_json,
            "plan_markdown": plan_md,
            "message": "方案已生成，请确认后执行",
        }

    async def execute_plan(self, task_id: str, on_progress: Callable = None) -> dict:
        """执行已确认的方案."""
        task = self._db.get_agent_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        plan_json = json.loads(task.get("plan_json", "{}")) if task.get("plan_json") else {}
        steps_data = plan_json.get("steps", [])

        steps = []
        for i, sd in enumerate(steps_data):
            steps.append(TaskStep(
                index=i,
                name=sd.get("name", f"step_{i}"),
                description=sd.get("description", ""),
                action=sd.get("action", "search"),
                tool_name=sd.get("tool_name", "search_papers"),
                tool_args=sd.get("tool_args", {}),
                acceptance_criteria=sd.get("acceptance_criteria", {}),
            ))

        return await self.execute(task_id, steps=steps, on_progress=on_progress)
