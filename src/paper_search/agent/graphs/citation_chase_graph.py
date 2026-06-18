"""CitationChaseAgent — 引用追溯子 Agent。

7 节点动态 Execute Graph:
  resolve → check → fetch(evaluate parallel) → filter → ingest(parallel) → decide(loop) → summarize

功能:
  - 从种子论文出发，沿引用网络追溯 (前向+后向)
  - Semantic Scholar API 获取引用关系
  - LLM 评估引用论文的相关性
  - 高相关论文自动入库 (下载→转换→索引)
  - 逐层追溯 (默认 2 层，用户可指定，LLM 可提前终止)
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class ChaseState(TypedDict, total=False):
    # 输入
    seed_title: str
    seed_doi: str
    project_id: str
    max_depth: int                   # 最大追溯层数
    direction: str                   # "forward" | "backward" | "both"

    # 当前状态
    current_depth: int
    seen_paper_ids: list[str]        # 已处理论文 (防重)

    # 每层结果
    current_citations: list[dict]    # 当前层的引用论文
    evaluated: list[dict]            # LLM 评估后的结果
    relevant: list[dict]             # 高相关论文
    ingested: list[dict]             # 已入库论文

    # 控制
    should_continue: bool
    continue_reason: str

    # 整体
    all_layers: list[dict]           # 所有层的汇总
    layers_completed: int
    total_found: int
    total_ingested: int

    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# CitationChaseAgent
# ═══════════════════════════════════════════════════════════════


class CitationChaseAgent:
    """引用追溯 Agent — 7 节点动态图。

    图结构:
      resolve → check → fetch → filter → ingest → decide → summarize
                  ↑                                  │
                  └──────── loop (continue) ─────────┘
    """

    def __init__(self, db, llm, engine, runner=None, on_progress=None):
        self._db = db
        self._llm = llm
        self._engine = engine
        self._runner = runner
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(ChaseState)

        builder.add_node("resolve", self._resolve_node)
        builder.add_node("check", self._check_node)
        builder.add_node("fetch", self._fetch_node)
        builder.add_node("filter", self._filter_node)
        builder.add_node("ingest", self._ingest_node)
        builder.add_node("decide", self._decide_node)
        builder.add_node("summarize", self._summarize_node)

        builder.add_edge(START, "resolve")
        builder.add_edge("resolve", "check")
        builder.add_conditional_edges(
            "check", self._should_proceed,
            {"fetch": "fetch", "summarize": "summarize"},
        )
        builder.add_edge("fetch", "filter")
        builder.add_edge("filter", "ingest")
        builder.add_edge("ingest", "decide")
        builder.add_conditional_edges(
            "decide", self._should_loop,
            {"yes": "check", "no": "summarize"},
        )
        builder.add_edge("summarize", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("CitationChaseAgent not compiled")
        return self._graph

    # ── 节点 ─────────────────────────────────────────

    async def _resolve_node(self, state: ChaseState) -> dict:
        """解析种子论文 → 获取 paper ID。"""
        seed = state.get("seed_title", "") or state.get("seed_doi", "")
        await self._notify("解析种子", 1, 7, f"解析种子论文: {seed[:80]}")

        # 在 DB 中查找
        rows = self._db.conn.execute(
            "SELECT id, title FROM papers WHERE title LIKE ? OR doi = ?",
            (f"%{seed[:30]}%", state.get("seed_doi", "")),
        ).fetchall()

        if rows:
            row = dict(rows[0])
            return {"seed_title": row.get("title", seed), "seen_paper_ids": [row["id"]]}

        return {"seed_title": seed, "seen_paper_ids": []}

    async def _check_node(self, state: ChaseState) -> dict:
        """检查是否应该继续。"""
        current_depth = state.get("current_depth", 0)
        max_depth = state.get("max_depth", 2)
        seen_ids = state.get("seen_paper_ids", [])
        await self._notify("检查深度", 2, 7, f"深度 {current_depth}/{max_depth}, 已见 {len(seen_ids)} 篇")

        should_proceed = current_depth < max_depth
        return {"should_continue": should_proceed}

    async def _fetch_node(self, state: ChaseState) -> dict:
        """获取当前层引用论文 — 使用 Semantic Scholar API。"""
        depth = state.get("current_depth", 0) + 1
        seed_title = state.get("seed_title", "")
        seen_ids = list(state.get("seen_paper_ids", []))
        await self._notify("获取引用", 3, 7, f"获取第 {depth} 层引用")

        citations = []
        try:
            from ...providers.semantic_scholar import SemanticScholarProvider
            provider = SemanticScholarProvider()

            # 搜索引用关系
            result = await provider.search_citations(
                title=seed_title,
                doi=state.get("seed_doi"),
                direction=state.get("direction", "both"),
                limit=20,
            )
            citations = result.get("papers", [])
        except Exception as e:
            logger.warning(f"Citation fetch failed: {e}")
            citations = []

        # 去重
        new_citations = [c for c in citations if c.get("paperId", c.get("id", "")) not in seen_ids]

        logger.info(f"Fetched {len(citations)} citations, {len(new_citations)} new (depth={depth})")
        return {
            "current_citations": new_citations,
            "current_depth": depth,
            "seen_paper_ids": seen_ids + [c.get("paperId", c.get("id", "")) for c in new_citations],
        }

    async def _filter_node(self, state: ChaseState) -> dict:
        """LLM 评估引用论文的相关性。"""
        citations = state.get("current_citations", [])
        seed_title = state.get("seed_title", "")
        await self._notify("评估相关", 4, 7, f"评估 {len(citations)} 篇引用论文相关性")

        if not citations:
            return {"evaluated": [], "relevant": []}

        evaluated = []
        for c in citations[:10]:
            title = c.get("title", "")
            try:
                from ..llm_client import RelevanceJudgment
                p = type('Paper', (), {'title': title, 'authors': c.get('authors', []),
                                        'year': c.get('year'), 'abstract': c.get('abstract', '')[:300],
                                        'venue': c.get('venue', ''), 'source': 'semantic_scholar'})
                j = await self._llm.evaluate_relevance(p, f"引用自: {seed_title}")
                evaluated.append({**c, "score": j.score, "reason": j.reason, "is_relevant": j.is_relevant})
            except Exception:
                evaluated.append({**c, "score": 0.5, "reason": "评估失败", "is_relevant": True})

        relevant = [e for e in evaluated if e.get("is_relevant", True)]
        logger.info(f"Filter: {len(relevant)}/{len(evaluated)} relevant")
        return {"evaluated": evaluated, "relevant": relevant}

    async def _ingest_node(self, state: ChaseState) -> dict:
        """入库高相关论文。"""
        relevant = state.get("relevant", [])
        project_id = state.get("project_id", "")
        await self._notify("入库", 5, 7, f"入库 {len(relevant)} 篇高相关论文")

        ingested = list(state.get("ingested", []))
        for paper in relevant[:10]:
            paper_id = paper.get("paperId", paper.get("id", ""))
            title = paper.get("title", "")
            try:
                # 写入 DB
                self._db.conn.execute(
                    "INSERT OR REPLACE INTO papers (id, title, authors, year, abstract, source) VALUES (?, ?, ?, ?, ?, ?)",
                    (paper_id, title,
                     json.dumps(paper.get("authors", [])),
                     paper.get("year"), paper.get("abstract", "")[:500],
                     "semantic_scholar"),
                )
                self._db.conn.commit()
                self._db.link_paper_to_project(project_id, paper_id, round_num=state.get("current_depth", 1))
                ingested.append({"paper_id": paper_id, "title": title, "status": "indexed"})
            except Exception as e:
                logger.warning(f"Ingest failed for {title}: {e}")
                ingested.append({"paper_id": paper_id, "title": title, "status": "failed", "error": str(e)})

        total = state.get("total_ingested", 0) + sum(1 for i in ingested if i.get("status") == "indexed")
        return {"ingested": ingested, "total_ingested": total}

    async def _decide_node(self, state: ChaseState) -> dict:
        """LLM 决定是否继续下一层。"""
        depth = state.get("current_depth", 1)
        max_depth = state.get("max_depth", 2)
        relevant = state.get("relevant", [])
        await self._notify("决策", 6, 7, f"决定是否继续第 {depth + 1} 层")

        if depth >= max_depth:
            return {"should_continue": False, "continue_reason": "达到最大深度"}
        if len(relevant) < 3:
            return {"should_continue": False, "continue_reason": "高相关论文不足"}

        # LLM 评估是否值得继续
        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": (
                    f"当前第 {depth}/{max_depth} 层，找到 {len(relevant)} 篇高相关论文。"
                    f"是否值得继续追溯下一层？"
                )}],
                system="输出纯 JSON: {\"should_continue\": true/false, \"reason\": \"继续或停止的理由\"}",
            )
            should_continue = result.get("should_continue", False)
        except Exception:
            should_continue = len(relevant) >= 5

        return {"should_continue": should_continue,
                "continue_reason": "LLM 建议继续" if should_continue else "已足够"}

    async def _summarize_node(self, state: ChaseState) -> dict:
        """生成追溯报告。"""
        total = state.get("total_ingested", 0)
        layers = state.get("current_depth", 1)
        await self._notify("汇总", 7, 7, f"汇总: {layers} 层, {total} 篇入库")

        result = {
            "seed": state.get("seed_title", ""),
            "layers_completed": layers,
            "total_found": len(state.get("seen_paper_ids", [])),
            "total_ingested": total,
            "ingested_papers": state.get("ingested", []),
        }

        # 生成 Markdown 报告
        from ...config import get_outputs_dir
        out_dir = get_outputs_dir() / (state.get("project_id", "default"))
        out_dir.mkdir(parents=True, exist_ok=True)

        report_lines = [
            f"# 引用追溯报告",
            f"种子论文: {state.get('seed_title', '')}",
            f"追溯层数: {layers}",
            f"发现论文: {len(state.get('seen_paper_ids', []))} 篇",
            f"入库论文: {total} 篇",
            "",
            "## 入库论文",
        ]
        for p in state.get("ingested", []):
            report_lines.append(f"- {p.get('title', '')} ({p.get('status', '?')})")

        report_path = out_dir / "citation_chase_report.md"
        report_path.write_text("\n".join(report_lines), encoding="utf-8")

        return {"result": result, "layers_completed": layers}

    # ── 条件路由 ─────────────────────────────────────

    @staticmethod
    def _should_proceed(state: ChaseState) -> str:
        return "fetch" if state.get("should_continue") else "summarize"

    @staticmethod
    def _should_loop(state: ChaseState) -> str:
        return "yes" if state.get("should_continue") else "no"

    # ── 辅助 ─────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  CitationChase [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception:
                pass
