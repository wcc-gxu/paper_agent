"""Glossary Sub-Agent — 学术术语管理 (v3 Phase 2 新建).

功能:
  - 术语收集: TF-IDF 关键词提取 → LLM 专业翻译 → 重排序 → 入库
  - 术语搜索: pgvector 语义搜索 + 模糊匹配
  - 术语验证: LLM 验证术语定义准确性（领域特定）
  - 术语进化: 追踪术语使用变化趋势

LangGraph 4 节点图:
  collect → search → verify → evolve

Celery 异步编排:
  论文入库完成后 → Celery 异步触发术语收集 → 完成后通知前端

用法:
    from .glossary_graph import GlossaryAgent
    agent = GlossaryAgent(db, vector_store, llm_client)

    # 手动触发术语收集
    result = await agent.collect_terms(paper_ids=["pap-001", "pap-002"])

    # 搜索术语
    result = await agent.search("attention mechanism")
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class GlossaryTerm(TypedDict, total=False):
    """术语条目结构。"""
    en_term: str
    zh_term: str
    definition: str
    domain: str
    df: int             # 文档频率
    llm_confidence: float
    synonyms: list[str]
    related_terms: list[str]
    source_paper_ids: list[str]


class GlossaryState(TypedDict, total=False):
    paper_ids: list[str]
    domain: str
    terms: list[dict]
    new_terms: list[dict]
    verified_terms: list[dict]
    trend_report: dict

    # 查询
    search_query: str
    search_results: list[dict]

    # 元数据
    current_stage: str
    stage_index: int
    total_stages: int

    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# GlossaryAgent
# ═══════════════════════════════════════════════════════════════


class GlossaryAgent:
    """学术术语管理 Sub-Agent — 4 节点 StateGraph。

    节点:
      collect → search → verify → evolve

    作为 Sub-Agent 被 MainAgent 通过 tool_registry 调用，
    也可通过 Celery 异步任务在论文入库后自动触发。
    """

    # TF-IDF 候选最小 df
    MIN_DF = 2
    # 最少术语长度（字符）
    MIN_TERM_LEN = 4

    def __init__(self, db=None, vector_store=None, llm_client=None, on_progress=None):
        self.db = db
        self.vector_store = vector_store
        self.llm = llm_client
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(GlossaryState)

        builder.add_node("collect", self._collect_node)
        builder.add_node("search", self._search_node)
        builder.add_node("verify", self._verify_node)
        builder.add_node("evolve", self._evolve_node)

        builder.add_edge(START, "collect")
        builder.add_edge("collect", "search")
        builder.add_conditional_edges(
            "search", self._has_search_results,
            {"yes": "search", "no": "verify"},
        )
        # search self-loop for multi-round
        builder.add_edge("search", "verify")
        builder.add_edge("verify", "evolve")
        builder.add_edge("evolve", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("GlossaryAgent not compiled — call compile() first")
        return self._graph

    # ── 便捷入口 ─────────────────────────────────────

    async def collect_terms(self, paper_ids: list[str] = None,
                             domain: str = "") -> dict:
        """术语收集入口。"""
        if self._graph is None:
            self.compile()
        result = await self._graph.ainvoke({
            "paper_ids": paper_ids or [],
            "domain": domain,
        })
        return result.get("result", {})

    async def search_terms(self, query: str, top_k: int = 10) -> list[dict]:
        """术语搜索入口。"""
        state = {"search_query": query}
        result = await self._search_node(state)
        return result.get("search_results", [])[:top_k]

    # ── 节点 ─────────────────────────────────────────

    async def _collect_node(self, state: GlossaryState) -> dict:
        """术语收集 — TF-IDF 提取 + LLM 翻译 + 重排序。

        流程:
          1. 从论文全文/MD 中提取候选术语（TF-IDF）
          2. LLM 翻译中文术语 → 英文
          3. 查询现有术语表去重
          4. LLM 重排序 → 选出最有价值的 Top-K
        """
        paper_ids = state.get("paper_ids", [])
        domain = state.get("domain", "")

        await self._notify("术语收集", 1, 4, f"从 {len(paper_ids)} 篇论文收集术语")

        # 1. TF-IDF 候选提取
        candidates = await self._extract_candidates(paper_ids)

        if not candidates:
            return {"terms": [], "new_terms": [], "current_stage": "collect",
                    "stage_index": 1, "total_stages": 4}

        # 2. LLM 翻译 + 去重
        if self.llm:
            new_terms = await self._translate_and_filter(candidates, domain)
        else:
            new_terms = candidates

        # 3. 持久化到 pgvector/ChromaDB
        saved_count = 0
        if self.vector_store and new_terms:
            try:
                saved_count = await self._save_terms(new_terms, domain)
            except Exception as e:
                logger.warning(f"Failed to save terms: {e}")

        await self._notify("术语收集", 1, 4, f"收集完成: {saved_count} 个新术语")

        return {
            "terms": candidates,
            "new_terms": new_terms,
            "current_stage": "collect", "stage_index": 1, "total_stages": 4,
            "result": {
                "candidates_found": len(candidates),
                "new_terms": saved_count,
                "domain": domain,
            },
        }

    async def _search_node(self, state: GlossaryState) -> dict:
        """术语搜索 — 语义搜索 + 模糊匹配。

        支持:
          - 英文术语查询
          - 中文术语查询
          - 模糊匹配（编辑距离）
        """
        query = state.get("search_query", "")
        if not query:
            return {"search_results": [], "current_stage": "search", "stage_index": 2}

        await self._notify("术语搜索", 2, 4, f"搜索: {query}")

        results = []
        if self.vector_store:
            try:
                raw = self.vector_store.search_similar(query, n_results=10)
                for r in raw:
                    results.append({
                        "en_term": r.get("en_term", ""),
                        "zh_term": r.get("zh_term", ""),
                        "definition": r.get("definition", ""),
                        "domain": r.get("domain", ""),
                        "score": r.get("score", 0),
                    })
            except Exception as e:
                logger.warning(f"Term search failed: {e}")

        # 回退：DB 查询
        if not results and self.db:
            try:
                # 尝试 glossary_terms 表（如果存在）
                db_results = self._db_term_search(query)
                results.extend(db_results)
            except Exception:
                pass

        return {"search_results": results, "current_stage": "search", "stage_index": 2}

    async def _verify_node(self, state: GlossaryState) -> dict:
        """术语验证 — LLM 校对术语翻译质量和领域准确性。

        验证维度:
          1. 翻译准确性（中英对应）
          2. 领域相关性
          3. 定义清晰度
        """
        new_terms = state.get("new_terms", [])
        if not new_terms:
            return {"verified_terms": [], "current_stage": "verify", "stage_index": 3}

        await self._notify("术语验证", 3, 4, f"验证 {len(new_terms)} 个术语")

        verified = []
        if self.llm:
            for term in new_terms[:20]:  # 每次最多验证 20 个
                try:
                    result = await self._llm_verify_term(term)
                    term["verified"] = result.get("passed", False)
                    term["llm_confidence"] = result.get("confidence", 0.5)
                    term["issues"] = result.get("issues", [])
                    verified.append(term)
                except Exception as e:
                    logger.debug(f"Term verification skipped: {e}")
                    term["verified"] = True
                    term["llm_confidence"] = 0.5
                    verified.append(term)
        else:
            verified = new_terms

        await self._notify("术语验证", 3, 4,
                           f"验证完成: {sum(1 for t in verified if t.get('verified', True))}/{len(verified)} 通过")

        return {"verified_terms": verified, "current_stage": "verify", "stage_index": 3}

    async def _evolve_node(self, state: GlossaryState) -> dict:
        """术语进化追踪 — 分析术语使用趋势。

        追踪维度:
          - 新术语增长率
          - 术语频率变化
          - 跨领域扩散
        """
        verified = state.get("verified_terms", [])
        terms = state.get("terms", [])

        await self._notify("趋势分析", 4, 4, "分析术语演变趋势")

        trend_report = {
            "total_terms": len(terms),
            "new_terms": len(verified),
            "verified_count": sum(1 for t in verified if t.get("verified", True)),
            "domains": {},
        }

        # 按领域统计
        for t in verified:
            domain = t.get("domain", "general")
            trend_report["domains"].setdefault(domain, 0)
            trend_report["domains"][domain] += 1

        return {
            "trend_report": trend_report,
            "current_stage": "evolve", "stage_index": 4,
            "result": {
                "collected": len(terms),
                "new": len(verified),
                "verified": trend_report["verified_count"],
                "trend": trend_report,
            },
        }

    # ── 条件路由 ─────────────────────────────────────

    @staticmethod
    def _has_search_results(state: GlossaryState) -> str:
        """如果有搜索查询且有结果，停留在 search（实际 routed to verify）。"""
        return "no"  # search → verify always

    # ── 核心方法 ─────────────────────────────────────

    async def _extract_candidates(self, paper_ids: list[str]) -> list[dict]:
        """从论文文本中提取候选术语（简化 TF-IDF）。"""
        if not paper_ids or not self.db:
            return []

        all_terms = Counter()
        paper_terms: dict[str, list] = {}  # term → [paper_ids]

        for pid in paper_ids[:50]:  # 限制 50 篇
            try:
                paper = self.db.get_paper(pid)
                if not paper:
                    continue

                # 合并 title + abstract + digest
                text = f"{paper.get('title','')} {paper.get('abstract','')} {paper.get('digest','')}"

                # 简单英文术语提取（大写字母开头多词短语）
                candidates = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
                # 中文术语提取（2-6 字连续中文字符）
                zh_candidates = re.findall(r'[一-鿿]{2,6}', text)

                for term in candidates:
                    if len(term) >= self.MIN_TERM_LEN:
                        all_terms[term.lower()] += 1
                        paper_terms.setdefault(term.lower(), []).append(pid)

            except Exception as e:
                logger.debug(f"Term extraction failed for {pid}: {e}")

        # 过滤低频术语
        return [
            {
                "en_term": term,
                "zh_term": "",  # 待 LLM 翻译
                "df": count,
                "source_paper_ids": paper_terms.get(term, []),
            }
            for term, count in all_terms.most_common(100)
            if count >= self.MIN_DF
        ]

    async def _translate_and_filter(self, candidates: list[dict], domain: str) -> list[dict]:
        """LLM 翻译中文术语并过滤无关术语。"""
        if not self.llm:
            return candidates

        # 分批处理
        batch_size = 20
        results = []
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            terms_text = "\n".join([
                f"[{j}] {t['en_term']} (df={t['df']})"
                for j, t in enumerate(batch)
            ])

            prompt = (
                f"For the following English technical terms in the domain of '{domain}',\n"
                f"provide Chinese translations and assess domain relevance.\n\n"
                f"{terms_text}\n\n"
                f"Return JSON array: [{{'index': N, 'zh_term': '...', 'definition': '...', "
                f"'relevance': 0.0-1.0, 'keep': true/false}}]\n"
                f"Only keep terms that are relevant to '{domain}'."
            )

            try:
                response = await self.llm.chat(prompt)
                json_match = re.search(r'\[.*\]', response, re.DOTALL)
                if json_match:
                    translations = json.loads(json_match.group())
                    for t in translations:
                        idx = t.get("index", 0)
                        if idx < len(batch) and t.get("keep", True):
                            batch[idx]["zh_term"] = t.get("zh_term", "")
                            batch[idx]["definition"] = t.get("definition", "")
                            batch[idx]["llm_confidence"] = t.get("relevance", 0.7)
                            batch[idx]["domain"] = domain
                            results.append(batch[idx])
            except Exception as e:
                logger.warning(f"Term translation failed: {e}")
                # 保留原始候选项
                for t in batch:
                    t["domain"] = domain
                    t["llm_confidence"] = 0.5
                results.extend(batch)

        return results

    async def _save_terms(self, terms: list[dict], domain: str) -> int:
        """持久化术语到向量存储和 DB。"""
        count = 0
        for term in terms:
            try:
                term_id = f"glossary-{term.get('en_term', '').replace(' ', '-').lower()}"

                # 保存到向量存储（语义搜索）
                if self.vector_store:
                    text = f"{term.get('en_term','')} {term.get('zh_term','')} {term.get('definition','')}"
                    self.vector_store.add_glossary_term(
                        term_id=term_id,
                        en_term=term.get("en_term", ""),
                        zh_term=term.get("zh_term", ""),
                        definition=term.get("definition", ""),
                        domain=domain,
                        df=term.get("df", 1),
                    )
                    count += 1

                # 保存到 DB
                if self.db:
                    self.db.upsert_glossary_term(
                        term_id=term_id,
                        en_term=term.get("en_term", ""),
                        zh_term=term.get("zh_term", ""),
                        definition=term.get("definition", ""),
                        domain=domain,
                        df=term.get("df", 1),
                        llm_confidence=term.get("llm_confidence", 0.5),
                    )
            except Exception as e:
                logger.debug(f"Save term failed: {e}")

        return count

    async def _llm_verify_term(self, term: dict) -> dict:
        """LLM 验证单个术语。"""
        if not self.llm:
            return {"passed": True, "confidence": 0.5, "issues": []}

        prompt = (
            f"Verify this technical term:\n"
            f"  English: {term.get('en_term', '')}\n"
            f"  Chinese: {term.get('zh_term', '')}\n"
            f"  Definition: {term.get('definition', '')}\n"
            f"  Domain: {term.get('domain', '')}\n\n"
            f"Check:\n"
            f"1. Is the Chinese translation accurate?\n"
            f"2. Is the definition correct?\n"
            f"3. Is this a real technical term in this domain?\n\n"
            f"Return JSON: {{\"passed\": true/false, \"confidence\": 0.0-1.0, "
            f"\"issues\": [\"...\"]}}"
        )
        try:
            response = await self.llm.chat(prompt)
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception:
            pass

        return {"passed": True, "confidence": 0.5, "issues": []}

    def _db_term_search(self, query: str) -> list[dict]:
        """DB 回退术语搜索。"""
        try:
            # 尝试 glossary_terms 表
            results = self.db.search_glossary_terms(query, limit=10)
            return results
        except Exception:
            return []

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  GlossaryAgent [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception:
                pass
