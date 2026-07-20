"""持久化日志配置 — stdout + RotatingFileHandler，容器重建不丢失。

用法:
    from paper_search.logging_setup import setup_file_logging
    setup_file_logging("supervisor")

环境变量:
    LOGS_DIR    日志目录（默认 /var/log/paper_agent）
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 10 * 1024 * 1024  # 10MB 轮转
_BACKUP_COUNT = 5               # 保留 5 个历史文件


def setup_file_logging(name: str, logs_dir: str | None = None):
    """为当前进程添加 RotatingFileHandler，日志同时写入 stdout 和文件。

    幂等：多次调用同名不会重复添加 handler。
    """
    logs_dir = logs_dir or os.getenv("LOGS_DIR", "/var/log/paper_agent")
    os.makedirs(logs_dir, exist_ok=True)

    filepath = os.path.join(logs_dir, f"{name}.log")
    root = logging.getLogger()

    # 幂等检查
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and h.baseFilename == os.path.abspath(filepath):
            return

    handler = RotatingFileHandler(
        filepath, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)

    logging.getLogger(__name__).info("File logging enabled: %s", filepath)
