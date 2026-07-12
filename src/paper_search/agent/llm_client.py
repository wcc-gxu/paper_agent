"""LLM 客户端 — 火山引擎 Anthropic 协议兼容接口.

用途: 意图解析、相关性评估、搜索策略、报告生成。
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ── 火山引擎配置 ────────────────────────────────────────

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan"
ARK_MODEL = "ark-code-latest"


@dataclass
class SearchIntent:
    """LLM 解析的用户搜索意图。"""
    original_query: str
    sub_queries: list[str] = field(default_factory=list)
    time_range: Optional[str] = None       # "2024-2026" or "last 6 months"
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    sources: list[str] = field(default_factory=list)  # ["arxiv","pubmd",...]
    entities: list[str] = field(default_factory=list)  # 识别到的作者/DOI/论文名
    domain_hint: str = ""                  # "AI security", "CV", etc.


@dataclass
class RelevanceJudgment:
    """LLM 对单篇论文的相关性判断。"""
    score: float                    # 0.0 ~ 1.0
    reason: str                     # 一句话理由
    is_relevant: bool               # threshold >= 0.5


@dataclass
class ContinueDecision:
    """LLM 关于是否继续搜索的判断。"""
    should_continue: bool
    reason: str
    new_queries: list[str] = field(default_factory=list)
    new_sources: list[str] = field(default_factory=list)


class LLMClient:
    """火山引擎 LLM 调用封装。

    兼容 Anthropic Messages API 协议。
    模型: ark-code-latest
    """

    def __init__(
        self,
        base_url: str = ARK_BASE_URL,
        model: str = ARK_MODEL,
        api_key: str = None,
        max_tokens: int = 4096,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        if api_key:
            self.api_key = api_key
        else:
            # 确保 .env 已加载
            try:
                from dotenv import load_dotenv
                from pathlib import Path
                env_path = Path(__file__).parent.parent.parent.parent / ".env"
                if env_path.exists():
                    load_dotenv(env_path)
            except ImportError:
                pass
            self.api_key = os.environ.get("LLM_API_KEY") or os.environ.get("VOLCANO_API_KEY", "")
        self.max_tokens = max_tokens

    # ── 底层调用 ────────────────────────────────────────

    async def _chat(self, system_prompt: str, user_message: str, temperature: float = 0.3) -> str:
        """发送消息到 LLM，返回文本响应。"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # 提取 content
        content = data.get("content", [])
        if isinstance(content, list):
            return "".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
        return str(content)

    async def _chat_json(self, system_prompt: str, user_message: str) -> dict:
        """发送消息并解析 JSON 响应。"""
        text = await self._chat(system_prompt, user_message, temperature=0.1)
        # 尝试从 markdown 代码块或裸 JSON 中提取
        text = text.strip()
        if text.startswith("```"):
            # 移除 markdown 代码块标记
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取第一个 JSON 对象
            import re
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group(0))
            logger.warning(f"LLM 返回非 JSON: {text[:300]}")
            return {"error": "parse_failed", "raw": text[:500]}

    # ── Stage 1: 意图解析 ────────────────────────────────

    INTENT_SYSTEM_PROMPT = """你是一个学术搜索意图解析器。用户会用自然语言描述搜索需求，
你需要输出结构化的搜索计划。

可用搜索来源:
- arxiv: 预印本 (CS/AI/数学/物理)
- semantic_scholar: 综合学术搜索
- pubmed: 生物医学
- sciencedirect: Elsevier 综合期刊
- ieee: IEEE 电子/计算机工程
- cnki: 中国知网 (中文学术)

领域提示:
- "AI安全" / "对抗攻击" / "adversarial" → 侧重 arxiv, ieee, semantic_scholar
- "网络安全" / "cybersecurity" → 侧重 ieee, arxiv
- "医学" / "生物" → 侧重 pubmed
- 中文关键词 → 必须包含 cnki

对于时间范围:
- "最近半年" → year_from = 当前年份-1 到当前年份
- "2023年以来" → year_from = 2023
- 未提及 → 不设时间限制

输出纯 JSON（不要 markdown 代码块）:
{
  "sub_queries": ["拆解的子查询1", "子查询2"],
  "year_from": 2024,         // 或 null
  "year_to": 2026,           // 或 null
  "sources": ["arxiv", "semantic_scholar"],
  "entities": ["识别到的作者名", "论文DOI"],
  "domain_hint": "AI security"
}"""

    async def parse_intent(self, user_query: str) -> SearchIntent:
        """Stage 1: 解析用户自然语言为结构化搜索意图。"""
        today = datetime.now()
        user_message = (
            f"当前日期: {today.strftime('%Y-%m-%d')}\n"
            f"用户搜索: {user_query}\n\n"
            f"请解析搜索意图。"
        )
        try:
            result = await self._chat_json(self.INTENT_SYSTEM_PROMPT, user_message)
        except Exception as e:
            logger.error(f"意图解析 LLM 调用失败: {e}")
            # 降级：用原始查询直接搜
            return SearchIntent(
                original_query=user_query,
                sub_queries=[user_query],
                sources=["arxiv", "semantic_scholar"],
            )

        return SearchIntent(
            original_query=user_query,
            sub_queries=result.get("sub_queries", [user_query]),
            year_from=result.get("year_from"),
            year_to=result.get("year_to"),
            sources=result.get("sources", ["arxiv", "semantic_scholar"]),
            entities=result.get("entities", []),
            domain_hint=result.get("domain_hint", ""),
        )

    # ── Stage 4: 相关性评估 ───────────────────────────────

    RELEVANCE_SYSTEM_PROMPT = """你是一个论文学术价值评估器。给定用户的研究需求和一篇论文的元数据，
判断这篇论文与用户需求的相关性。

评分标准:
- 1.0: 完美匹配，论文核心就是用户要找的
- 0.7-0.9: 高度相关，值得精读
- 0.5-0.6: 部分相关，可作为参考
- 0.3-0.4: 勉强相关，领域接近但主题有偏差
- 0.0-0.2: 不相关

输出纯 JSON（不要代码块）:
{
  "score": 0.85,
  "reason": "一句话解释为什么给这个分数（中文）",
  "is_relevant": true
}"""

    async def evaluate_relevance(self, paper, user_query: str) -> RelevanceJudgment:
        """Stage 4: 评估单篇论文与用户查询的相关性。

        Args:
            paper: Paper 对象（含 title, abstract, authors, year, venue）。
            user_query: 用户原始搜索意图。

        Returns:
            RelevanceJudgment 包含评分、理由和是否相关。
        """
        user_message = (
            f"用户研究需求: {user_query}\n\n"
            f"论文标题: {paper.title}\n"
            f"作者: {', '.join(paper.authors[:5])}\n"
            f"年份: {paper.year or '未知'}\n"
            f"期刊/会议: {paper.venue or '未知'}\n"
            f"摘要: {(paper.abstract or '无')[:500]}\n"
        )
        try:
            result = await self._chat_json(self.RELEVANCE_SYSTEM_PROMPT, user_message)
        except Exception as e:
            # L4 fail-closed：评估失败 → 默认不相关（不再保留垃圾论文进入语料库）
            # 上游 evaluate_batch 收到 is_relevant=False 时会自动剔除
            logger.warning(
                f"相关性评估失败: {e}, FAIL-CLOSED → is_relevant=False (剔除该篇)"
            )
            return RelevanceJudgment(
                score=0.0, reason=f"评估失败 ({e})，按不相关处理", is_relevant=False,
            )

        return RelevanceJudgment(
            score=float(result.get("score", 0.5)),
            reason=result.get("reason", ""),
            is_relevant=result.get("is_relevant", True),
        )

    async def evaluate_batch(
        self, papers: list, user_query: str, max_concurrent: int = 5
    ) -> list[RelevanceJudgment]:
        """并发评估多篇论文的相关性。

        Args:
            papers: Paper 对象列表。
            user_query: 用户原始搜索意图。
            max_concurrent: 最大并发 LLM 调用数。
        """
        import asyncio

        semaphore = asyncio.Semaphore(max_concurrent)

        async def evaluate_one(paper):
            async with semaphore:
                return await self.evaluate_relevance(paper, user_query)

        tasks = [evaluate_one(p) for p in papers]
        return await asyncio.gather(*tasks)

    # ── Stage 3: 继续判断 ────────────────────────────────

    CONTINUE_SYSTEM_PROMPT = """你是一个学术搜索策略评估器。用户进行了一次文献搜索，
你需要判断是否已经搜够了，还是需要调整策略继续搜索。

判断标准:
- 已经找到10+篇高质量相关论文 → 可以停止
- 只找到0-3篇相关论文 → 需要调整搜索词继续
- 结果集中在某几个子领域，可能遗漏了其他方面 → 建议新的搜索方向
- 结果太少可能是搜索词太窄，建议放宽关键词

输出纯 JSON:
{
  "should_continue": true,
  "reason": "只找到2篇相关论文，建议用更宽泛的关键词重试或扩展到其他来源",
  "new_queries": ["broader keyword 1", "alternative keyword 2"],
  "new_sources": ["ieee"]
}"""

    async def should_continue_search(
        self,
        user_query: str,
        current_round: int,
        total_found: int,
        relevant_count: int,
        sample_titles: list[str],
    ) -> ContinueDecision:
        """Stage 3: 判断是否需要继续搜索。"""
        user_message = (
            f"用户原始需求: {user_query}\n"
            f"当前搜索轮次: 第 {current_round} 轮\n"
            f"共找到论文: {total_found} 篇\n"
            f"其中相关论文: {relevant_count} 篇\n"
            f"相关论文标题样本:\n" + "\n".join(f"- {t}" for t in sample_titles[:10])
        )
        try:
            result = await self._chat_json(self.CONTINUE_SYSTEM_PROMPT, user_message)
        except Exception as e:
            logger.warning(f"继续判断失败: {e}")
            return ContinueDecision(should_continue=False, reason="判断失败，停止搜索")

        return ContinueDecision(
            should_continue=result.get("should_continue", False),
            reason=result.get("reason", ""),
            new_queries=result.get("new_queries", []),
            new_sources=result.get("new_sources", []),
        )

    # ── Stage 6: 报告生成 ─────────────────────────────────

    # ── 摘要提炼 (Phase 3B 新增) ──────────────────────────

    DIGEST_SYSTEM_PROMPT = """你是一个学术论文摘要提炼器。给定论文元数据，输出结构化摘要:

输出纯 JSON:
{
  "digest": ["要点1", "要点2", "要点3", "要点4", "要点5"],
  "one_liner": "一句话总结这篇论文的贡献",
  "method_tags": ["方法1", "方法2"],
  "dataset_info": "用的数据集/基准",
  "reading_level": "skim"  // "skim"=粗略读即可 "deep"=值得细读 (考虑期刊等级)
}"""

    async def extract_digest(self, paper, journal_level: str = None) -> dict:
        """提炼论文关键要点和方法标签。"""
        user_message = (
            f"标题: {paper.title}\n"
            f"作者: {', '.join(paper.authors[:5])}\n"
            f"年份: {paper.year or '?'}\n"
            f"期刊/会议: {paper.venue or '未知'} (等级: {journal_level or '未评级'})\n"
            f"摘要: {(paper.abstract or '无')[:800]}\n"
        )
        try:
            return await self._chat_json(self.DIGEST_SYSTEM_PROMPT, user_message)
        except Exception as e:
            logger.warning(f"摘要提炼失败: {e}")
            return {"digest": [], "one_liner": "", "method_tags": [], "dataset_info": "", "reading_level": "skim"}

    # ── 报告生成 ─────────────────────────────────────────

    REPORT_SYSTEM_PROMPT = """你是一个学术搜索报告生成器。根据搜索结果生成结构化的文献综述摘要。

输出应包含以下部分（使用 Markdown 格式）:

## 搜索概况
- 原始需求
- 搜索来源
- 找到论文数 / 相关论文数

## 关键论文
对每篇高相关性论文 (>=0.7) 做简短描述:
- **标题**: 一句话概括
- **方法/贡献**: 一句话
- **来源/年份**: 出处

## 研究方向分类
将相关论文按主题分组（2-4 个组），每组一句话说明侧重点

## 建议
根据搜索结果，给出进一步研究的建议（2-3 条）"""

    async def generate_report(
        self,
        user_query: str,
        papers: list,
        judgments: list,
        db=None,
        project_id: Optional[str] = None,
    ) -> str:
        """Stage 6: 生成搜索报告。

        L2 反幻觉：传入 db 时，对生成的报告做 CitationVerifier 引用校验
        （parse + match，不做 fact-check 以控成本）。校验失败的引用会被标记
        ⚠️[verify] 或删除，报告末尾附审计段。
        """
        # 按评分排序
        scored = sorted(
            zip(papers, judgments),
            key=lambda x: x[1].score,
            reverse=True,
        )

        paper_summaries = []
        for p, j in scored[:30]:
            paper_summaries.append(
                f"- [{j.score:.2f}] {p.title} ({p.year or '?'}) | {p.source.value} | {p.venue or ''}\n"
                f"  理由: {j.reason}"
            )

        user_message = (
            f"用户搜索: {user_query}\n\n"
            f"搜索结果 (共 {len(papers)} 篇，展示前30):\n" + "\n".join(paper_summaries)
        )
        try:
            report = await self._chat(self.REPORT_SYSTEM_PROMPT, user_message, temperature=0.5)
        except Exception as e:
            logger.error(f"报告生成失败 ({type(e).__name__})", exc_info=True)
            return (
                f"# 搜索报告\n\n搜索: {user_query}\n找到 {len(papers)} 篇论文\n\n"
                f"(报告生成失败：{type(e).__name__}，详见服务日志)"
            )

        # L2 反幻觉：CitationVerifier 校验引用
        if db is not None:
            from .verifier import verify_and_wrap_report
            report = await verify_and_wrap_report(report, db, project_id)

        return report
