"""提示词优化引擎 — 3阶段 Pipeline: Parse → Clarify → Generate.

将用户零散的非结构化需求转化为高质量的结构化研究方案。

Pipeline:
  Stage 1: Parse (需求解析)
    输入: 用户原始自然语言
    输出: 领域识别、实体提取、歧义标注、信息缺口分析

  Stage 2: Clarify (澄清提问)
    输入: 解析结果 + 信息缺口
    输出: 精准的澄清问题（每次≤4个）

  Stage 3: Generate (方案生成)
    输入: 解析结果 + 用户回答
    输出: 结构化 Plan (JSON + Markdown) + 验收标准 + 风险提示

使用方式:
    from paper_search.agent.prompt_optimizer import PromptOptimizer

    opt = PromptOptimizer(llm_client)

    # Stage 1: 分析用户输入
    analysis = await opt.parse("研究自动驾驶的安全性")

    # Stage 2: 生成澄清问题
    questions = await opt.clarify(analysis)

    # 用户回答 questions 后...
    # Stage 3: 生成方案
    plan = await opt.generate(analysis, user_answers)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class ParseResult:
    """Stage 1 输出: 需求解析结果."""
    original_query: str
    domain: str = ""  # 主要研究领域
    sub_domains: list[str] = field(default_factory=list)  # 子领域
    research_type: str = ""  # "survey" | "method_comparison" | "gap_analysis" | "trend_tracking" | ...
    depth: str = ""  # "quick_scan" | "standard" | "deep" | "systematic_review"
    entities: dict[str, list[str]] = field(default_factory=dict)  # 识别到的实体
    # e.g. {"authors": [...], "papers": [...], "methods": [...], "datasets": [...]}
    ambiguities: list[str] = field(default_factory=list)  # 歧义点
    missing_info: list[str] = field(default_factory=list)  # 缺失的关键信息
    suggested_time_range: str = ""  # 建议的时间范围
    suggested_sources: list[str] = field(default_factory=list)  # 建议的来源
    complexity_score: float = 0.5  # 需求复杂度 0-1
    raw_analysis: dict = field(default_factory=dict)


@dataclass
class ClarifyResult:
    """Stage 2 输出: 澄清问题."""
    questions: list[dict] = field(default_factory=list)
    # [{"id": "q1", "question": "...", "context": "...", "type": "single_choice|multi_choice|open",
    #   "options": [...], "priority": "high|medium|low"}]
    requires_clarification: bool = False
    education_hints: list[str] = field(default_factory=list)  # 教育式引导提示
    raw_response: dict = field(default_factory=dict)


@dataclass
class GeneratedPlan:
    """Stage 3 输出: 生成的方案."""
    goal: str
    user_query: str
    refined_query: str  # 优化后的查询描述
    sub_tasks: list[dict] = field(default_factory=list)
    search_strategy: dict = field(default_factory=dict)
    tools_required: list[str] = field(default_factory=list)
    acceptance_criteria: dict = field(default_factory=dict)
    expected_output: str = ""
    risks: list[dict] = field(default_factory=list)
    timeline_estimate: str = ""  # 预估耗时
    cost_estimate: int = 0  # 预估 token 消耗
    plan_json: dict = field(default_factory=dict)
    plan_markdown: str = ""


# ═══════════════════════════════════════════════════════════════
# System Prompts
# ═══════════════════════════════════════════════════════════════

STAGE1_PARSE_PROMPT = """你是一个学术研究需求深度解析器。用户会用自然语言描述他们的研究需求，
你需要做深度的语义分析。

分析维度:
1. 领域识别: 确定主要研究领域和子领域
2. 研究类型: survey(文献综述) / method_comparison(方法对比) / gap_analysis(研究空白分析) / trend_tracking(前沿追踪) / deep_reading(论文精读)
3. 深度判断: quick_scan(快速扫描20篇) / standard(标准调研50篇) / deep(深度调研100篇) / systematic_review(系统综述150+篇)
4. 实体提取: 识别提到的作者、论文标题、方法名、数据集、指标
5. 歧义检测: 存在多义性的术语和表述
6. 缺口分析: 缺少哪些关键信息才能开始搜索
7. 复杂度评估: 0-1, 越复杂的任务值越高

输出纯 JSON:
{
  "domain": "主要研究领域",
  "sub_domains": ["子领域1", "子领域2"],
  "research_type": "survey",
  "depth": "standard",
  "entities": {
    "authors": [],
    "papers": [],
    "methods": [],
    "datasets": [],
    "metrics": []
  },
  "ambiguities": ["歧义1: 术语X可能指Y或Z"],
  "missing_info": ["缺少年份范围", "缺少对论文类型的偏好"],
  "suggested_time_range": "2022-2026",
  "suggested_sources": ["arxiv", "semantic_scholar", "ieee"],
  "complexity_score": 0.4
}"""

STAGE2_CLARIFY_PROMPT = """你是一个精准的学术需求澄清专家。基于需求解析的结果，生成澄清问题。

问题设计原则:
1. 精准而不冗余: 只问对搜索结果有实质性影响的问题
2. 优先级排序: high=必须先澄清 / medium=建议澄清 / low=可以后澄清
3. 教育式引导: 当需求过于宽泛时，提供结构化的思考框架
4. 最多4个问题: 避免用户疲劳，聚焦最关键的缺口

问题类型:
- single_choice: 单选，提供具体选项
- multi_choice: 多选
- open: 开放式

输出纯 JSON:
{
  "questions": [
    {
      "id": "q1",
      "question": "你关注的是功能安全(safety,如ISO 26262)还是AI安全(security,如对抗攻击)？",
      "context": "因为'安全性'在自动驾驶领域有多个含义，不同含义对应不同的论文方向",
      "type": "single_choice",
      "options": ["功能安全 Safety", "AI安全 Security", "两者都关注"],
      "priority": "high"
    }
  ],
  "education_hints": [
    "自动驾驶安全可分为感知安全、决策安全、功能安全、信息安全四大方向",
    "建议先确定一个主要方向做深入调研，再扩展到其他方向"
  ],
  "requires_clarification": true
}

如果需求已经足够清晰不需要澄清:
{
  "questions": [],
  "education_hints": [],
  "requires_clarification": false
}"""

STAGE3_GENERATE_PROMPT = """你是一个学术研究方案生成器。根据充分澄清后的需求，生成详细的可执行方案。

方案设计要求:
1. 每个子任务有明确的搜索关键词（含同义词、变体）
2. 关键词策略: 使用 AND/OR 组合，确保查全率和查准率
3. 来源选择: 根据领域特性选择最合适的来源
4. 验收标准: 量化的、可自动检查的标准
5. 风险识别: 预判可能的困难和应对方案

输出纯 JSON:
{
  "goal": "一句话研究目标",
  "refined_query": "优化后的完整需求描述（比用户原始输入更精确）",
  "sub_tasks": [
    {
      "id": "st1",
      "name": "子任务名称",
      "description": "详细描述这个子任务要做什么",
      "keywords": ["关键词1 AND 关键词2", "synonym1 OR synonym2"],
      "sources": ["arxiv", "semantic_scholar"],
      "year_from": 2022,
      "year_to": 2026,
      "max_results_per_source": 20,
      "acceptance_criteria": {
        "metric": "relevant_papers_count",
        "threshold": 10,
        "description": "至少找到10篇相关性≥0.7的论文"
      },
      "priority": "high"
    }
  ],
  "search_strategy": {
    "approach": "breadth_first",
    "description": "先同时搜索所有子方向，根据结果质量和数量决定是否深入",
    "max_iterations": 3,
    "iteration_rules": "每轮后评估结果→调整关键词→重新搜索不足的方向"
  },
  "tools_required": ["search_papers", "evaluate_papers", "download_paper", "convert_paper", "index_paper"],
  "expected_output": "包含50篇核心论文的结构化文献综述报告，含研究趋势分析和空白发现",
  "risks": [
    {"risk": "关键词可能太窄导致论文太少", "mitigation": "自动放宽年份和关键词约束"},
    {"risk": "某些方向搜索结果可能不相关", "mitigation": "LLM自动评估后筛选"}
  ],
  "timeline_estimate": "预估15-30分钟完成全流程",
  "cost_estimate": 15000
}"""


# ═══════════════════════════════════════════════════════════════
# Prompt Optimizer
# ═══════════════════════════════════════════════════════════════


class PromptOptimizer:
    """3阶段提示词优化 Pipeline.

    将用户零散的自然语言 → 结构化高质量研究方案。
    """

    def __init__(self, llm_client=None):
        from .llm_client_v2 import LLMClientV2
        self._llm = llm_client or LLMClientV2()

    # ── Stage 1: Parse ─────────────────────────────────────

    async def parse(self, user_query: str) -> ParseResult:
        """Stage 1: 深度解析用户需求.

        Args:
            user_query: 用户原始自然语言输入

        Returns:
            ParseResult with domain, ambiguities, gaps, etc.
        """
        today = datetime.now()
        user_msg = (
            f"当前日期: {today.strftime('%Y-%m-%d')}\n"
            f"用户输入: {user_query}\n\n"
            f"请深度解析这个研究需求。"
        )

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": user_msg}],
                system=STAGE1_PARSE_PROMPT,
            )
        except Exception as e:
            logger.error(f"Stage 1 parse failed: {e}")
            return ParseResult(original_query=user_query, raw_analysis={"error": str(e)})

        return ParseResult(
            original_query=user_query,
            domain=result.get("domain", ""),
            sub_domains=result.get("sub_domains", []),
            research_type=result.get("research_type", "survey"),
            depth=result.get("depth", "standard"),
            entities=result.get("entities", {}),
            ambiguities=result.get("ambiguities", []),
            missing_info=result.get("missing_info", []),
            suggested_time_range=result.get("suggested_time_range", ""),
            suggested_sources=result.get("suggested_sources", ["arxiv", "semantic_scholar"]),
            complexity_score=result.get("complexity_score", 0.5),
            raw_analysis=result,
        )

    # ── Stage 2: Clarify ───────────────────────────────────

    async def clarify(self, parse_result: ParseResult) -> ClarifyResult:
        """Stage 2: 生成澄清问题.

        Args:
            parse_result: Stage 1 的解析结果

        Returns:
            ClarifyResult with questions and education hints
        """
        context = {
            "domain": parse_result.domain,
            "sub_domains": parse_result.sub_domains,
            "ambiguities": parse_result.ambiguities,
            "missing_info": parse_result.missing_info,
            "complexity_score": parse_result.complexity_score,
        }

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                system=STAGE2_CLARIFY_PROMPT,
            )
        except Exception as e:
            logger.error(f"Stage 2 clarify failed: {e}")
            return ClarifyResult(requires_clarification=False, raw_response={"error": str(e)})

        return ClarifyResult(
            questions=result.get("questions", []),
            requires_clarification=result.get("requires_clarification", False),
            education_hints=result.get("education_hints", []),
            raw_response=result,
        )

    # ── Stage 3: Generate ──────────────────────────────────

    async def generate(
        self,
        parse_result: ParseResult,
        user_answers: list[dict] = None,
    ) -> GeneratedPlan:
        """Stage 3: 生成结构化方案.

        Args:
            parse_result: Stage 1 的解析结果
            user_answers: 用户对 Stage 2 问题的回答

        Returns:
            GeneratedPlan with full execution plan
        """
        context = {
            "original_query": parse_result.original_query,
            "domain": parse_result.domain,
            "sub_domains": parse_result.sub_domains,
            "research_type": parse_result.research_type,
            "depth": parse_result.depth,
            "suggested_sources": parse_result.suggested_sources,
            "suggested_time_range": parse_result.suggested_time_range,
            "user_answers": user_answers or [],
        }

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                system=STAGE3_GENERATE_PROMPT,
            )
        except Exception as e:
            logger.error(f"Stage 3 generate failed: {e}")
            return self._fallback_plan(parse_result)

        if "error" in result:
            return self._fallback_plan(parse_result)

        # 构建 Markdown
        plan_md = self._render_plan_markdown(result)

        return GeneratedPlan(
            goal=result.get("goal", parse_result.original_query),
            user_query=parse_result.original_query,
            refined_query=result.get("refined_query", parse_result.original_query),
            sub_tasks=result.get("sub_tasks", []),
            search_strategy=result.get("search_strategy", {}),
            tools_required=result.get("tools_required", []),
            acceptance_criteria=result.get("acceptance_criteria", {}),
            expected_output=result.get("expected_output", ""),
            risks=result.get("risks", []),
            timeline_estimate=result.get("timeline_estimate", ""),
            cost_estimate=result.get("cost_estimate", 0),
            plan_json=result,
            plan_markdown=plan_md,
        )

    def _fallback_plan(self, parse_result: ParseResult) -> GeneratedPlan:
        """降级: 简单的默认方案."""
        return GeneratedPlan(
            goal=parse_result.original_query,
            user_query=parse_result.original_query,
            refined_query=parse_result.original_query,
            sub_tasks=[{
                "name": "搜索论文",
                "description": f"搜索: {parse_result.original_query}",
                "keywords": [parse_result.original_query],
                "sources": parse_result.suggested_sources or ["arxiv", "semantic_scholar"],
                "acceptance_criteria": {"metric": "papers_found", "threshold": 10},
            }],
            search_strategy={"approach": "single_pass"},
            expected_output="论文列表和相关性评估",
            plan_json={"error": "llm_generation_failed"},
        )

    # ── Full Pipeline ──────────────────────────────────────

    async def optimize(
        self,
        user_query: str,
        user_answers: list[dict] = None,
        skip_clarify: bool = False,
    ) -> dict:
        """运行完整的 3 阶段 Pipeline.

        Args:
            user_query: 用户原始输入
            user_answers: 预设的答案 (跳过 Stage 2)
            skip_clarify: 是否跳过澄清阶段

        Returns:
            {"parse": ParseResult, "clarify": ClarifyResult, "plan": GeneratedPlan}
        """
        # Stage 1
        parsed = await self.parse(user_query)

        # Stage 2
        if skip_clarify or user_answers:
            clarify_result = ClarifyResult(requires_clarification=False)
        else:
            clarify_result = await self.clarify(parsed)

        # If clarification needed and no answers provided, return early
        if clarify_result.requires_clarification and not user_answers:
            return {
                "stage": "clarify_needed",
                "parse": parsed,
                "clarify": clarify_result,
                "plan": None,
            }

        # Stage 3
        plan = await self.generate(parsed, user_answers)

        return {
            "stage": "complete",
            "parse": parsed,
            "clarify": clarify_result,
            "plan": plan,
        }

    # ── Markdown Rendering ─────────────────────────────────

    def _render_plan_markdown(self, plan_json: dict) -> str:
        """将 JSON plan 渲染为可读的 Markdown."""
        lines = [
            f"# 研究方案: {plan_json.get('goal', '')}",
            "",
            f"**优化后需求**: {plan_json.get('refined_query', '')}",
            "",
            "---",
            "",
            "## 子任务",
        ]

        for i, st in enumerate(plan_json.get("sub_tasks", [])):
            lines.append(f"### {i+1}. {st.get('name', f'子任务{i+1}')}")
            lines.append(f"- **描述**: {st.get('description', '')}")
            lines.append(f"- **关键词**: `{', '.join(st.get('keywords', []))}`")
            lines.append(f"- **来源**: {', '.join(st.get('sources', []))}")
            if st.get("year_from") or st.get("year_to"):
                lines.append(f"- **时间**: {st.get('year_from', '?')}–{st.get('year_to', '?')}")
            criteria = st.get("acceptance_criteria", {})
            if criteria:
                lines.append(f"- **验收标准**: {criteria.get('description', '')} ({criteria.get('metric', '')} ≥ {criteria.get('threshold', '')})")
            if st.get("priority"):
                lines.append(f"- **优先级**: {st['priority']}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## 搜索策略")
        strategy = plan_json.get("search_strategy", {})
        lines.append(f"- **方式**: {strategy.get('approach', 'N/A')}")
        lines.append(f"- **说明**: {strategy.get('description', '')}")
        lines.append(f"- **最大迭代**: {strategy.get('max_iterations', 'N/A')} 轮")
        lines.append("")

        lines.append("## 预期产出")
        lines.append(f"{plan_json.get('expected_output', 'N/A')}")
        lines.append("")

        lines.append("## 所需工具")
        for tool in plan_json.get("tools_required", []):
            lines.append(f"- `{tool}`")
        lines.append("")

        risks = plan_json.get("risks", [])
        if risks:
            lines.append("## 风险与应对")
            for r in risks:
                lines.append(f"- ⚠️ **{r.get('risk', '')}** → {r.get('mitigation', '')}")
            lines.append("")

        lines.append("## 预估")
        lines.append(f"- 时间: {plan_json.get('timeline_estimate', 'N/A')}")
        lines.append(f"- Token 消耗: ~{plan_json.get('cost_estimate', 0):,}")
        lines.append("")

        return "\n".join(lines)
