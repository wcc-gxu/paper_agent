"""配置管理 — 从环境变量和配置文件加载设置.

Base 目录规则:
- Windows → D:/
- Linux/macOS → ~/ (用户目录)
- 可通过 PAPER_SEARCH_BASE_DIR 环境变量覆盖
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Base Directory
# ═══════════════════════════════════════════════════════════════


def get_base_dir() -> Path:
    """获取基础数据目录。

    优先级:
    1. PAPER_SEARCH_BASE_DIR 环境变量
    2. Windows → D:/
    3. Linux/macOS → ~/ (用户 home 目录)
    """
    env = os.environ.get("PAPER_SEARCH_BASE_DIR", "")
    if env:
        return Path(env)

    if sys.platform == "win32":
        return Path("D:/")
    else:
        return Path.home()


# 自动加载项目根目录的 .env 文件
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"
if _ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH)
    except ImportError:
        pass


@dataclass
class Config:
    """全局配置。优先从环境变量加载，可从 YAML 文件覆盖。"""

    # ── 存储 (全部从 base_dir 派生) ───────────────────────
    storage_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PAPER_SEARCH_STORAGE_DIR",
                           str(get_base_dir() / "papers"))
        )
    )

    # ── 下载 ──────────────────────────────────────────────
    download_timeout: int = int(os.environ.get("PAPER_SEARCH_DOWNLOAD_TIMEOUT", "60"))
    max_concurrent_downloads: int = int(
        os.environ.get("PAPER_SEARCH_MAX_CONCURRENT_DOWNLOADS", "4")
    )

    # ── API 密钥 ──────────────────────────────────────────
    semantic_scholar_api_key: str = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    ncbi_email: str = os.environ.get("NCBI_EMAIL", "")
    ieee_api_key: str = os.environ.get("IEEE_API_KEY", "")
    elsevier_api_key: str = os.environ.get("ELSEVIER_API_KEY", "")

    # ── 校园网 ────────────────────────────────────────────
    campus_ip: bool = os.environ.get("PAPER_SEARCH_CAMPUS_IP", "").lower() in (
        "1", "true", "yes"
    )
    ezproxy_url: str = os.environ.get("PAPER_SEARCH_EZPROXY_URL", "")

    # ── 搜索默认值 ────────────────────────────────────────
    default_max_results: int = int(
        os.environ.get("PAPER_SEARCH_DEFAULT_MAX_RESULTS", "20")
    )
    request_delay: float = float(
        os.environ.get("PAPER_SEARCH_REQUEST_DELAY", "0.5")
    )

    # ── Cookie 缓存 ───────────────────────────────────────
    cookie_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PAPER_SEARCH_COOKIE_DIR",
                           str(get_base_dir() / ".paper_search" / "cookies"))
        )
    )

    # ── 日志 ──────────────────────────────────────────────
    log_level: str = os.environ.get("PAPER_SEARCH_LOG_LEVEL", "INFO")

    @classmethod
    def from_yaml(cls, path: Optional[Path] = None) -> "Config":
        """从 YAML 配置文件加载（覆盖环境变量默认值）。

        配置文件优先级:
        1. path 参数指定的文件
        2. PAPER_SEARCH_CONFIG 环境变量
        3. {base_dir}/.paper_search/config.yaml
        """
        import yaml

        config_path: Optional[Path] = None
        if path:
            config_path = path
        elif env_path := os.environ.get("PAPER_SEARCH_CONFIG"):
            config_path = Path(env_path)
        else:
            default_path = get_base_dir() / ".paper_search" / "config.yaml"
            if default_path.exists():
                config_path = default_path

        if config_path and config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls(**{k: v for k, v in data.items() if v is not None})

        return cls()

    def ensure_dirs(self) -> None:
        """确保所有必要的目录存在。"""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_cache_dir.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# Path Utilities — 供其他模块使用
# ═══════════════════════════════════════════════════════════════


def get_data_dir() -> Path:
    """获取 .paper_search 数据目录 (DB, ChromaDB, cookies)。"""
    return get_base_dir() / ".paper_search"


def get_papers_dir() -> Path:
    """获取论文存储目录 (PDF, Markdown, outputs)。"""
    return get_base_dir() / "papers"


def get_db_path() -> Path:
    """获取 SQLite 数据库路径。"""
    return get_data_dir() / "agent.db"


def get_chroma_path() -> Path:
    """获取 ChromaDB 向量存储路径。"""
    return get_data_dir() / "chroma"


def get_markdown_dir() -> Path:
    """获取 Markdown 输出目录。"""
    return get_papers_dir() / "markdown"


def get_outputs_dir(project_id: str = "") -> Path:
    """获取项目输出目录。"""
    p = get_papers_dir() / "outputs"
    if project_id:
        p = p / project_id
    return p
