"""Literature Agent — 文献检索与下载子 Agent (v3 Phase 2).

从 IngestAgent 拆分出的前半段：负责搜索、评估、下载、PDF 转换和元数据提取。
Knowledge Agent 负责后续的切片、embedding、入库。

LangGraph 5 节点线性 Execute Graph:
  search → evaluate → download → convert → extract_metadata

与 IngestAgent 的关系:
  - IngestAgent 7 节点 = Literature Agent 5 节点 + Knowledge Agent 2 节点(index + rank)
  - v3 中 IngestAgent 保留作为兼容层：内部委托给 Literature + Knowledge

用法:
    from .literature_graph import LiteratureAgent
    agent = LiteratureAgent(runner)
    graph = agent.compile()
    result = await graph.ainvoke({"project_id": "...", "user_query": "..."})
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add_messages(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class LiteratureState(TypedDict, total=False):
    project_id: str
    user_query: str
    sources: list[str]
    year_from: int
    max_results: int
    current_stage: str
    stage_index: int
    total_stages: int
    papers: list[dict]
    errors: list[dict]
    is_single_tool: bool
    single_tool_name: str
    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# LiteratureAgent
# ═══════════════════════════════════════════════════════════════


class LiteratureAgent:
    """文献检索与下载 Agent — 5 节点线性 StateGraph。

    节点:
      search           → 跨源搜索论文
      evaluate         → LLM 评估相关性
      download         → 下载 PDF
      convert          → PDF → Markdown
      extract_metadata → 提取结构化元数据（方法/数据集/贡献）

    用法:
        runner = PipelineRunner(engine, db, llm, converter=converter)
        agent = LiteratureAgent(runner)
        graph = agent.compile()
        result = await graph.ainvoke({"project_id": "prj-xxx", "user_query": "..."})
    """

    def __init__(self, runner, on_progress=None):
        self.runner = runner
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(LiteratureState)

        builder.add_node("search", self._search_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_node("download", self._download_node)
        builder.add_node("convert", self._convert_node)
        builder.add_node("extract_metadata", self._extract_metadata_node)

        builder.add_edge(START, "search")
        builder.add_edge("search", "evaluate")
        builder.add_edge("evaluate", "download")
        builder.add_edge("download", "convert")
        builder.add_edge("convert", "extract_metadata")
        builder.add_edge("extract_metadata", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("LiteratureAgent not compiled — call compile() first")
        return self._graph

    # ── 节点 ─────────────────────────────────────────────

    async def _search_node(self, state: LiteratureState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "search":
            return {}

        await self._notify("搜索论文", 1, 5, "搜索论文", 0, 0)
        papers = await self.runner._search_stage(
            "literature", state["project_id"], state["user_query"],
            state.get("sources", ["arxiv", "semantic_scholar"]),
            state.get("year_from", 2022),
            state.get("max_results", 20),
            None,
        )
        await self._notify("搜索论文", 1, 5, f"搜索完成: {len(papers)} 篇", len(papers), len(papers))
        return {"papers": papers, "current_stage": "search", "stage_index": 1, "total_stages": 5}

    async def _evaluate_node(self, state: LiteratureState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "evaluate":
            return {}

        papers = state.get("papers", [])
        if not papers:
            return {"current_stage": "evaluate", "stage_index": 2}

        await self._notify("评估论文", 2, 5, "评估论文相关性", 0, len(papers))
        evaluations = await self.runner._evaluate_stage(
            "literature", state["project_id"], state["user_query"], papers, None,
        )

        for p, ev in zip(papers, evaluations):
            p["score"] = ev.get("score", 0)
            p["is_relevant"] = ev.get("is_relevant", False)

        relevant_count = sum(1 for p in papers if p.get("is_relevant", True))
        await self._notify("评估论文", 2, 5, f"评估完成: {relevant_count} 篇相关", len(papers), len(papers))
        return {"papers": papers, "current_stage": "evaluate", "stage_index": 2}

    async def _download_node(self, state: LiteratureState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "download":
            return {}

        papers = state.get("papers", [])
        relevant = [p for p in papers if p.get("is_relevant", True)]
        if not relevant:
            relevant = papers[:10]

        await self._notify("下载论文", 3, 5, f"下载论文 (0/{len(relevant)})", 0, len(relevant))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(relevant):
            await self._notify("下载论文", 3, 5, f"下载论文 ({i+1}/{len(relevant)})", i + 1, len(relevant))
            result = await self.runner._download_single(
                "literature", state["project_id"], paper, None,
            )
            paper["pdf_path"] = result.get("local_path", "")
            paper["download_ok"] = result.get("success", False)
            if not result.get("success"):
                errors.append({"stage": "download", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": result.get("error", "")})

        return {"papers": papers, "errors": errors, "current_stage": "download", "stage_index": 3}

    async def _convert_node(self, state: LiteratureState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "convert":
            return {}

        papers = state.get("papers", [])
        to_convert = [p for p in papers if p.get("download_ok") and p.get("pdf_path")]

        await self._notify("转换PDF", 4, 5, f"转换 PDF ({0}/{len(to_convert)})", 0, len(to_convert))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(to_convert):
            await self._notify("转换PDF", 4, 5, f"转换 PDF ({i+1}/{len(to_convert)})", i + 1, len(to_convert))
            result = await self.runner._convert_single(
                "literature", paper["paper_id"], paper["pdf_path"], paper.get("title", ""), None,
            )
            paper["markdown_path"] = result.get("markdown_path", "")
            paper["convert_ok"] = result.get("success", False)
            if not result.get("success"):
                errors.append({"stage": "convert", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": result.get("error", "")})

        return {"papers": papers, "errors": errors, "current_stage": "convert", "stage_index": 4}

    async def _extract_metadata_node(self, state: LiteratureState) -> dict:
        """提取结构化元数据 — 贡献/方法/数据集/代码链接等。

        这是 v3 新增的独立节点。之前元数据提取隐含在 index 阶段中，
        现在单独抽取出来作为 Literature Agent 的收尾节点。
        """
        if state.get("is_single_tool") and state.get("single_tool_name") != "extract_metadata":
            return {}

        papers = state.get("papers", [])
        converted = [p for p in papers if p.get("convert_ok") and p.get("markdown_path")]
        errors = state.get("errors", [])

        if not converted:
            await self._notify("提取元数据", 5, 5, "无可提取的论文（转换失败）", 0, 0)
            return {
                "current_stage": "extract_metadata", "stage_index": 5,
                "result": self._build_result(state),
            }

        await self._notify("提取元数据", 5, 5, f"提取元数据 ({0}/{len(converted)})", 0, len(converted))

        for i, paper in enumerate(converted):
            await self._notify("提取元数据", 5, 5, f"提取 ({i+1}/{len(converted)})", i + 1, len(converted))
            try:
                result = await self.runner._extract_metadata_single(
                    paper["paper_id"], paper["markdown_path"],
                    paper.get("title", ""), paper.get("abstract", ""),
                )
                paper["extracted_meta"] = result
                paper["extract_ok"] = True
            except Exception as e:
                logger.warning(f"Metadata extraction failed for {paper.get('title', '')}: {e}")
                paper["extract_ok"] = False
                errors.append({"stage": "extract_metadata", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": str(e)})

        return {
            "papers": papers, "errors": errors,
            "current_stage": "extract_metadata", "stage_index": 5,
            "result": self._build_result(state),
        }

    # ── Single Tool ──────────────────────────────────────

    async def run_single(self, tool_name: str, **kwargs) -> dict:
        """单独执行一个文献处理工具。"""
        return await self.runner.run_single(tool_name, **kwargs)

    # ── Helpers ──────────────────────────────────────────

    def _build_result(self, state: LiteratureState) -> dict:
        papers = state.get("papers", [])
        errors = state.get("errors", [])
        downloaded = sum(1 for p in papers if p.get("download_ok"))
        converted = sum(1 for p in papers if p.get("convert_ok"))
        return {
            "project_id": state.get("project_id", ""),
            "total_papers": len(papers),
            "downloaded": downloaded,
            "converted": converted,
            "failed": len(errors),
            "errors": errors[:20],
            "papers": papers,  # 包含所有中间状态，供 Knowledge Agent 消费
        }

    async def _notify(self, stage: str, index: int, total: int, message: str,
                       current: int = 0, paper_total: int = 0):
        logger.info(f"  LiteratureAgent [{index}/{total}] {stage}: {message}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, current, paper_total)
            except Exception as e:
                logger.debug(f"LiteratureAgent on_progress error: {e}")
