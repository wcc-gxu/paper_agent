"""CLI 公共基础设施 — Rich 输出、DB 连接、共享参数."""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Rich 控制台（延迟导入，避免依赖缺失导致整个模块不可用）────

_console = None


def get_console():
    global _console
    if _console is None:
        try:
            from rich.console import Console
            _console = Console(stderr=True, highlight=False)
        except ImportError:
            _console = None
    return _console


def _rich_available():
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


# ── 输出辅助 ────────────────────────────────────────────────


def output_json(data: dict, exit_code: int = 0):
    """将 dict 以 JSON 格式写入 stdout 并退出。"""
    # stdout 是机器可读的 JSON
    json_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    print(json_str, flush=True)
    sys.exit(exit_code)


def output_error(message: str, exit_code: int = 1):
    """输出错误信息到 stderr 和 stdout（JSON）。"""
    output_json({"error": True, "message": str(message)}, exit_code=exit_code)


def console_print(*args, **kwargs):
    """Rich 美化输出到 stderr（人类可读），回退到 print。"""
    console = get_console()
    if console:
        console.print(*args, **kwargs)
    else:
        print(*args, file=sys.stderr, **kwargs)


def progress_spinner(description: str = "处理中"):
    """返回 Rich spinner 上下文管理器（如果可用），否则返回空上下文。"""
    if _rich_available():
        try:
            from rich.progress import Progress, SpinnerColumn, TextColumn

            class _SpinnerCtx:
                def __init__(self, parent_progress, task_id):
                    self._progress = parent_progress
                    self._task_id = task_id

                def update(self, description: str):
                    self._progress.update(self._task_id, description=description)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self._progress.stop()

            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                transient=True,
                console=get_console(),
            )
            progress.start()
            task_id = progress.add_task(description, total=None)
            return _SpinnerCtx(progress, task_id)
        except Exception:
            pass

    class _NoopCtx:
        def update(self, _desc):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return _NoopCtx()


def show_table(title: str, columns: list[str], rows: list[list[str]]):
    """显示 Rich 表格到 stderr。"""
    if _rich_available() and get_console():
        from rich.table import Table
        table = Table(title=title)
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*[str(c) for c in row])
        get_console().print(table)
    else:
        print(f"\n=== {title} ===", file=sys.stderr)
        print("  ".join(columns), file=sys.stderr)
        for row in rows:
            print("  ".join(str(c) for c in row), file=sys.stderr)
        print(file=sys.stderr)


def format_duration(seconds: float) -> str:
    """格式化耗时。"""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    else:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.0f}s"


# ── DB / Engine 连接工厂 ────────────────────────────────────


def create_db(db_path: Optional[Path] = None):
    """创建 AgentDB 实例。"""
    from ..agent.db import AgentDB
    return AgentDB(db_path)


def create_engine():
    """创建 PaperSearchEngine 实例。"""
    from ..config import Config
    from ..engine import PaperSearchEngine
    return PaperSearchEngine(Config())


def _load_all_providers():
    """导入所有 Provider 模块以触发注册。"""
    try:
        from ..providers import arxiv_provider  # noqa: F401
        from ..providers import semanticscholar_provider  # noqa: F401
        from ..providers import pubmed_provider  # noqa: F401
    except ImportError:
        pass
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


# ── 公共 argparse 参数 ──────────────────────────────────────


def add_project_id_arg(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--project-id", type=str, default=None,
        help="项目 ID（用于关联论文到项目，不提供则自动生成）",
    )


def add_output_dir_arg(parser: argparse.ArgumentParser, default: str = None):
    if default is None:
        from ..config import get_papers_dir
        default = str(get_papers_dir())
    parser.add_argument(
        "--output-dir", "-o", type=str, default=default,
        help=f"输出目录 (默认: {default})",
    )


def add_max_results_arg(parser: argparse.ArgumentParser, default: int = 20):
    parser.add_argument(
        "--max-results", "-n", type=int, default=default,
        help=f"最大结果数 (默认: {default})",
    )


def parse_sources(sources_str: Optional[str]):
    """解析逗号分隔的来源字符串为 SourceType 列表。"""
    from ..models import SourceType
    if not sources_str:
        return [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]
    return [SourceType(s.strip().lower()) for s in sources_str.split(",")]


# ── 平台编码修复 ────────────────────────────────────────────


def setup_windows_utf8():
    """修复 Windows 控制台 GBK 编码问题。"""
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )


# ── async main runner ───────────────────────────────────────


def run_async(main_coro):
    """统一的 async CLI 入口包装器。"""
    setup_windows_utf8()
    _load_all_providers()

    try:
        result = asyncio.run(main_coro)
        return result if isinstance(result, int) else 0
    except KeyboardInterrupt:
        console_print("\n[yellow][!] 用户中断[/yellow]")
        return 130
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        console_print(f"[red][ERROR] {e}[/red]")
        return 1
