"""PDF→Markdown 转换器 — 使用 docling (IBM) 将论文 PDF 转为结构化 Markdown.

功能:
  - 文本提取 + 公式 (LaTeX) + 表格 → 结构化 Markdown
  - convert_with_figures(): 同步提取嵌入图片到本地目录
"""

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PDFConverter:
    """PDF 到 Markdown 转换器。

    使用 docling (基于 PyMuPDF + 自家模型):
    - 保留标题层级、表格、LaTeX 公式
    - 识别页眉页脚并去除
    - 输出适合向量化的 Markdown 文本
    - MIT 许可证

    用法:
        converter = PDFConverter()
        md_path = await converter.convert(Path("paper.pdf"), Path("output/"))
        # → output/paper.md
    """

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._converter = None  # 延迟初始化

    def _get_converter(self):
        """延迟初始化 docling converter（线程安全）。"""
        if self._converter is None:
            from docling.document_converter import DocumentConverter
            self._converter = DocumentConverter()
        return self._converter

    async def convert(self, pdf_path: Path, output_dir: Path) -> Optional[Path]:
        """将单个 PDF 转为 Markdown。

        Args:
            pdf_path: PDF 文件路径。
            output_dir: 输出目录。

        Returns:
            生成的 .md 文件路径，失败返回 None。
        """
        if not pdf_path.exists():
            logger.error(f"PDF不存在: {pdf_path}")
            return None

        async with self._semaphore:
            return await asyncio.to_thread(self._convert_sync, pdf_path, output_dir)

    def _convert_sync(self, pdf_path: Path, output_dir: Path) -> Optional[Path]:
        """同步转换逻辑（在线程池中运行）。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"{pdf_path.stem}.md"

        # 跳过已转换的
        if md_path.exists() and md_path.stat().st_size > 100:
            logger.debug(f"Markdown已存在，跳过: {md_path.name}")
            return md_path

        try:
            converter = self._get_converter()
            result = converter.convert(str(pdf_path))
            md_text = result.document.export_to_markdown()

            if not md_text or len(md_text) < 100:
                logger.warning(f"转换结果太短 ({len(md_text)} chars): {pdf_path.name}")
                return None

            md_path.write_text(md_text, encoding="utf-8")
            logger.info(f"PDF→MD: {pdf_path.name} → {md_path.name} ({len(md_text)} chars)")
            return md_path

        except Exception as e:
            logger.error(f"PDF转换失败 ({pdf_path.name}): {e}")
            return None

    async def convert_batch(
        self, pdf_paths: list[Path], output_dir: Path
    ) -> list[Path]:
        """批量转换 PDF，限制并发数避免内存溢出。

        Returns:
            成功转换的 .md 文件路径列表。
        """
        tasks = [self.convert(p, output_dir) for p in pdf_paths]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    # ═══════════════════════════════════════════════════════════════
    # 图表提取 (KnowledgeAgent local PDF ingest)
    # ═══════════════════════════════════════════════════════════════

    async def convert_with_figures(
        self, pdf_path: Path, output_dir: Path, figures_dir: Path
    ) -> tuple[Optional[Path], list[dict]]:
        """PDF→MD + 图片提取。

        Args:
            pdf_path: PDF 文件路径。
            output_dir: Markdown 输出目录。
            figures_dir: 图片输出目录 (figures_dir/{paper_id}/)。

        Returns:
            (md_path, figures) — md_path 可能为 None（转换失败）；
            figures 为 list[dict]，每个含 id, caption, local_path, page_number, image_hash。
        """
        async with self._semaphore:
            return await asyncio.to_thread(
                self._convert_with_figures_sync, pdf_path, output_dir, figures_dir,
            )

    def _convert_with_figures_sync(
        self, pdf_path: Path, output_dir: Path, figures_dir: Path,
    ) -> tuple[Optional[Path], list[dict]]:
        """同步执行: MD 转换 + 图片提取。"""
        import fitz  # PyMuPDF

        output_dir.mkdir(parents=True, exist_ok=True)
        figures_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"{pdf_path.stem}.md"

        figures: list[dict] = []

        try:
            # Phase A: 提取图片 (PyMuPDF — docling 依赖已包含)
            pdf_doc = fitz.open(str(pdf_path))
            for page_num in range(len(pdf_doc)):
                page = pdf_doc[page_num]
                image_list = page.get_images(full=True)

                for img_idx, img_info in enumerate(image_list):
                    xref = img_info[0]
                    base_image = pdf_doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"] or "png"
                    image_md5 = hashlib.md5(image_bytes).hexdigest()

                    figure_id = str(uuid.uuid4())
                    figure_path = figures_dir / f"{figure_id}.{image_ext}"
                    figure_path.write_bytes(image_bytes)

                    page_text = page.get_text("text")
                    caption = self._extract_figure_caption(
                        page_text, img_idx, len(image_list),
                    )

                    figures.append({
                        "id": figure_id,
                        "caption": caption,
                        "figure_type": "figure",
                        "local_path": str(figure_path),
                        "page_number": page_num + 1,
                        "image_hash": image_md5,
                    })

            pdf_doc.close()

            # Phase B: MD 转换 (docling)
            converter = self._get_converter()
            result = converter.convert(str(pdf_path))
            md_text = result.document.export_to_markdown()

            if not md_text or len(md_text) < 100:
                logger.warning(f"转换结果太短 ({len(md_text)} chars): {pdf_path.name}")
                return None, figures

            md_path.write_text(md_text, encoding="utf-8")
            logger.info(
                f"PDF→MD+figures: {pdf_path.name} → {md_path.name} "
                f"({len(md_text)} chars, {len(figures)} figures)"
            )
            return md_path, figures

        except Exception as e:
            logger.error(f"PDF转换+图表提取失败 ({pdf_path.name}): {e}")
            return None, figures

    @staticmethod
    def _extract_figure_caption(page_text: str, img_index: int,
                                 total_images: int) -> str:
        """从页面文本中尝试提取图片 caption。"""
        patterns = [
            r'(?:Fig\.?|Figure)\s*\d+[\.:]\s*(.+?)(?:\n|$)',
            r'(?:图)\s*\d+[\.:]\s*(.+?)(?:\n|$)',
            r'FIGURE\s*\d+[\.:]\s*(.+?)(?:\n|$)',
        ]
        for pat in patterns:
            m = re.search(pat, page_text, re.IGNORECASE)
            if m:
                return m.group(1).strip()[:500]
        return ""
