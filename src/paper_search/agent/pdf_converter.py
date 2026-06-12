"""PDF→Markdown 转换器 — 使用 pymupdf4llm 将论文PDF转为结构化Markdown."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PDFConverter:
    """PDF 到 Markdown 转换器。

    使用 pymupdf4llm (基于 PyMuPDF):
    - 保留标题层级、表格、公式
    - 识别页眉页脚并去除
    - 输出适合向量化的 Markdown 文本

    用法:
        converter = PDFConverter()
        md_path = await converter.convert(Path("paper.pdf"), Path("output/"))
        # → output/paper.md
    """

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

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
        import pymupdf4llm

        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"{pdf_path.stem}.md"

        # 跳过已转换的
        if md_path.exists() and md_path.stat().st_size > 100:
            logger.debug(f"Markdown已存在，跳过: {md_path.name}")
            return md_path

        try:
            # 使用 pymupdf4llm 转换
            md_text = pymupdf4llm.to_markdown(
                str(pdf_path),
                write_images=False,  # 不提取图片（向量库不需要）
                page_chunks=False,   # 不按页分块
            )

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
