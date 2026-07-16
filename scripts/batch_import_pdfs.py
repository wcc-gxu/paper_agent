#!/usr/bin/env python3
"""批量 PDF 入库脚本 — 扫描目录 → 转换 → 分块 → 向量索引.

Usage:
    python scripts/batch_import_pdfs.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from paper_search.agent.pgdb import PostgresAgentDB
from paper_search.agent.pgvector_store import PgVectorStore
from paper_search.agent.pdf_converter import PDFConverter
from paper_search.agent.chunker import SectionChunker

PDF_DIRS = [
    "/home/ubuntu/data_row/papers",
]

USER_ID = "user-default"
PROJECT_ID = "proj-batch-import-001"


def parse_filename(filepath: Path) -> dict:
    """从文件名提取元数据. 格式: Author_Year_Title.pdf"""
    stem = filepath.stem
    parts = stem.split("_", 2)
    if len(parts) >= 3:
        author = parts[0]
        try:
            year = int(parts[1])
        except ValueError:
            year = 0
        title = parts[2].replace("_", " ")
    else:
        author = ""
        year = 0
        title = stem.replace("_", " ")

    # 截断过长标题
    title = re.sub(r'\s+', ' ', title)[:500]
    return {"author": author, "year": year, "title": title, "source": "local_pdf"}


def make_paper_id(filepath: Path) -> str:
    """生成稳定 paper_id."""
    h = hashlib.md5(str(filepath).encode()).hexdigest()[:8]
    return f"local:{h}"


async def main():
    parser = argparse.ArgumentParser(description="批量 PDF 入库")
    parser.add_argument("--dry-run", action="store_true", help="不执行实际入库")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量")
    parser.add_argument("--start", type=int, default=0, help="起始偏移")
    args = parser.parse_args()

    db = PostgresAgentDB()
    vector_store = PgVectorStore()
    converter = PDFConverter(max_concurrent=4)
    chunker = SectionChunker()

    # 扫描所有 PDF
    pdfs = []
    for d in PDF_DIRS:
        dp = Path(d)
        if dp.exists():
            pdfs.extend(sorted(dp.rglob("*.pdf")))

    print(f"📂 扫描到 {len(pdfs)} 个 PDF 文件")

    if args.start:
        pdfs = pdfs[args.start:]
        print(f"   跳过前 {args.start} 个，剩余 {len(pdfs)}")

    if args.limit:
        pdfs = pdfs[:args.limit]
        print(f"   限制处理 {args.limit} 个")

    if args.dry_run:
        for p in pdfs[:20]:
            meta = parse_filename(p)
            print(f"   {p.name} → {meta['author']} ({meta['year']}) {meta['title'][:60]}...")
        if len(pdfs) > 20:
            print(f"   ... 还有 {len(pdfs) - 20} 个")
        return

    # 确保 project 存在
    try:
        project = db.get_project(PROJECT_ID)
        if not project:
            db.create_project(user_query="批量导入本地 PDF", project_id=PROJECT_ID)
            print(f"✅ 创建 project: {PROJECT_ID}")
    except Exception:
        db.create_project(user_query="批量导入本地 PDF", project_id=PROJECT_ID)
        print(f"✅ 创建 project: {PROJECT_ID}")

    successful = 0
    skipped = 0
    failed = 0
    t0 = time.monotonic()

    for i, pdf_path in enumerate(pdfs):
        meta = parse_filename(pdf_path)
        paper_id = make_paper_id(pdf_path)

        # 去重
        existing = db.get_paper(paper_id)
        if existing:
            skipped += 1
            if i % 50 == 0:
                print(f"   [{i+1}/{len(pdfs)}] ⏭ skip (dup) — {pdf_path.name[:60]}")
            continue

        try:
            # 1. 转换 PDF → Markdown
            md_content = await converter.convert(str(pdf_path))
            if not md_content:
                failed += 1
                print(f"   [{i+1}/{len(pdfs)}] ❌ convert failed: {pdf_path.name[:60]}")
                continue

            # 2. 入库 paper 元数据
            paper = {
                "paper_id": paper_id,
                "title": meta["title"],
                "authors": [meta["author"]],
                "year": meta["year"],
                "abstract": md_content[:2000] if md_content else "",
                "source": meta["source"],
                "local_path": str(pdf_path),
                "status": "active",
            }
            db.upsert_paper(paper, user_id=USER_ID)

            # 3. 关联到 project
            db.add_paper_to_project(PROJECT_ID, paper_id, USER_ID)

            # 4. 分块 + 向量索引
            chunks = chunker.chunk(md_content, paper_id)
            if chunks:
                vector_store.add_fulltext_chunks(chunks)

            # 5. 摘要向量索引
            vector_store.add_paper_abstract(
                paper_id=paper_id,
                title=meta["title"],
                abstract=meta.get("abstract", md_content[:2000]) if md_content else "",
            )

            successful += 1
            elapsed = time.monotonic() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            if i % 10 == 0:
                print(f"   [{i+1}/{len(pdfs)}] ✅ {pdf_path.name[:60]} "
                      f"(ok={successful} skip={skipped} fail={failed} "
                      f"{rate:.1f}/s)")

        except Exception as e:
            failed += 1
            if failed <= 5:  # 只打印前几个错误
                print(f"   [{i+1}/{len(pdfs)}] ❌ {pdf_path.name[:50]} — {e}")

    elapsed = time.monotonic() - t0
    total = len(pdfs)
    print(f"\n{'='*60}")
    print(f"📊 批量入库完成: {total} 文件, {elapsed:.1f}s")
    print(f"   ✅ 成功: {successful}")
    print(f"   ⏭  跳过 (重复): {skipped}")
    print(f"   ❌ 失败: {failed}")
    print(f"   📈 速率: {total/elapsed:.1f} 篇/秒")
    print(f"   📁 Project: {PROJECT_ID}")


if __name__ == "__main__":
    asyncio.run(main())
