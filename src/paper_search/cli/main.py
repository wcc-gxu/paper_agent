"""CLI 入口 — 学术论文搜索与 PDF 下载命令行工具.

用法:
    paper-search search "transformer" --sources arxiv,semantic_scholar
    paper-search search --title "Attention Is All You Need" --sources arxiv
    paper-search download "Attention Is All You Need" --source arxiv
    paper-search batch queries.json
    paper-search list-sources
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from ..config import Config
from ..engine import PaperSearchEngine
from ..models import SearchQuery, SourceType

logger = logging.getLogger(__name__)


def _setup_logging(level: str = "INFO"):
    """配置基础日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ─── 子命令处理函数 ──────────────────────────────────────


async def cmd_search(args):
    """执行搜索子命令。"""
    engine = PaperSearchEngine(Config())

    # 构建 SearchQuery
    sources = _parse_sources(args.sources)
    query = SearchQuery(
        keywords=args.keywords or "",
        title=args.title,
        author=args.author,
        doi=args.doi,
        year_from=args.year_from,
        year_to=args.year_to,
        max_results=args.max_results,
        sources=sources,
    )

    print(f"[搜索] {query.effective_query()}")
    print(f"   来源: {[s.value for s in sources]}")
    print()

    result = await engine.search(query)

    # 输出结果
    if args.format == "json":
        # JSON 输出
        output = {
            "query": query.model_dump(mode="json"),
            "total_found": result.total_found,
            "errors": result.errors,
            "papers": [_paper_to_dict(p) for p in result.papers],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # 文本表格输出
        if result.errors:
            for err in result.errors:
                print(f"  [!] {err}")
            print()

        print(f"  找到 {result.total_found} 篇论文:\n")
        for i, paper in enumerate(result.papers, 1):
            _print_paper(i, paper)

    # 如果有 --download 标记，批量下载
    if args.download and result.papers:
        print(f"\n[下载] 开始下载 {len(result.papers)} 篇论文...")
        dl_results = await engine.download_many(result.papers)
        success = sum(1 for d in dl_results if d.success)
        failed = sum(1 for d in dl_results if not d.success)
        print(f"  下载完成: {success} 成功, {failed} 失败")
        for d in dl_results:
            if d.success:
                print(f"    [OK] {d.local_path}")
            else:
                print(f"    [FAIL] {d.paper.title[:60]} - {d.error}")

    await engine.close()


async def cmd_download(args):
    """执行下载子命令。"""
    from ..models import Paper

    engine = PaperSearchEngine(Config())

    source = SourceType(args.source)
    output_dir = Path(args.output).expanduser().resolve()

    # 先搜索定位论文
    query = SearchQuery(
        title=args.title,
        doi=args.doi,
        keywords=args.title or "",
        sources=[source],
        max_results=5,
    )
    result = await engine.search(query)

    if not result.papers:
        print(f"[ERROR] 未找到论文: {args.title or args.doi}")
        await engine.close()
        return 1

    # 如果有多篇匹配，取第一篇或让用户选择
    if len(result.papers) > 1 and not args.doi:
        print(f"  找到 {len(result.papers)} 篇匹配论文，使用第一篇:")
        _print_paper(1, result.papers[0])

    paper = result.papers[0]
    print(f"[下载] {paper.title[:80]}")
    dl_result = await engine.download(paper, target_dir=output_dir)

    if dl_result.success:
        print(f"  [OK] {dl_result.local_path}")
    else:
        print(f"  [FAIL] 下载失败: {dl_result.error}")
        await engine.close()
        return 1

    await engine.close()
    return 0


async def cmd_batch(args):
    """执行批量搜索子命令。"""
    engine = PaperSearchEngine(Config())

    default_sources = _parse_sources(args.sources) if args.sources else None
    print(f"[文件] 读取查询文件: {args.file}")
    print(f"   默认来源: {[s.value for s in default_sources] if default_sources else '文件中指定'}")
    print()

    try:
        summary = await engine.batch_search_from_file(
            args.file,
            download=args.download,
            default_sources=default_sources,
        )
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        await engine.close()
        return 1

    # 输出汇总
    print(f"[完成] 批量搜索完成:")
    print(f"   查询数: {summary.total_queries}")
    print(f"   找到论文: {summary.total_papers_found}")
    if args.download:
        print(f"   下载成功: {summary.total_downloaded}")
        print(f"   下载失败: {summary.total_failed}")

    # 保存结果
    output_file = args.output or args.file.replace(".json", "_results.json").replace(".csv", "_results.json")
    output = {
        "total_queries": summary.total_queries,
        "total_papers_found": summary.total_papers_found,
        "total_downloaded": summary.total_downloaded,
        "total_failed": summary.total_failed,
        "results": [
            {
                "query": r.query.model_dump(mode="json"),
                "total_found": r.total_found,
                "errors": r.errors,
                "papers": [_paper_to_dict(p) for p in r.papers],
            }
            for r in summary.results
        ],
        "downloads": [
            {
                "title": d.paper.title,
                "source": d.paper.source.value,
                "local_path": d.local_path,
                "success": d.success,
                "error": d.error,
            }
            for d in summary.downloads
        ],
    }
    Path(output_file).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已保存到: {output_file}")

    await engine.close()
    return 0


async def cmd_list_sources(args):
    """列出所有可用来源及其状态。"""
    from ..providers import list_providers

    engine = PaperSearchEngine(Config())

    print("可用文献来源:\n")
    print(f"{'来源':<20} {'状态':<10} {'类型':<10} {'描述'}")
    print("-" * 70)

    # 加载所有 provider 以触发注册
    _load_all_providers()

    health = await engine.health_check()

    source_info = {
        SourceType.ARXIV: ("免费 API", "预印本论文 (CS/AI/数学/物理等)"),
        SourceType.SEMANTIC_SCHOLAR: ("免费 API", "跨学科学术搜索引擎"),
        SourceType.PUBMED: ("免费 API", "生物医学文献数据库"),
        SourceType.CNKI: ("校内IP", "中国知网 — 中文学术论文/学位论文"),
        SourceType.IEEE: ("校内IP", "IEEE Xplore — 电子/计算机工程"),
        SourceType.SCIENCEDIRECT: ("校内IP", "Elsevier ScienceDirect"),
    }

    for st in list_providers():
        info = source_info.get(st, ("未知", ""))
        status = "[OK] 可用" if health.get(st.value, False) else "[--] 不可用"
        print(f"  {st.value:<18} {status:<10} {info[0]:<10} {info[1]}")

    await engine.close()


# ─── 辅助函数 ────────────────────────────────────────────


def _parse_sources(sources_str: Optional[str]) -> list[SourceType]:
    """解析逗号分隔的来源字符串。"""
    if not sources_str:
        return [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]
    return [SourceType(s.strip().lower()) for s in sources_str.split(",")]


def _paper_to_dict(paper) -> dict:
    """将 Paper 转为 JSON 友好的 dict。"""
    return {
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "abstract": paper.abstract[:300] + "..." if paper.abstract and len(paper.abstract) > 300 else paper.abstract,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "source": paper.source.value,
        "source_url": paper.source_url,
        "pdf_url": paper.pdf_url,
        "citation_count": paper.citation_count,
        "venue": paper.venue,
    }


def _print_paper(index: int, paper):
    """格式化打印一篇论文。"""
    title = paper.title[:100]
    if len(paper.title) > 100:
        title += "..."
    authors = ", ".join(paper.authors[:4])
    if len(paper.authors) > 4:
        authors += f" et al."
    year = str(paper.year) if paper.year else "----"
    source = paper.source.value
    doi = paper.doi or ""
    pdf_marker = " [PDF]" if paper.pdf_url else ""

    print(f"  [{index}] {title}{pdf_marker}")
    print(f"      {authors} | {year} | {source}")
    if doi:
        print(f"      DOI: {doi}")
    if paper.source_url:
        print(f"      URL: {paper.source_url}")
    print()


def _load_all_providers():
    """导入所有 Provider 模块以触发 @register 装饰器。"""
    try:
        from ..providers import arxiv_provider  # noqa: F401
        from ..providers import semanticscholar_provider  # noqa: F401
        from ..providers import pubmed_provider  # noqa: F401
    except ImportError:
        pass
    # 机构数据库可能不在当前环境可用，静默跳过
    try:
        from ..providers import cnki_provider  # noqa: F401
    except ImportError:
        pass
    try:
        from ..providers import ieee_provider  # noqa: F401
    except ImportError:
        pass
    try:
        from ..providers import sciencedirect_provider  # noqa: F401
    except ImportError:
        pass


# ─── 命令行参数解析 ──────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        description="学术论文搜索与 PDF 下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 关键词搜索
  paper-search search "transformer attention" --sources arxiv,semantic_scholar

  # 按标题搜索
  paper-search search --title "Attention Is All You Need" --sources arxiv

  # 搜索并下载
  paper-search search "adversarial attack" --sources arxiv --download

  # 按 DOI 下载
  paper-search download --doi "10.1000/xyz123" --source semantic_scholar

  # 批量搜索
  paper-search batch queries.json --download

  # 列出可用来源
  paper-search list-sources
""",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # ── search ──────────────────────────────────────────
    p_search = sub.add_parser("search", help="搜索论文")
    p_search.add_argument("keywords", nargs="?", default="", help="搜索关键词")
    p_search.add_argument("--title", "-t", default=None, help="按标题精确搜索")
    p_search.add_argument("--author", "-a", default=None, help="按作者筛选")
    p_search.add_argument("--doi", "-d", default=None, help="按 DOI 查找")
    p_search.add_argument("--year-from", type=int, default=None, help="起始年份")
    p_search.add_argument("--year-to", type=int, default=None, help="截止年份")
    p_search.add_argument(
        "--sources", "-s",
        default="arxiv,semantic_scholar",
        help="来源，逗号分隔 (默认: arxiv,semantic_scholar)",
    )
    p_search.add_argument(
        "--max-results", "-n",
        type=int, default=20,
        help="每个来源最大结果数 (默认: 20, 最大: 100)",
    )
    p_search.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="输出格式 (默认: text)",
    )
    p_search.add_argument(
        "--download", action="store_true",
        help="同时下载所有查询到的论文 PDF",
    )
    p_search.set_defaults(func=cmd_search)

    # ── download ────────────────────────────────────────
    p_dl = sub.add_parser("download", help="下载论文 PDF")
    p_dl.add_argument("title", nargs="?", default="", help="论文标题")
    p_dl.add_argument("--doi", "-d", default=None, help="论文 DOI")
    p_dl.add_argument(
        "--source", "-s",
        required=True,
        help="来源 (arxiv, semantic_scholar, pubmed, cnki, ieee, sciencedirect)",
    )
    from ..config import get_papers_dir
    p_dl.add_argument(
        "--output", "-o",
        default=str(get_papers_dir()),
        help=f"下载目录 (默认: {get_papers_dir()})",
    )
    p_dl.set_defaults(func=cmd_download)

    # ── batch ───────────────────────────────────────────
    p_batch = sub.add_parser("batch", help="从文件批量搜索")
    p_batch.add_argument("file", help="查询文件路径 (.json 或 .csv)")
    p_batch.add_argument(
        "--sources", "-s",
        default=None,
        help="默认来源（文件未指定时使用）",
    )
    p_batch.add_argument(
        "--download", action="store_true",
        help="同时下载所有论文 PDF",
    )
    p_batch.add_argument(
        "--output", "-o",
        default=None,
        help="结果输出文件 (默认: 原文件名_results.json)",
    )
    p_batch.set_defaults(func=cmd_batch)

    # ── list-sources ────────────────────────────────────
    p_list = sub.add_parser("list-sources", help="列出可用文献来源")
    p_list.set_defaults(func=cmd_list_sources)

    return parser


def main():
    """CLI 主入口。"""
    # 修复 Windows GBK 编码问题：强制 stdout 使用 UTF-8
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    _setup_logging()

    # 确保所有 Provider 已注册
    _load_all_providers()

    # 执行对应的 async 子命令
    try:
        result = asyncio.run(args.func(args))
        return result if isinstance(result, int) else 0
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
        return 130
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        print(f"[ERROR] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
