"""引用幻觉防控系统 — 严格校验 LLM 输出中的引用.

核心流程:
  1. 解析: 从 LLM 输出中提取所有引用标记
  2. 匹配: 与 SQLite 论文库交叉校验
  3. 事实校验: 验证声明内容是否与原文一致
  4. 处理: 自动修正轻微错误 / 标记严重不匹配 / 删除无法修复的引用

使用方式:
    from paper_search.agent.verifier import CitationVerifier

    verifier = CitationVerifier(db, llm_client)

    # 校验 LLM 生成的综述报告
    report = await verifier.verify(report_text, project_id)

    # 查看修正后的报告
    print(report.verified_text)

    # 查看校验报告
    print(report.verification_summary)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class CitationMatch:
    """单个引用的校验结果."""
    # 原始引用
    raw_text: str  # 原始引用文本
    raw_context: str = ""  # 引用所在的声明上下文

    # 匹配结果
    matched: bool = False
    matched_paper_id: str = ""
    matched_title: str = ""
    match_score: float = 0.0  # 匹配置信度 0-1
    match_method: str = ""  # "exact_author_year" | "fuzzy_title" | "doi" | "none"

    # 事实校验
    claim_verified: bool = False  # 声明是否与原文一致
    claim_score: float = 0.0
    claim_assessment: str = ""

    # 处理
    action: str = ""  # "keep" | "fix" | "flag" | "delete"
    fix_suggestion: str = ""
    fixed_text: str = ""


@dataclass
class VerificationReport:
    """校验报告."""
    original_text: str
    verified_text: str  # 修正后的文本
    citations_found: int
    citations_matched: int
    citations_verified: int  # 事实也验证通过的
    citations_flagged: int  # 需要人工审核的
    citations_deleted: int  # 无法修复已删除的
    details: list[CitationMatch] = field(default_factory=list)

    @property
    def verification_summary(self) -> str:
        return (
            f"引用校验完成: {self.citations_found} 个引用发现, "
            f"{self.citations_matched} 个匹配论文, "
            f"{self.citations_verified} 个事实验证通过, "
            f"{self.citations_flagged} 个需人工审核, "
            f"{self.citations_deleted} 个已删除"
        )


# ═══════════════════════════════════════════════════════════════
# Citation Parser
# ═══════════════════════════════════════════════════════════════


class CitationParser:
    """从文本中提取引用标记.

    支持的格式:
    - [Author, Year] 或 [Author et al., Year]
    - (Author, Year) 或 (Author et al., Year)
    - [N] 或 [N, M, K] (编号引用)
    - Author (Year) 内联引用
    """

    # 匹配 [Author, Year] / [Author et al., Year]
    BRACKET_CITATION = re.compile(
        r'\[([^\]]+?,\s*\d{4}[a-z]?)\]'
    )

    # 匹配 (Author, Year) / (Author et al., Year)
    PAREN_CITATION = re.compile(
        r'\(([^)]+?,\s*\d{4}[a-z]?)\)'
    )

    # 匹配 Author (Year) 内联
    INLINE_CITATION = re.compile(
        r'([A-Z][a-z]+(?:\s+(?:et\s+al\.?|&\s+[A-Z][a-z]+))?)\s*\((\d{4}[a-z]?)\)'
    )

    # 匹配 [N] 编号引用
    NUMBERED_CITATION = re.compile(
        r'\[(\d+(?:\s*,\s*\d+)*)\]'
    )

    def extract(self, text: str) -> list[dict]:
        """从文本中提取所有引用标记.

        Returns:
            [{"type": "bracket"|"paren"|"inline"|"numbered",
              "match": "完整匹配文本",
              "author_part": "作者部分",
              "year": 年份,
              "span": (start, end),
              "context": "前后50字符上下文"}]
        """
        citations = []

        # [Author, Year]
        for m in self.BRACKET_CITATION.finditer(text):
            citation_text = m.group(0)
            inner = m.group(1)
            parts = inner.rsplit(",", 1)
            author_part = parts[0].strip() if len(parts) > 1 else inner
            year_str = parts[1].strip() if len(parts) > 1 else ""
            year = self._parse_year(year_str)

            ctx_start = max(0, m.start() - 80)
            ctx_end = min(len(text), m.end() + 80)
            context = text[ctx_start:ctx_end]

            citations.append({
                "type": "bracket",
                "match": citation_text,
                "author_part": author_part,
                "year": year,
                "span": (m.start(), m.end()),
                "context": context,
            })

        # (Author, Year) — 避免与括号内的普通内容混淆
        for m in self.PAREN_CITATION.finditer(text):
            citation_text = m.group(0)
            inner = m.group(1)
            # 过滤明显的非引用（如 "(see Figure 1)"）
            if re.search(r'\d{4}', inner) and re.search(r'[A-Z]', inner):
                parts = inner.rsplit(",", 1)
                author_part = parts[0].strip() if len(parts) > 1 else inner
                year_str = parts[1].strip() if len(parts) > 1 else ""
                year = self._parse_year(year_str)

                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(text), m.end() + 80)
                context = text[ctx_start:ctx_end]

                citations.append({
                    "type": "paren",
                    "match": citation_text,
                    "author_part": author_part,
                    "year": year,
                    "span": (m.start(), m.end()),
                    "context": context,
                })

        # 编号引用 [N] — 需要参考文献列表才能解析
        # 这里先提取出来
        for m in self.NUMBERED_CITATION.finditer(text):
            citation_text = m.group(0)
            numbers = [int(n.strip()) for n in m.group(1).split(",") if n.strip().isdigit()]

            ctx_start = max(0, m.start() - 80)
            ctx_end = min(len(text), m.end() + 80)
            context = text[ctx_start:ctx_end]

            citations.append({
                "type": "numbered",
                "match": citation_text,
                "numbers": numbers,
                "author_part": "",
                "year": None,
                "span": (m.start(), m.end()),
                "context": context,
            })

        return citations

    def _parse_year(self, year_str: str) -> Optional[int]:
        """从字符串中提取年份."""
        match = re.search(r'(\d{4})', year_str)
        return int(match.group(1)) if match else None


# ═══════════════════════════════════════════════════════════════
# Citation Verifier
# ═══════════════════════════════════════════════════════════════


class CitationVerifier:
    """引用严格校验引擎.

    对 LLM 输出中的每个引用:
    1. 在 SQLite 论文库中查找匹配论文
    2. 验证声明与原文的一致性
    3. 自动修正 / 标记 / 删除
    """

    # 匹配阈值
    EXACT_AUTHOR_MATCH_THRESHOLD = 0.85
    FUZZY_TITLE_MATCH_THRESHOLD = 0.70

    def __init__(self, db, llm_client=None):
        self._db = db
        self._llm = llm_client  # 可选, 用于高级事实校验
        self._parser = CitationParser()

    async def verify(
        self,
        text: str,
        project_id: str = None,
        auto_fix: bool = True,
    ) -> VerificationReport:
        """校验文本中的所有引用.

        Args:
            text: 包含引用的文本 (如 LLM 生成的综述报告)
            project_id: 关联的项目 ID (限制校验范围)
            auto_fix: 是否自动修正可修复的引用

        Returns:
            VerificationReport
        """
        # Step 1: 提取引用
        raw_citations = self._parser.extract(text)
        if not raw_citations:
            return VerificationReport(
                original_text=text,
                verified_text=text,
                citations_found=0,
                citations_matched=0,
                citations_verified=0,
                citations_flagged=0,
                citations_deleted=0,
            )

        # Step 2: 匹配每篇引用到数据库中的论文
        matches = []
        for rc in raw_citations:
            match = await self._match_citation(rc, project_id)
            matches.append(match)

        # Step 3: 事实校验 (如果有 LLM)
        if self._llm:
            matches = await self._verify_claims(matches)

        # Step 4: 确定处理动作
        for m in matches:
            if not m.matched:
                m.action = "delete"
                m.fix_suggestion = "无法在论文库中找到匹配论文"
            elif m.matched and m.claim_verified:
                m.action = "keep"
            elif m.matched and not m.claim_verified:
                if m.claim_score > 0.5:
                    m.action = "flag"
                    m.fix_suggestion = "声明可能与原文不完全一致，建议人工审核"
                else:
                    m.action = "flag"
                    m.fix_suggestion = "声明与原文严重不一致，需要修正"

        # Step 5: 应用修正
        verified_text = text
        if auto_fix:
            verified_text = self._apply_fixes(text, matches)

        return VerificationReport(
            original_text=text,
            verified_text=verified_text,
            citations_found=len(raw_citations),
            citations_matched=sum(1 for m in matches if m.matched),
            citations_verified=sum(1 for m in matches if m.claim_verified),
            citations_flagged=sum(1 for m in matches if m.action == "flag"),
            citations_deleted=sum(1 for m in matches if m.action == "delete"),
            details=matches,
        )

    async def _match_citation(self, raw_citation: dict, project_id: str = None) -> CitationMatch:
        """在数据库中查找匹配论文."""
        rc = raw_citation
        author_part = rc.get("author_part", "")
        year = rc.get("year")

        cm = CitationMatch(
            raw_text=rc["match"],
            raw_context=rc.get("context", ""),
        )

        if rc["type"] == "numbered":
            # 编号引用需要参考文献列表才能解析
            return cm  # 暂时无法处理

        # 策略1: 精确匹配 Author + Year
        if author_part and year:
            # 在 DB 中搜索
            like_author = f"%{author_part.split(',')[0].split()[0]}%"  # 取第一作者姓氏
            rows = self._db.conn.execute(
                "SELECT * FROM papers WHERE authors LIKE ? AND year = ?",
                (like_author, year),
            ).fetchall()

            if rows:
                best = self._best_match(author_part, rows)
                if best:
                    rd = dict(best)
                    cm.matched = True
                    cm.matched_paper_id = rd.get("id", "")
                    cm.matched_title = rd.get("title", "")
                    cm.match_score = 0.9
                    cm.match_method = "exact_author_year"
                    return cm

        # 策略2: 模糊匹配 title（在 project 范围内）
        if project_id:
            papers = self._db.get_project_papers(project_id)
            for p in papers:
                title = p.get("title", "")
                # 检查引用文本是否出现在标题中
                score = SequenceMatcher(None, rc["match"].lower(), title.lower()).ratio()
                if score > self.FUZZY_TITLE_MATCH_THRESHOLD:
                    cm.matched = True
                    cm.matched_paper_id = p.get("id", "")
                    cm.matched_title = title
                    cm.match_score = score
                    cm.match_method = "fuzzy_title"
                    return cm

        # 策略3: 从上下文中提取可能的标题片段再搜索
        if not cm.matched and rc.get("context"):
            # 尝试在上下文中找到论文标题
            context_lower = rc["context"].lower()
            # 假设标题用引号或斜体标记
            title_matches = re.findall(r'"([^"]{10,200})"', rc["context"])
            title_matches += re.findall(r"'([^']{10,200})'", rc["context"])
            title_matches += re.findall(r'\*([^*]{10,200})\*', rc["context"])

            for tm in title_matches:
                # 在 DB 中搜索相似标题
                papers = self._db.conn.execute(
                    "SELECT * FROM papers WHERE title LIKE ?", (f"%{tm[:30]}%",)
                ).fetchall()
                if papers:
                    rd = dict(papers[0])
                    cm.matched = True
                    cm.matched_paper_id = rd.get("id", "")
                    cm.matched_title = rd.get("title", "")
                    cm.match_score = 0.7
                    cm.match_method = "context_title"
                    return cm

        return cm

    def _best_match(self, author_part: str, rows) -> Optional[dict]:
        """从 DB 行中找最佳作者匹配."""
        best = None
        best_score = 0

        for row in rows:
            try:
                authors = json.loads(row["authors"]) if isinstance(row["authors"], str) else (row["authors"] or [])
            except (json.JSONDecodeError, TypeError):
                authors = []

            author_str = ", ".join(a for a in authors[:3])
            score = SequenceMatcher(None, author_part.lower(), author_str.lower()).ratio()

            if score > best_score:
                best_score = score
                best = row

        if best_score > self.EXACT_AUTHOR_MATCH_THRESHOLD:
            return best
        return None

    async def _verify_claims(self, matches: list[CitationMatch]) -> list[CitationMatch]:
        """使用 LLM 验证声明与原文的一致性（含 PDF 全文接入）。"""
        if not self._llm:
            return matches

        for m in matches:
            if not m.matched or not m.raw_context:
                continue

            # ── Bugfix: 全文接入 — 读取论文 Markdown 用于事实验证 ──
            paper_content = ""
            paper_id = m.matched_paper_id
            if paper_id and self._db:
                try:
                    row = self._db.conn.execute(
                        "SELECT title, abstract, markdown_path FROM papers WHERE id = ?",
                        (paper_id,),
                    ).fetchone()
                    if row:
                        rd = dict(row)
                        # 优先使用全文
                        md_path = rd.get("markdown_path", "")
                        if md_path:
                            from pathlib import Path
                            p = Path(md_path)
                            if p.exists():
                                # 限制全文长度避免超出 token 限制
                                full_text = p.read_text(encoding="utf-8")
                                # 取引言+结论最关键的部分
                                intro = full_text[:3000]
                                conclusion = full_text[-2000:] if len(full_text) > 2000 else ""
                                paper_content = f"全文引言: {intro}\n\n全文结论: {conclusion}"
                            else:
                                paper_content = f"摘要: {rd.get('abstract', '')[:1500]}"
                        else:
                            paper_content = f"摘要: {rd.get('abstract', '')[:1500]}"
                except Exception as e:
                    logger.debug(f"Failed to read full text for {paper_id}: {e}")

            # 构建校验上下文
            if paper_content:
                user_content = (
                    f"论文标题: {m.matched_title}\n"
                    f"论文内容:\n{paper_content}\n\n"
                    f"声明文本: {m.raw_context}\n\n"
                    f"请判断：该声明内容是否准确反映了这篇论文的实际内容？"
                )
            else:
                user_content = (
                    f"论文标题: {m.matched_title}\n"
                    f"声明文本: {m.raw_context}\n\n"
                    f"请判断：该声明内容是否准确反映了这篇论文的实际内容？（注意：无全文，仅基于标题判断，confidence 应降低）"
                )

            try:
                result = await self._llm.chat_json(
                    messages=[{"role": "user", "content": user_content}],
                    system="""你是一个学术事实校验器。判断一个声明是否准确反映了论文的实际内容。

输出纯 JSON:
{
  "consistent": true,
  "confidence": 0.9,
  "assessment": "声明准确反映了论文的核心贡献和方法",
  "issues": []
}

如果不一致:
{
  "consistent": false,
  "confidence": 0.2,
  "assessment": "声明中说该论文使用了Transformer，但实际上该论文使用的是CNN",
  "issues": ["方法错误", "应改为CNN-based approach"]
}

重要原则:
- 若提供了全文，基于全文内容判断；若仅提供了摘要，confidence 不应超过 0.6
- 不确定时宁保守，标记为 inconsistent
- 只判断声明是否与论文内容一致，不评价论文质量""",
                )
                m.claim_verified = result.get("consistent", False)
                m.claim_score = result.get("confidence", 0.5)
                m.claim_assessment = result.get("assessment", "")
            except Exception as e:
                logger.warning(f"Claim verification failed: {e}")
                m.claim_score = 0.5

        return matches

    def _apply_fixes(self, text: str, matches: list[CitationMatch]) -> str:
        """应用修正到文本."""
        # 从后往前替换（保持位置不变）
        fixes = []
        for m in matches:
            if m.action == "delete":
                fixes.append((m.raw_text, "[citation needed]"))
            elif m.action == "flag":
                fixes.append((m.raw_text, f"{m.raw_text} ⚠️[verify]"))

        # 按位置排序（从后往前）
        for old, new in sorted(fixes, key=lambda x: -text.find(x[0])):
            text = text.replace(old, new, 1)

        return text


# ═══════════════════════════════════════════════════════════════
# Quick verification (synchronous, no LLM)
# ═══════════════════════════════════════════════════════════════


def quick_verify_citation(db, author_surname: str, year: int, project_id: str = None) -> Optional[dict]:
    """快速校验单个引用（同步, 无 LLM）.

    Returns:
        匹配的论文 dict 或 None
    """
    like_author = f"%{author_surname}%"
    rows = db.conn.execute(
        "SELECT * FROM papers WHERE authors LIKE ? AND year = ?",
        (like_author, year),
    ).fetchall()

    if not rows:
        return None

    return dict(rows[0])
