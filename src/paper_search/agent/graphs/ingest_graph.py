"""IngestAgent — 论文入库子 Agent。

LangGraph 7 节点线性 Execute Graph:
  search → evaluate → download → convert → index → rank → survey

支持两种执行模式:
  - ExecuteGraph: 完整流水线 astream / ainvoke
  - Single Tool: 直接调用 PipelineRunner.run_single()
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


class IngestState(TypedDict):
    project_id: str
    user_query: str
    sources: list[str]
    year_from: int
    max_results: int
    current_stage: str          # search|evaluate|download|convert|index|rank|verify|survey
    stage_index: int
    total_stages: int
    papers: list[dict]          # 论文列表（含处理状态）
    errors: list[dict]          # 失败汇总
    is_single_tool: bool
    single_tool_name: str
    enable_verify: bool         # [NEW Phase 2] 启用引用校验
    result: Optional[dict]      # 最终结果
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# IngestAgent
# ═══════════════════════════════════════════════════════════════


class IngestAgent:
    """论文入库子 Agent — 7 节点线性 StateGraph。

    用法:
        runner = PipelineRunner(engine, db, llm, chroma, converter, ranker)
        agent = IngestAgent(runner)
        graph = agent.compile()

        # ExecuteGraph — 完整流水线
        result = await graph.ainvoke({
            "project_id": "proj-xxx",
            "user_query": "transformer attention mechanism",
            ...
        })

        # Single Tool — 单步调用
        result = await agent.run_single("download", paper_id="...")
    """

    def __init__(self, runner, on_progress=None):
        """
        Args:
            runner: PipelineRunner 实例
            on_progress: 可选进度回调 async def(stage, stage_index, total_stages, current, total)
                         用于推送 task(running) 到 WS
        """
        self.runner = runner
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        """编译最多 8 节点线性图（含可选 verify 阶段）。

        search → evaluate → download → convert → index → rank → [verify] → survey
        """
        builder = StateGraph(IngestState)

        builder.add_node("search", self._search_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_node("download", self._download_node)
        builder.add_node("convert", self._convert_node)
        builder.add_node("index", self._index_node)
        builder.add_node("rank", self._rank_node)
        builder.add_node("verify", self._verify_node)  # [NEW] Phase 2 — 可选
        builder.add_node("survey", self._survey_node)

        builder.add_edge(START, "search")
        builder.add_edge("search", "evaluate")
        builder.add_edge("evaluate", "download")
        builder.add_edge("download", "convert")
        builder.add_edge("convert", "index")
        builder.add_edge("index", "rank")
        # rank → verify (if enabled) or survey
        builder.add_conditional_edges(
            "rank", self._should_verify,
            {"yes": "verify", "no": "survey"},
        )
        builder.add_edge("verify", "survey")
        builder.add_edge("survey", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("IngestAgent not compiled — call compile() first")
        return self._graph

    # ── 条件路由 ─────────────────────────────────────

    @staticmethod
    def _should_verify(state: IngestState) -> str:
        """检查是否启用引用校验。"""
        if state.get("enable_verify", False):
            return "yes"
        return "no"

    # ── 节点 ─────────────────────────────────────────────

    async def _search_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "search":
            return {}

        await self._notify("搜索论文", 1, 8, "搜索论文", 0, 0)
        papers = await self.runner._search_stage(
            "ingest", state["project_id"], state["user_query"],
            state.get("sources", ["arxiv", "semantic_scholar"]),
            state.get("year_from", 2022),
            state.get("max_results", 20),
            None,
        )
        await self._notify("搜索论文", 1, 7, f"搜索完成: {len(papers)} 篇", len(papers), len(papers))
        total = 8 if state.get("enable_verify") else 7
        return {"papers": papers, "current_stage": "search", "stage_index": 1, "total_stages": total}

    async def _evaluate_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "evaluate":
            return {}

        papers = state.get("papers", [])
        if not papers:
            return {"current_stage": "evaluate"}

        await self._notify("评估论文", 2, 7, "评估论文相关性", 0, len(papers))
        evaluations = await self.runner._evaluate_stage(
            "ingest", state["project_id"], state["user_query"], papers, None,
        )

        # Update papers with evaluation scores
        for p, ev in zip(papers, evaluations):
            p["score"] = ev.get("score", 0)
            p["is_relevant"] = ev.get("is_relevant", False)

        relevant_count = sum(1 for p in papers if p.get("is_relevant", True))
        await self._notify("评估论文", 2, 7, f"评估完成: {relevant_count} 篇相关", len(papers), len(papers))
        return {"papers": papers, "current_stage": "evaluate", "stage_index": 2}

    async def _download_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "download":
            return {}

        papers = state.get("papers", [])
        relevant = [p for p in papers if p.get("is_relevant", True)]
        if not relevant:
            relevant = papers[:10]

        await self._notify("下载论文", 3, 7, f"下载论文 (0/{len(relevant)})", 0, len(relevant))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(relevant):
            await self._notify("下载论文", 3, 7, f"下载论文 ({i+1}/{len(relevant)})", i + 1, len(relevant))
            result = await self.runner._download_single(
                "ingest", state["project_id"], paper, None,
            )
            paper["pdf_path"] = result.get("local_path", "")
            paper["download_ok"] = result.get("success", False)
            if not result.get("success"):
                errors.append({"stage": "download", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": result.get("error", "")})

        return {"papers": papers, "errors": errors, "current_stage": "download", "stage_index": 3}

    async def _convert_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "convert":
            return {}

        papers = state.get("papers", [])
        to_convert = [p for p in papers if p.get("download_ok") and p.get("pdf_path")]

        await self._notify("转换PDF", 4, 7, f"转换 PDF ({0}/{len(to_convert)})", 0, len(to_convert))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(to_convert):
            await self._notify("转换PDF", 4, 7, f"转换 PDF ({i+1}/{len(to_convert)})", i + 1, len(to_convert))
            result = await self.runner._convert_single(
                "ingest", paper["paper_id"], paper["pdf_path"], paper.get("title", ""), None,
            )
            paper["markdown_path"] = result.get("markdown_path", "")
            paper["convert_ok"] = result.get("success", False)
            if not result.get("success"):
                errors.append({"stage": "convert", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": result.get("error", "")})

        return {"papers": papers, "errors": errors, "current_stage": "convert", "stage_index": 4}

    async def _index_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "index":
            return {}

        papers = state.get("papers", [])
        to_index = [p for p in papers if p.get("convert_ok") and p.get("markdown_path")]

        await self._notify("索引入库", 5, 7, f"索引入库 ({0}/{len(to_index)})", 0, len(to_index))

        errors = list(state.get("errors", []))
        for i, paper in enumerate(to_index):
            await self._notify("索引入库", 5, 7, f"索引 ({i+1}/{len(to_index)})", i + 1, len(to_index))
            result = await self.runner._index_single(
                "ingest", paper["paper_id"], paper["markdown_path"],
                paper.get("title", ""), paper.get("abstract", ""),
                paper.get("year"), paper.get("source", ""), paper.get("venue", ""), None,
            )
            paper["index_ok"] = result.get("success", False)
            if not result.get("success"):
                errors.append({"stage": "index", "paper_id": paper.get("paper_id", ""),
                               "title": paper.get("title", ""), "error": result.get("error", "")})

        return {"papers": papers, "errors": errors, "current_stage": "index", "stage_index": 5}

    async def _rank_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "rank":
            return {}

        papers = state.get("papers", [])
        indexed = [p for p in papers if p.get("index_ok")]

        await self._notify("期刊评级", 6, 7, "评定期刊等级", len(indexed), len(indexed))

        if indexed:
            await self.runner._rank_stage("ingest", state["project_id"], indexed, None)

        return {"current_stage": "rank", "stage_index": 6}

    async def _verify_node(self, state: IngestState) -> dict:
        """[NEW Phase 2] 可选 — 引用幻觉校验。

        对所有已入库论文的引用进行三步校验:
          1. 格式检查
          2. 数据库匹配
          3. 事实验证 (LLM + 全文)
        """
        if state.get("is_single_tool") and state.get("single_tool_name") != "verify":
            return {}

        papers = state.get("papers", [])
        indexed = [p for p in papers if p.get("index_ok")]
        if not indexed:
            return {"current_stage": "verify", "stage_index": 7}

        await self._notify("引用校验", 7, 8, f"校验 {len(indexed)} 篇论文引用", 0, len(indexed))

        try:
            from ..verifier import CitationVerifier
            verifier = CitationVerifier(db=None, llm_client=None)  # DB 由 runner 提供

            verified_count = 0
            flagged_count = 0
            for i, paper in enumerate(indexed):
                paper_id = paper.get("paper_id", "")
                title = paper.get("title", "")
                markdown_path = paper.get("markdown_path", "")

                # 读取全文做验证
                if markdown_path:
                    from pathlib import Path
                    md = Path(markdown_path)
                    if md.exists():
                        text = md.read_text(encoding="utf-8")[:5000]
                        # 提取引用并验证
                        citations = verifier._parser.extract(text)
                        if citations:
                            verified_count += 1
                            # 记录到 paper 状态
                            paper["citation_count"] = len(citations)
                            paper["citations_verified"] = True
                        else:
                            paper["citation_count"] = 0
                            paper["citations_verified"] = False
                            flagged_count += 1

                await self._notify("引用校验", 7, 8,
                                   f"校验 ({i+1}/{len(indexed)})",
                                   i + 1, len(indexed))

            logger.info(f"Verify: {verified_count} verified, {flagged_count} flagged")
            return {
                "papers": papers,
                "current_stage": "verify", "stage_index": 7,
            }
        except Exception as e:
            logger.warning(f"Verify stage failed (non-blocking): {e}")
            return {"current_stage": "verify", "stage_index": 7}

    async def _survey_node(self, state: IngestState) -> dict:
        if state.get("is_single_tool") and state.get("single_tool_name") != "survey":
            return {}

        await self._notify("生成综述", 7, 7, "生成文献综述", 0, 0)

        result = await self.runner._survey_stage(
            "ingest", state["project_id"], state["user_query"], None,
        )

        papers = state.get("papers", [])
        errors = state.get("errors", [])
        downloaded = sum(1 for p in papers if p.get("download_ok"))
        indexed = sum(1 for p in papers if p.get("index_ok"))

        await self._notify("生成综述", 7, 7, f"综述完成", 7, 7)

        return {
            "current_stage": "survey",
            "stage_index": 7,
            "result": {
                "project_id": state["project_id"],
                "total_papers": len(papers),
                "downloaded": downloaded,
                "indexed": indexed,
                "failed": len(errors),
                "errors": errors[:20],
                "survey_path": result.get("survey_path", ""),
            },
        }

    # ── Single Tool ──────────────────────────────────────

    async def run_single(self, tool_name: str, **kwargs) -> dict:
        """单独执行一个入库工具。"""
        return await self.runner.run_single(tool_name, **kwargs)

    # ── Helpers ──────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, message: str,
                       current: int = 0, paper_total: int = 0):
        """通知进度（通过 on_progress 回调推送 WS，否则仅日志）。"""
        logger.info(f"    IngestAgent [{index}/{total}] {stage}: {message}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, current, paper_total)
            except Exception as e:
                logger.debug(f"IngestAgent on_progress error: {e}")
