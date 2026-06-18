"""TranslationAgent — 中英学术术语翻译与术语库维护。

无 Graph (工具型 Agent):
  build_glossary     → 从论文集合构建术语库
  translate_query    → 中文查询 → 准确学术英文
  enrich_terminology → 入库后从新论文提取新术语

功能:
  - 维护学术术语库 (中英对照 + 学术语境 + 来源论文)
  - 中文查询时自动翻译为准确学术英文
  - 入库后从论文中提取新术语

术语库存储:
  SQLite: terminology 表
  ChromaDB: terminology 向量集合用于语义搜索
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class TranslationState(TypedDict, total=False):
    action: str                     # build_glossary | translate_query | enrich
    text: str                       # 翻译输入
    direction: str                  # zh2en | en2zh
    project_id: str

    # 翻译结果
    translation: str
    alternatives: list[str]
    context: str

    # 术语库操作
    terms_found: list[dict]         # 发现的新术语
    terms_added: int
    glossary_size: int

    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# TranslationAgent
# ═══════════════════════════════════════════════════════════════


class TranslationAgent:
    """学术术语翻译 Agent — 工具型 (非状态图流程)。

    三种操作模式:
      - translate_query: 翻译用户查询
      - build_glossary: 从论文构建术语库
      - enrich_terminology: 入库后提取新术语
    """

    def __init__(self, db, llm, chroma_store=None, on_progress=None):
        self._db = db
        self._llm = llm
        self._chroma = chroma_store
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        """编译路由图 — 根据 action 分发。"""
        builder = StateGraph(TranslationState)

        builder.add_node("route", self._route_node)
        builder.add_node("translate", self._translate_node)
        builder.add_node("build", self._build_node)
        builder.add_node("enrich", self._enrich_node)

        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route", self._dispatch,
            {"translate": "translate", "build": "build", "enrich": "enrich"},
        )
        builder.add_edge("translate", END)
        builder.add_edge("build", END)
        builder.add_edge("enrich", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("TranslationAgent not compiled")
        return self._graph

    # ── 路由 ─────────────────────────────────────────

    async def _route_node(self, state: TranslationState) -> dict:
        action = state.get("action", "translate_query")
        logger.info(f"TranslationAgent: action={action}")
        return {}

    @staticmethod
    def _dispatch(state: TranslationState) -> str:
        action = state.get("action", "translate_query")
        if action in ("build_glossary", "build"):
            return "build"
        elif action in ("enrich_terminology", "enrich"):
            return "enrich"
        else:
            return "translate"

    # ── 翻译 ─────────────────────────────────────────

    async def _translate_node(self, state: TranslationState) -> dict:
        """翻译查询文本 — 中文 → 学术英文。"""
        text = state.get("text", "")
        direction = state.get("direction", "zh2en")
        project_id = state.get("project_id", "")
        await self._notify("翻译", 1, 1, f"翻译: {text[:80]} ({direction})")

        # 先查术语库
        glossary_match = self._lookup_glossary(text, project_id)

        try:
            result = await self._llm.chat_json(
                messages=[{"role": "user", "content": (
                    f"请将以下学术查询翻译为{'英文' if direction == 'zh2en' else '中文'}:\n"
                    f"原文: {text}\n"
                    f"{'术语库参考: ' + json.dumps(glossary_match, ensure_ascii=False) if glossary_match else ''}"
                )}],
                system="""你是学术术语翻译专家。输出纯 JSON:
{
  "translation": "翻译结果",
  "alternatives": ["备选翻译1", "备选翻译2"],
  "notes": "翻译说明",
  "keywords": ["提取的英文关键词"]
}
翻译原则:
- 使用准确的学术术语，不用口语化表达
- 保留技术缩写 (CNN, LSTM, Transformer 等)
- 优先使用领域通用译法""",
            )
            translation = result.get("translation", text)
            alternatives = result.get("alternatives", [])
            keywords = result.get("keywords", [])
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            translation = text
            alternatives = []
            keywords = []

        logger.info(f"Translation: '{text[:50]}' → '{translation[:80]}'")
        return {
            "translation": translation,
            "alternatives": alternatives,
            "result": {
                "original": text,
                "translation": translation,
                "alternatives": alternatives,
                "keywords": keywords,
                "direction": direction,
            },
        }

    # ── 构建术语库 ───────────────────────────────────

    async def _build_node(self, state: TranslationState) -> dict:
        """从项目论文集合构建术语库。"""
        project_id = state.get("project_id", "")
        await self._notify("构建术语库", 1, 1, f"从项目 {project_id} 构建术语库")

        # 获取项目论文
        papers = self._db.get_project_papers(project_id)
        if not papers:
            papers = self._db.conn.execute(
                "SELECT * FROM papers ORDER BY year DESC LIMIT 100"
            ).fetchall()
            papers = [dict(r) for r in papers]

        terms_added = 0
        seen_terms = set()

        # 加载已有术语
        existing = self._db.conn.execute(
            "SELECT en_term, zh_term FROM terminology"
        ).fetchall()
        for row in existing:
            seen_terms.add((row["en_term"] or "").lower())

        for paper in papers[:50]:
            title = paper.get("title", "")
            abstract = paper.get("abstract", "")[:500]

            try:
                result = await self._llm.chat_json(
                    messages=[{"role": "user", "content": (
                        f"从以下论文中提取学术术语（中英对照）：\n"
                        f"标题: {title}\n"
                        f"摘要: {abstract}"
                    )}],
                    system="""你是学术术语提取器。输出纯 JSON:
{
  "terms": [
    {"en": "adversarial attack", "zh": "对抗攻击", "context": "AI安全领域"},
    {"en": "gradient descent", "zh": "梯度下降", "context": "优化方法"}
  ]
}
只提取有明确中英对照的学术术语，不确定的不要加。""",
                )
                for term in result.get("terms", []):
                    en = (term.get("en") or "").strip()
                    zh = (term.get("zh") or "").strip()
                    if en and zh and en.lower() not in seen_terms:
                        self._db.conn.execute(
                            "INSERT OR IGNORE INTO terminology (en_term, zh_term, context, source_paper_id) VALUES (?, ?, ?, ?)",
                            (en, zh, term.get("context", ""), paper.get("id", "")),
                        )
                        seen_terms.add(en.lower())
                        terms_added += 1
            except Exception as e:
                logger.warning(f"Term extraction failed for {title}: {e}")

        self._db.conn.commit()

        # 也索引到 ChromaDB
        if self._chroma and terms_added > 0:
            try:
                terms = self._db.conn.execute(
                    "SELECT en_term, zh_term, context FROM terminology ORDER BY rowid DESC LIMIT ?",
                    (terms_added,),
                ).fetchall()
                self._chroma.add_terms_batch([dict(t) for t in terms])
            except Exception:
                pass

        logger.info(f"Glossary built: {terms_added} new terms added (total: {len(seen_terms)})")
        return {
            "terms_added": terms_added,
            "glossary_size": len(seen_terms),
            "result": {"terms_added": terms_added, "glossary_size": len(seen_terms)},
        }

    # ── 丰富术语库 ───────────────────────────────────

    async def _enrich_node(self, state: TranslationState) -> dict:
        """从新入库论文提取新术语。"""
        project_id = state.get("project_id", "")
        await self._notify("丰富术语", 1, 1, "从新论文提取术语")

        # 获取最近入库的论文 (未提取过术语的)
        rows = self._db.conn.execute(
            """SELECT p.* FROM papers p
               LEFT JOIN terminology t ON p.id = t.source_paper_id
               WHERE t.source_paper_id IS NULL
               ORDER BY p.first_seen_at DESC LIMIT 20"""
        ).fetchall()

        new_papers = [dict(r) for r in rows]
        if not new_papers:
            return {"terms_added": 0, "result": {"message": "No new papers to enrich"}}

        # 委托给 build 逻辑 (复用)
        result = await self._build_node({**state, "papers": new_papers})
        logger.info(f"Enriched: {result.get('terms_added', 0)} new terms")
        return result

    # ── 术语库查询 ───────────────────────────────────

    def _lookup_glossary(self, text: str, project_id: str = "") -> list[dict]:
        """在术语库中查找匹配的术语。"""
        results = []
        try:
            # 中文关键词
            for word in text.split():
                if len(word) >= 2:
                    rows = self._db.conn.execute(
                        "SELECT en_term, zh_term, context FROM terminology WHERE zh_term LIKE ? OR en_term LIKE ? LIMIT 10",
                        (f"%{word}%", f"%{word}%"),
                    ).fetchall()
                    results.extend(dict(r) for r in rows)
        except Exception:
            pass
        return results[:10]

    # ── 直接调用接口 (工具型 Agent) ─────────────────

    async def translate_query(self, text: str, direction: str = "zh2en",
                              project_id: str = "") -> dict:
        """直接翻译查询 (不走 Graph)。"""
        state = {"action": "translate_query", "text": text,
                 "direction": direction, "project_id": project_id}
        result = await self._translate_node(state)
        return result.get("result", {})

    async def build_glossary(self, project_id: str) -> dict:
        """直接构建术语库。"""
        state = {"action": "build_glossary", "project_id": project_id}
        result = await self._build_node(state)
        return result.get("result", {})

    async def enrich_terminology(self, project_id: str) -> dict:
        """直接丰富术语库。"""
        state = {"action": "enrich_terminology", "project_id": project_id}
        result = await self._enrich_node(state)
        return result.get("result", {})

    # ── 辅助 ─────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  Translation [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception:
                pass
