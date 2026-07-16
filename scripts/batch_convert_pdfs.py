#!/usr/bin/env python3
"""批量 PDF→Markdown 转换脚本 — 使用项目 PDFConverter.convert_with_figures().

特性:
- 公式解析 (pymupdf4llm 自动处理 LaTeX/公式区域)
- 图片提取并保存到本地 figures 目录
- 分批处理 (默认每批 20 篇)
- 断点续传 (跳过已转换的 MD)
- 批次验证 (检查 MD 长度 + 图片数量)
- 验证通过后删除原始 PDF
- 进度日志 (JSON 格式，可恢复)

用法:
    # 转换所有用户目录下的 PDF
    python scripts/batch_convert_pdfs.py --all

    # 转换指定目录
    python scripts/batch_convert_pdfs.py --dir /home/ubuntu/data_row/papers

    # 仅扫描预览（不转换）
    python scripts/batch_convert_pdfs.py --all --dry-run

    # 自定义批次大小和并发
    python scripts/batch_convert_pdfs.py --all --batch 10 --concurrent 3

输出:
    MD 文件: ~/papers/markdown/{paper_name}.md
    图片文件: ~/papers/figures/{paper_name}/*.png
    日志文件: ~/papers/markdown/.batch_convert_log.json
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 确保项目源码在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logger = logging.getLogger("batch_convert")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

EXCLUDE_DIRS = {
    "anaconda3", ".cache", ".claude", ".config", ".local",
    ".npm", ".paper_search", "__pycache__", "node_modules",
    ".git", "docs", "scripts", "backups",
}

EXCLUDE_PATTERNS = {
    "book-main.pdf",  # 项目文档，不是论文
}

MIN_MD_SIZE = 500       # MD 文件最小字节数（低于此值视为转换失败）
MIN_FIGURE_COUNT = 0     # 最低图片数（很多论文没有图片是正常的）
BATCH_SIZE = 20
MAX_CONCURRENT = 4
OUTPUT_BASE = Path.home() / "papers"
MD_DIR = OUTPUT_BASE / "markdown"
FIGURES_DIR = OUTPUT_BASE / "figures"
LOG_FILE = MD_DIR / ".batch_convert_log.json"


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_log() -> dict:
    """加载转换日志（断点续传）。"""
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "started_at": now_iso(),
        "updated_at": "",
        "total_pdfs": 0,
        "converted": 0,
        "failed": 0,
        "skipped": 0,
        "deleted": 0,
        "batches": [],
        "items": {},  # md5(pdf_path) → {status, md_path, figures_count, error}
    }


def save_log(log: dict):
    """保存转换日志。"""
    log["updated_at"] = now_iso()
    MD_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_pdfs(directories: list[str] = None, scan_all: bool = False) -> list[Path]:
    """扫描用户目录下的所有 PDF 论文文件（使用系统 find 命令，速度极快）。

    Returns:
        按文件大小排序的 PDF 路径列表（小文件优先）。
    """
    import subprocess

    search_roots = []
    if scan_all:
        # 不递归扫描整个 home — 仅扫描已知有 PDF 的目录
        search_roots = [
            str(Path.home() / "data_row" / "papers"),
            str(Path.home() / "papers"),
        ]
    elif directories:
        search_roots = list(directories)
    else:
        search_roots = [
            str(Path.home() / "data_row" / "papers"),
            str(Path.home() / "papers"),
        ]

    pdfs = []
    for root in search_roots:
        if not Path(root).exists():
            logger.warning(f"目录不存在: {root}")
            continue
        try:
            # 使用系统 find 命令（比 Python rglob 快 10x+）
            result = subprocess.run(
                ["find", root, "-type", "f", "-name", "*.pdf"],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                pdf_path = Path(line)
                parts = set(pdf_path.parts)
                # 排除系统目录
                if parts & EXCLUDE_DIRS:
                    continue
                # 排除特定文件
                if pdf_path.name in EXCLUDE_PATTERNS:
                    continue
                pdfs.append(pdf_path)
        except subprocess.TimeoutExpired:
            logger.warning(f"find 超时: {root}")
        except Exception as e:
            logger.warning(f"扫描失败 {root}: {e}")

    # 去重 + 按大小排序（小文件优先处理）
    seen = set()
    unique = []
    for p in pdfs:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(rp)

    # 按文件大小排序（只对已存在的文件 stat）
    try:
        unique.sort(key=lambda p: p.stat().st_size if p.exists() else 0)
    except Exception:
        pass

    return unique


def get_md_path(pdf_path: Path) -> Path:
    """获取对应的 MD 输出路径。"""
    return MD_DIR / f"{pdf_path.stem}.md"


def get_figures_dir(pdf_path: Path) -> Path:
    """获取对应的图片输出目录。"""
    safe_name = pdf_path.stem[:100].replace("/", "_").replace("\\", "_")
    return FIGURES_DIR / safe_name


def verify_conversion(pdf_path: Path, md_path: Path, figures: list[dict]) -> dict:
    """验证转换结果。

    Returns:
        {"ok": bool, "md_ok": bool, "md_size": int, "figures_ok": bool,
         "figures_count": int, "issues": [str]}
    """
    issues = []
    md_ok = False
    figures_ok = True

    # MD 验证
    md_size = 0
    if md_path.exists():
        md_size = md_path.stat().st_size
        if md_size >= MIN_MD_SIZE:
            md_ok = True
        else:
            issues.append(f"MD 文件太小: {md_size} bytes (min {MIN_MD_SIZE})")
    else:
        issues.append("MD 文件不存在")

    # 图片验证
    figures_count = len(figures)
    # 检查图片文件是否真的存在
    existing_figures = 0
    for fig in figures:
        lp = fig.get("local_path", "")
        if lp and Path(lp).exists():
            existing_figures += 1

    if existing_figures < figures_count:
        issues.append(f"图片丢失: {existing_figures}/{figures_count} 个图片文件存在")

    return {
        "ok": md_ok,
        "md_ok": md_ok,
        "md_size": md_size,
        "figures_ok": figures_ok if figures_count > 0 else None,
        "figures_count": existing_figures,
        "issues": issues,
    }


# ═══════════════════════════════════════════════════════════════
# Core
# ═══════════════════════════════════════════════════════════════

async def _convert_single(
    pdf_path: Path,
    pdf_key: str,
    log: dict,
    converter,
    batch_index: int,
    total_batch: int,
    dry_run: bool = False,
) -> dict:
    """转换单个 PDF（并发调用）。

    Returns:
        {"status": "converted"|"failed"|"skipped"|"dry_run", ...}
    """
    # 断点续传: 跳过已处理的
    if pdf_key in log.get("items", {}):
        prev = log["items"][pdf_key]
        if prev.get("status") == "converted":
            logger.info(f"[{batch_index}/{total_batch}] ⏭ 已转换，跳过: {pdf_path.name[:60]}")
            return {"status": "skipped", "pdf_path": str(pdf_path), "pdf_key": pdf_key}

    md_path = get_md_path(pdf_path)
    figures_subdir = get_figures_dir(pdf_path)

    # 检查是否已有有效 MD
    if md_path.exists() and md_path.stat().st_size >= MIN_MD_SIZE:
        logger.info(f"[{batch_index}/{total_batch}] ⏭ MD 已存在: {md_path.name}")
        return {
            "status": "skipped", "pdf_path": str(pdf_path), "pdf_key": pdf_key,
            "md_path": str(md_path),
        }

    if dry_run:
        logger.info(f"[{batch_index}/{total_batch}] 🔍 预览: {pdf_path.name[:80]}")
        return {"status": "dry_run", "pdf_path": str(pdf_path), "pdf_key": pdf_key}

    # ── 执行转换 ──
    logger.info(f"[{batch_index}/{total_batch}] 🔄 {pdf_path.name[:80]}")
    t0 = time.time()

    try:
        md_out, figures = await converter.convert_with_figures(
            pdf_path, MD_DIR, figures_subdir,
        )

        elapsed = time.time() - t0

        # 验证
        verification = verify_conversion(pdf_path, md_out, figures)

        if verification["ok"]:
            logger.info(
                f"  ✅ 完成 ({elapsed:.1f}s): "
                f"MD={verification['md_size']}B, "
                f"图片={verification['figures_count']}"
            )

            item = {
                "status": "converted",
                "pdf_path": str(pdf_path),
                "pdf_key": pdf_key,
                "md_path": str(md_out) if md_out else "",
                "md_size": verification["md_size"],
                "figures_count": verification["figures_count"],
                "elapsed_s": round(elapsed, 2),
                "verified": True,
                "deleted": False,
                "at": now_iso(),
            }

            # ── 验证通过 → 删除 PDF ──
            try:
                pdf_path.unlink()
                item["deleted"] = True
                logger.info(f"  🗑 已删除原始 PDF: {pdf_path.name}")
            except Exception as e:
                logger.warning(f"  ⚠️ 删除 PDF 失败: {e}")
                item["delete_error"] = str(e)

            return item

        else:
            logger.warning(
                f"  ❌ 验证失败 ({elapsed:.1f}s): "
                f"{'; '.join(verification['issues'])}"
            )
            return {
                "status": "failed_verify",
                "pdf_path": str(pdf_path),
                "pdf_key": pdf_key,
                "md_path": str(md_out) if md_out else "",
                "issues": verification["issues"],
                "elapsed_s": round(elapsed, 2),
                "at": now_iso(),
            }

    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"  💥 转换异常 ({elapsed:.1f}s): {e}")
        return {
            "status": "failed_error",
            "pdf_path": str(pdf_path),
            "pdf_key": pdf_key,
            "error": str(e),
            "elapsed_s": round(elapsed, 2),
            "at": now_iso(),
        }


async def convert_batch(
    pdf_paths: list[Path],
    log: dict,
    max_concurrent: int = MAX_CONCURRENT,
    dry_run: bool = False,
) -> dict:
    """并发转换一批 PDF（通过 asyncio.gather，受 PDFConverter semaphore 控制）。

    Returns:
        {converted: int, failed: int, skipped: int, deleted: int, batch_log: dict}
    """
    from paper_search.agent.pdf_converter import PDFConverter

    converter = PDFConverter(max_concurrent=max_concurrent)
    batch_log = {"started_at": now_iso(), "count": len(pdf_paths), "items": {}}

    # 预处理所有 PDF keys
    indexed = []
    for i, pdf_path in enumerate(pdf_paths):
        pdf_key = hashlib.md5(str(pdf_path.resolve()).encode()).hexdigest()
        indexed.append((pdf_path, pdf_key, i + 1))

    # 并发转换（PDFConverter 的 semaphore 控制实际并发度）
    tasks = [
        _convert_single(pdf_path, pdf_key, log, converter, idx, len(pdf_paths), dry_run)
        for pdf_path, pdf_key, idx in indexed
    ]
    results = await asyncio.gather(*tasks)

    # 汇总结果
    converted = 0
    failed = 0
    skipped = 0
    deleted = 0

    for item in results:
        status = item.get("status", "unknown")
        pdf_key = item.get("pdf_key", "")

        if status == "converted":
            converted += 1
            if item.get("deleted"):
                deleted += 1
        elif status == "failed_verify" or status == "failed_error":
            failed += 1
        elif status == "skipped" or status == "dry_run":
            skipped += 1

        if pdf_key and status not in ("dry_run",):
            log["items"][pdf_key] = item
            batch_log["items"][pdf_key] = item

    batch_log.update({
        "converted": converted,
        "failed": failed,
        "skipped": skipped,
        "deleted": deleted,
        "ended_at": now_iso(),
    })

    result = {
        "converted": converted,
        "failed": failed,
        "skipped": skipped,
        "deleted": deleted,
        "batch_log": batch_log,
    }

    # 更新全局日志
    log["converted"] = log.get("converted", 0) + converted
    log["failed"] = log.get("failed", 0) + failed
    log["skipped"] = log.get("skipped", 0) + skipped
    log["deleted"] = log.get("deleted", 0) + deleted
    log["total_pdfs"] = log.get("total_pdfs", 0)
    save_log(log)

    return result


async def run_batch_pipeline(
    pdf_paths: list[Path],
    batch_size: int = BATCH_SIZE,
    max_concurrent: int = MAX_CONCURRENT,
    dry_run: bool = False,
):
    """分批转换所有 PDF。"""
    log = load_log()
    log["total_pdfs"] = len(pdf_paths)
    save_log(log)

    total = len(pdf_paths)
    batches = [pdf_paths[i:i + batch_size] for i in range(0, total, batch_size)]

    logger.info(f"=" * 60)
    logger.info(f"批量 PDF→MD 转换")
    logger.info(f"  总 PDF 数: {total}")
    logger.info(f"  批次大小: {batch_size} (共 {len(batches)} 批)")
    logger.info(f"  最大并发: {max_concurrent}")
    logger.info(f"  输出目录: {MD_DIR}")
    logger.info(f"  图片目录: {FIGURES_DIR}")
    if dry_run:
        logger.info(f"  ⚠️ DRY RUN — 仅扫描不转换")
    logger.info(f"=" * 60)

    grand_total = {"converted": 0, "failed": 0, "skipped": 0, "deleted": 0}

    for batch_num, batch in enumerate(batches):
        logger.info(f"\n{'─' * 40}")
        logger.info(f"📦 批次 {batch_num + 1}/{len(batches)} ({len(batch)} 篇)")
        logger.info(f"{'─' * 40}")

        result = await convert_batch(batch, log, max_concurrent, dry_run)

        grand_total["converted"] += result["converted"]
        grand_total["failed"] += result["failed"]
        grand_total["skipped"] += result["skipped"]
        grand_total["deleted"] += result["deleted"]

        logger.info(
            f"📊 批次 {batch_num + 1} 完成: "
            f"转换={result['converted']}, "
            f"失败={result['failed']}, "
            f"跳过={result['skipped']}, "
            f"删除={result['deleted']}"
        )
        logger.info(
            f"📊 累计: "
            f"转换={grand_total['converted']}, "
            f"失败={grand_total['failed']}, "
            f"跳过={grand_total['skipped']}, "
            f"删除={grand_total['deleted']}"
        )

        if not dry_run:
            save_log(log)

    # ── 最终报告 ──
    logger.info(f"\n{'=' * 60}")
    logger.info(f"🏁 全部批次完成")
    logger.info(f"  转换成功: {grand_total['converted']}")
    logger.info(f"  转换失败: {grand_total['failed']}")
    logger.info(f"  跳过(已存在): {grand_total['skipped']}")
    logger.info(f"  原始 PDF 已删除: {grand_total['deleted']}")
    logger.info(f"  剩余 PDF 未处理: {grand_total['failed']}")
    logger.info(f"  日志文件: {LOG_FILE}")
    logger.info(f"{'=' * 60}")

    return grand_total


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="批量 PDF→Markdown 转换 (公式+图片)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/batch_convert_pdfs.py --all
  python scripts/batch_convert_pdfs.py --dir /home/ubuntu/data_row/papers
  python scripts/batch_convert_pdfs.py --all --dry-run
  python scripts/batch_convert_pdfs.py --all --batch 10 --concurrent 2
        """,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="扫描所有用户目录下的 PDF")
    group.add_argument("--dir", type=str, nargs="+", help="指定 PDF 目录（可多个）")

    parser.add_argument("--batch", type=int, default=BATCH_SIZE, help=f"批次大小 (默认 {BATCH_SIZE})")
    parser.add_argument("--concurrent", type=int, default=MAX_CONCURRENT,
                        help=f"最大并发转换数 (默认 {MAX_CONCURRENT})")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描预览，不实际转换")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")

    args = parser.parse_args()

    # 设置日志
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)

    # 扫描 PDF
    if args.all:
        pdf_paths = scan_pdfs(scan_all=True)
    else:
        pdf_paths = scan_pdfs(directories=args.dir)

    if not pdf_paths:
        logger.error("未找到任何 PDF 文件")
        sys.exit(1)

    logger.info(f"扫描完成: 发现 {len(pdf_paths)} 个 PDF 文件")

    # 显示分布
    dir_counts = {}
    total_size = 0
    for p in pdf_paths:
        parent = str(p.parent)
        dir_counts[parent] = dir_counts.get(parent, 0) + 1
        try:
            total_size += p.stat().st_size
        except Exception:
            pass
    logger.info("PDF 分布:")
    for d, c in sorted(dir_counts.items()):
        logger.info(f"  {d}: {c} 篇")
    logger.info(f"总大小: {total_size / (1024**3):.2f} GB")

    if args.dry_run:
        logger.info("\n🔍 DRY RUN — 仅扫描，不转换。取消 --dry-run 以开始转换。")
        return

    # 执行
    asyncio.run(run_batch_pipeline(
        pdf_paths,
        batch_size=args.batch,
        max_concurrent=args.concurrent,
        dry_run=False,
    ))


if __name__ == "__main__":
    main()
