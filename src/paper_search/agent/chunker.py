"""章节感知分块器 — 按论文章节边界切分Markdown文本."""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    """论文文本块。"""
    text: str
    section_title: str          # 章节标题，如 "Introduction", "Method"
    section_level: int          # 标题级别 (1=#, 2=##, 3=###)
    paper_id: str = ""          # 关联论文 ID
    chunk_index: int = 0        # 在本论文中的序号
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.paper_id}:{self.chunk_index}:{self.section_title[:30]}"


class SectionChunker:
    """按 Markdown 标题边界切分论文文本。

    识别论文的章节结构:
    - # Title (论文标题)
    - ## Abstract
    - ## Introduction
    - ## Related Work
    - ## Method / Approach
    - ## Experiment / Results
    - ## Discussion
    - ## Conclusion
    - ## References

    每个章节内容独立为一个 chunk。
    过长章节（>2000字）会进一步拆分为子块。
    """

    # 章节标题正则: 匹配 # / ## / ### 等
    HEADING_PATTERN = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

    # 最大块大小（字符数），超过则进一步切分
    MAX_CHUNK_SIZE = 2000
    # 最小块大小
    MIN_CHUNK_SIZE = 50

    def chunk(self, md_text: str, paper_id: str = "") -> list[Chunk]:
        """切分 Markdown 论文文本。

        Args:
            md_text: 论文的 Markdown 全文。
            paper_id: 关联的论文 ID。

        Returns:
            Chunk 对象列表，每个代表一个章节/子节。
        """
        if not md_text:
            return []

        sections = self._split_by_headings(md_text)
        chunks = []

        for i, (level, title, content) in enumerate(sections):
            content = content.strip()
            if len(content) < self.MIN_CHUNK_SIZE:
                continue  # 跳过空章节

            # 跳过参考文献章节（不对其做向量化）
            if self._is_reference_section(title):
                break  # 参考文献后面的内容不再处理

            if len(content) <= self.MAX_CHUNK_SIZE:
                chunks.append(Chunk(
                    text=content,
                    section_title=title,
                    section_level=level,
                    paper_id=paper_id,
                    chunk_index=i,
                ))
            else:
                # 过长章节进一步切分
                sub_chunks = self._split_long_section(content, title, level, paper_id, i)
                chunks.extend(sub_chunks)

        logger.debug(f"切分: {paper_id[:10]} → {len(chunks)} chunks ({len(sections)} sections)")
        return chunks

    def _split_by_headings(self, md_text: str) -> list[tuple[int, str, str]]:
        """按标题边界切分，返回 [(级别, 标题, 内容), ...]"""
        sections = []
        matches = list(self.HEADING_PATTERN.finditer(md_text))

        if not matches:
            # 没有标题，整篇作为一个 chunk
            sections.append((0, "Full Text", md_text))
            return sections

        for i, match in enumerate(matches):
            level = len(match.group(1))
            title = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
            content = md_text[start:end].strip()
            sections.append((level, title, content))

        return sections

    def _split_long_section(
        self, content: str, section_title: str, level: int,
        paper_id: str, base_index: int,
    ) -> list[Chunk]:
        """将过长章节进一步切分为子块（按段落边界）。"""
        paragraphs = re.split(r"\n\n+", content)
        chunks = []
        current_text = ""
        sub_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_text) + len(para) > self.MAX_CHUNK_SIZE and current_text:
                chunks.append(Chunk(
                    text=current_text,
                    section_title=f"{section_title} (part {sub_idx})",
                    section_level=level + 1,
                    paper_id=paper_id,
                    chunk_index=base_index * 100 + sub_idx,
                ))
                current_text = para
                sub_idx += 1
            else:
                current_text = (current_text + "\n\n" + para).strip()

        if current_text and len(current_text) >= self.MIN_CHUNK_SIZE:
            chunks.append(Chunk(
                text=current_text,
                section_title=f"{section_title} (part {sub_idx})",
                section_level=level + 1,
                paper_id=paper_id,
                chunk_index=base_index * 100 + sub_idx,
            ))

        return chunks

    def _is_reference_section(self, title: str) -> bool:
        """判断是否是参考文献章节。"""
        title_lower = title.lower()
        ref_keywords = [
            "reference", "references",
            "bibliography",
            "acknowledgment", "acknowledgments", "acknowledgement",
            "appendix",
        ]
        return any(kw in title_lower for kw in ref_keywords)

    def chunk_batch(self, md_texts: list[tuple[str, str]]) -> list[list[Chunk]]:
        """批量切分。输入: [(paper_id, md_text), ...]"""
        results = []
        for paper_id, md_text in md_texts:
            chunks = self.chunk(md_text, paper_id)
            results.append(chunks)
        return results
