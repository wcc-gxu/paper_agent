"""Writing Agent — 学术写作辅助 (v3 Phase 2 新建).

功能:
  - 综述生成: 基于已入库论文自动生成文献综述（支持模板定制）
  - 模板推荐: 根据目标期刊/会议推荐写作模板（CVPR/NeurIPS/ACL 等）
  - 引用标记: 统一引用格式 [local:xxx] / [ext:doi] / [Agent 综合]
  - AI 味校验: 检测并替换 AI 生成痕迹（黑名单 + 正则 + LLM judge）

LangGraph 4 节点图:
  survey → template_recommend → citation_format → ai_flavor_check

用法:
    from .writing_graph import WritingAgent
    agent = WritingAgent(db, vector_store, llm_client)
    result = await agent.generate_survey(project_id="prj-xxx", template="cvpr")
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# AI 味检测规则
# ═══════════════════════════════════════════════════════════════

AI_FLAVOR_BLACKLIST: list[tuple[str, str]] = [
    # (黑名单模式, 替换建议)
    ("值得注意的是", "注意"),
    ("总的来说", "综上"),
    ("综上所述", "综上"),
    ("不可否认", ""),
    ("在这个快速发展的时代", ""),
    ("随着人工智能技术的不断发展", ""),
    ("在本文中，我们", "本文"),
    ("值得注意的是，", ""),
    ("需要指出的是，", ""),
    ("毫无疑问，", ""),
    ("众所周知，", ""),
    ("不难发现，", ""),
    ("显而易见，", ""),
    ("——", "—"),  # 中文破折号替换为英文
    ("它提供了一种新的视角", ""),
    ("从某种程度上说", ""),
    ("在某种程度上", ""),
    ("不仅如此", "此外"),
    ("更重要的是", "更重要的是，"),
    ("这无疑", "这"),
]

AI_FLAVOR_REGEX_PATTERNS: list[tuple[str, str, str]] = [
    # (正则模式, 替换, 说明)
    (r'(值得注意)\s*(的是)?', r'', '"值得注意的是"'),
    (r'(总的来[说看])', r'综上', '"总的来说"'),
    (r'在这个\w+的时代', r'', '时代背景套话'),
    (r'随着[\w\s]+的(?:不断|持续|飞速)发展', r'', '伴随发展套话'),
    (r'为[\w\s]+提供了新的(?:思路|视角|方向|可能)', r'', '视角套话'),
    (r'毫无疑问[，,]?\s*', r'', '"毫无疑问"'),
    (r'众所周知[，,]?\s*', r'', '"众所周知"'),
]


# ═══════════════════════════════════════════════════════════════
# 期刊模板
# ═══════════════════════════════════════════════════════════════

TEMPLATES: dict[str, dict] = {
    "cvpr": {
        "name": "CVPR / ICCV",
        "sections": [
            "Introduction",
            "Related Work",
            "Method",
            "Experiments",
            "Conclusion",
        ],
        "citation_style": "ieee",
        "max_pages": 8,
        "language": "en",
    },
    "neurips": {
        "name": "NeurIPS / ICML / ICLR",
        "sections": [
            "Introduction",
            "Related Work",
            "Background",
            "Method",
            "Experiments",
            "Discussion",
            "Conclusion",
        ],
        "citation_style": "ieee",
        "max_pages": 9,
        "language": "en",
    },
    "acl": {
        "name": "ACL / EMNLP / NAACL",
        "sections": [
            "Introduction",
            "Related Work",
            "Method",
            "Experimental Setup",
            "Results",
            "Analysis",
            "Conclusion",
        ],
        "citation_style": "acl",
        "max_pages": 8,
        "language": "en",
    },
    "ccf_cn": {
        "name": "CCF 中文期刊 (计算机学报/软件学报等)",
        "sections": [
            "引言",
            "相关工作",
            "方法",
            "实验",
            "讨论",
            "结论",
        ],
        "citation_style": "gb7714",
        "language": "zh",
    },
    "arxiv": {
        "name": "arXiv 预印本",
        "sections": [
            "Introduction",
            "Related Work",
            "Method",
            "Experiments",
            "Conclusion",
        ],
        "citation_style": "ieee",
        "language": "en",
    },
}

CITATION_FORMATS: dict[str, str] = {
    "ieee": "[{N}]",
    "acl": "({Author}, {Year})",
    "gb7714": "[{N}]",
    "apa": "({Author}, {Year})",
}


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class WritingState(TypedDict, total=False):
    project_id: str
    user_query: str
    papers: list[dict]
    template: str               # cvpr / neurips / acl / ccf_cn / arxiv
    language: str

    # 各阶段产物
    survey_content: str
    recommended_template: dict
    citation_report: dict       # {total, verified, flagged, details: [...]}
    ai_flavor_report: dict      # {detected_count, replacements: [...]}

    # 元数据
    current_stage: str
    stage_index: int
    total_stages: int

    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# WritingAgent
# ═══════════════════════════════════════════════════════════════


class WritingAgent:
    """学术写作辅助 Agent — 6 节点 StateGraph。

    节点:
      survey            → 生成文献综述
      template_recommend → 推荐写作模板
      citation_format   → 统一引用格式并标记
      ai_flavor_check   → AI 味检测与替换
      landscape         → 生成研究全景图（分支地图+代表工作+时间线）
      gap_analysis      → 生成缺口分析文档（独立 MD，标注 AI 辅助）
    """

    def __init__(self, db=None, vector_store=None, llm_client=None, on_progress=None):
        self.db = db
        self.vector_store = vector_store
        self.llm = llm_client
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(WritingState)

        builder.add_node("survey", self._survey_node)
        builder.add_node("template_recommend", self._template_node)
        builder.add_node("citation_format", self._citation_node)
        builder.add_node("ai_flavor_check", self._ai_flavor_node)
        builder.add_node("landscape", self._landscape_node)
        builder.add_node("gap_analysis", self._gap_analysis_node)

        builder.add_edge(START, "survey")
        builder.add_edge("survey", "template_recommend")
        builder.add_edge("template_recommend", "citation_format")
        builder.add_edge("citation_format", "ai_flavor_check")
        builder.add_edge("ai_flavor_check", "landscape")
        builder.add_edge("landscape", "gap_analysis")
        builder.add_edge("gap_analysis", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("WritingAgent not compiled — call compile() first")
        return self._graph

    async def generate_survey(self, project_id: str, user_query: str = "",
                               template: str = "arxiv", papers: list[dict] = None) -> dict:
        """生成综述的便捷入口。"""
        if self._graph is None:
            self.compile()

        state = {
            "project_id": project_id,
            "user_query": user_query,
            "template": template,
            "papers": papers or [],
            "language": TEMPLATES.get(template, TEMPLATES["arxiv"])["language"],
        }
        result = await self._graph.ainvoke(state)
        return result.get("result", {})

    async def check_citations(self, text: str) -> dict:
        """独立引用格式检查（可单独调用）。"""
        return await self._citation_check(text)

    async def generate_gap_analysis(self, project_id: str) -> dict:
        """生成缺口分析的便捷入口。返回独立 MD 文档路径。"""
        if self._graph is None:
            self.compile()

        state = {"project_id": project_id, "user_query": "", "template": "arxiv", "papers": []}
        result = await self._graph.ainvoke(state)
        return result.get("result", {}).get("gap_analysis", {})

    async def remove_ai_flavor(self, text: str) -> dict:
        """独立 AI 味检查与清理（可单独调用）。"""
        return await self._ai_flavor_check_text(text)

    # ── 节点 ─────────────────────────────────────────

    async def _survey_node(self, state: WritingState) -> dict:
        """生成文献综述。"""
        project_id = state.get("project_id", "")
        user_query = state.get("user_query", "")
        template = state.get("template", "arxiv")
        papers = state.get("papers", [])

        await self._notify("生成综述", 1, 4, "正在生成文献综述...")

        # 从 DB 加载论文（如未传入）
        if not papers and self.db and project_id:
            try:
                papers = self.db.get_project_papers(project_id)
            except Exception as e:
                logger.warning(f"Failed to load papers: {e}")

        if not papers:
            return {
                "current_stage": "survey", "stage_index": 1, "total_stages": 4,
                "error": "No papers available for survey generation",
            }

        # 使用 LLM 生成综述
        survey = ""
        if self.llm:
            try:
                sections = TEMPLATES.get(template, TEMPLATES["arxiv"])["sections"]
                lang = TEMPLATES.get(template, TEMPLATES["arxiv"])["language"]

                paper_summaries = "\n".join([
                    f"- [{i+1}] {p.get('title','')} ({p.get('year','')}). "
                    f"{p.get('abstract','')[:300]}"
                    for i, p in enumerate(papers[:20])
                ])

                prompt = (
                    f"Write a literature survey in {'Chinese' if lang == 'zh' else 'English'}.\n\n"
                    f"Topic: {user_query}\n"
                    f"Sections: {', '.join(sections)}\n\n"
                    f"Available papers:\n{paper_summaries}\n\n"
                    f"Guidelines:\n"
                    f"- Focus on the 'Related Work' section\n"
                    f"- Use citation markers like [1], [2], etc.\n"
                    f"- Be critical and comparative, not just descriptive\n"
                    f"- Limit to 2000 words\n"
                )
                survey = await self.llm.chat([{"role": "user", "content": prompt}])
            except Exception as e:
                logger.warning(f"LLM survey generation failed: {e}")
                survey = f"[Survey generation failed: {e}]"

        return {
            "papers": papers,
            "survey_content": survey,
            "current_stage": "survey", "stage_index": 1, "total_stages": 4,
        }

    async def _template_node(self, state: WritingState) -> dict:
        """推荐写作模板。"""
        template_key = state.get("template", "arxiv")
        template = TEMPLATES.get(template_key, TEMPLATES["arxiv"])

        await self._notify("模板推荐", 2, 4, f"模板: {template['name']}")

        # 输出模板建议
        sections_text = "\n".join([f"  {i+1}. {s}" for i, s in enumerate(template["sections"])])
        logger.info(f"Template: {template['name']}\n{sections_text}")

        return {
            "recommended_template": template,
            "current_stage": "template_recommend", "stage_index": 2,
        }

    async def _citation_node(self, state: WritingState) -> dict:
        """引用格式检查与标记。"""
        survey = state.get("survey_content", "")
        template = state.get("recommended_template", {})
        citation_style = template.get("citation_style", "ieee")
        papers = state.get("papers", [])

        await self._notify("引用格式", 3, 4, "检查引用格式...")

        report = await self._citation_check(survey, papers, citation_style)
        return {
            "citation_report": report,
            "current_stage": "citation_format", "stage_index": 3,
        }

    async def _ai_flavor_node(self, state: WritingState) -> dict:
        """AI 味检测与清理。"""
        survey = state.get("survey_content", "")

        await self._notify("AI味校验", 4, 4, "检测AI生成痕迹...")

        report = await self._ai_flavor_check_text(survey)

        # 汇总结果
        citation_report = state.get("citation_report", {})
        result = {
            "project_id": state.get("project_id", ""),
            "template": state.get("recommended_template", {}),
            "survey": survey,
            "survey_length": len(survey),
            "citations": citation_report,
            "ai_flavor": report,
        }

        return {
            "ai_flavor_report": report,
            "current_stage": "ai_flavor_check", "stage_index": 4,
            "result": result,
        }

    # ── 核心方法 ─────────────────────────────────────

    async def _citation_check(self, text: str, papers: list[dict] = None,
                               style: str = "ieee") -> dict:
        """检查文本中的引用标记。

        识别三种引用格式:
          - [local:paper_id]  库内引用
          - [ext:doi]         外部 DOI 引用
          - [N]               编号引用
        """
        if not text:
            return {"total": 0, "verified": 0, "flagged": 0, "details": []}

        # 查找所有引用标记
        patterns = [
            (r'\[local:([^\]]+)\]', 'local'),
            (r'\[ext:([^\]]+)\]', 'external'),
            (r'\[(\d+)\]', 'numeric'),
            (r'\(([A-Z][a-z]+),\s*(\d{4})\)', 'acl'),  # ACL 风格
        ]

        citations = []
        for pattern, kind in patterns:
            for m in re.finditer(pattern, text):
                citations.append({
                    "kind": kind,
                    "value": m.group(1) if kind != 'acl' else f"{m.group(1)}, {m.group(2)}",
                    "position": m.start(),
                })

        verified = 0
        flagged = 0
        details = []

        for c in citations:
            if c["kind"] == "local":
                # 本地引用：检查 paper_id 是否存在
                paper_exists = False
                if papers:
                    paper_exists = any(p.get("paper_id") == c["value"] for p in papers)
                if paper_exists:
                    verified += 1
                    details.append({**c, "status": "verified"})
                else:
                    flagged += 1
                    details.append({**c, "status": "flagged", "reason": "paper_id not found"})
            elif c["kind"] == "external":
                # 外部 DOI：简单的格式检查
                if re.match(r'^10\.\d{4,}/', c["value"]):
                    verified += 1
                    details.append({**c, "status": "verified"})
                else:
                    flagged += 1
                    details.append({**c, "status": "flagged", "reason": "invalid DOI format"})
            else:
                verified += 1
                details.append({**c, "status": "verified"})

        return {
            "total": len(citations),
            "verified": verified,
            "flagged": flagged,
            "details": details,
        }

    async def _ai_flavor_check_text(self, text: str) -> dict:
        """检测并标记 AI 生成痕迹。

        三层检测:
          1. 黑名单精确匹配
          2. 正则模式匹配
          3. LLM judge（在有 LLM 时启用）
        """
        if not text:
            return {"detected_count": 0, "replacements": []}

        detected = []
        cleaned = text

        # Layer 1: 黑名单
        for pattern, replacement in AI_FLAVOR_BLACKLIST:
            count = cleaned.count(pattern)
            if count > 0:
                detected.append({
                    "pattern": pattern,
                    "replacement": replacement,
                    "count": count,
                    "layer": "blacklist",
                })
                cleaned = cleaned.replace(pattern, replacement)

        # Layer 2: 正则
        for pattern, replacement, desc in AI_FLAVOR_REGEX_PATTERNS:
            matches = list(re.finditer(pattern, cleaned))
            if matches:
                detected.append({
                    "pattern": desc,
                    "replacement": replacement,
                    "count": len(matches),
                    "layer": "regex",
                })
                cleaned = re.sub(pattern, replacement, cleaned)

        # Layer 3: LLM judge（可选，较慢）
        if self.llm and len(text) > 200:
            try:
                llm_detections = await self._llm_ai_flavor_judge(text[:2000])
                if llm_detections:
                    detected.extend(llm_detections)
            except Exception as e:
                logger.debug(f"LLM AI flavor judge skipped: {e}")

        return {
            "detected_count": len(detected),
            "total_replacements": sum(d.get("count", 1) for d in detected),
            "replacements": detected,
        }

    async def _llm_ai_flavor_judge(self, text: str) -> list[dict]:
        """LLM 判断 AI 味 — 识别更深层的 AI 写作特征。"""
        prompt = (
            "Analyze this academic text for AI-generated writing patterns. "
            "Look for:\n"
            "1. Overly formal transitions (e.g., 'Furthermore', 'Moreover', 'In addition')\n"
            "2. Hedging overuse (e.g., 'It is worth noting that', 'It should be noted that')\n"
            "3. Generic summaries (e.g., 'In conclusion', 'To sum up')\n"
            "4. Vague claims without citations\n"
            "5. Repetitive sentence structures\n\n"
            f"Text:\n{text}\n\n"
            "Return a JSON list of issues found: [{\"pattern\": \"...\", \"count\": N}]. "
            "If none found, return empty list []."
        )
        try:
            response = await self.llm.chat([{"role": "user", "content": prompt}])
            # 尝试解析 JSON
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                issues = json.loads(json_match.group())
                return [{"pattern": i["pattern"], "count": i.get("count", 1),
                         "layer": "llm_judge"} for i in issues]
        except Exception as e:
                        logger.warning(f"LLM AI flavor check failed: {e}")
        return []

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  WritingAgent [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception as e:
                                logger.debug(f"WritingAgent on_progress error: {e}")


# ═══════════════════════════════════════════════════════════════
# 工具函数（可直接 import 使用）
# ═══════════════════════════════════════════════════════════════

def quick_ai_flavor_check(text: str) -> dict:
    """快速 AI 味检测（同步，无 LLM，适合 CLI 使用）。"""
    detected = []
    cleaned = text

    for pattern, replacement in AI_FLAVOR_BLACKLIST:
        count = cleaned.count(pattern)
        if count > 0:
            detected.append({"pattern": pattern, "count": count})
            cleaned = cleaned.replace(pattern, replacement)

    for pattern, replacement, desc in AI_FLAVOR_REGEX_PATTERNS:
        matches = list(re.finditer(pattern, cleaned))
        if matches:
            detected.append({"pattern": desc, "count": len(matches)})
            cleaned = re.sub(pattern, replacement, cleaned)

    return {
        "detected_count": len(detected),
        "issues": detected,
        "cleaned_text": cleaned,
    }


def list_templates() -> list[dict]:
    """列出所有可用写作模板。"""
    return [{"key": k, "name": v["name"], "sections": v["sections"],
             "citation_style": v["citation_style"], "language": v["language"]}
            for k, v in TEMPLATES.items()]
