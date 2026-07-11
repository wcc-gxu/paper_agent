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
        get_videos_dir().mkdir(parents=True, exist_ok=True)
        get_cookie_dir().mkdir(parents=True, exist_ok=True)


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


# ═══════════════════════════════════════════════════════════════
# PostgreSQL 连接 (v3 Phase 1)
# ═══════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get("DATABASE_URL", "")
"""PostgreSQL 连接字符串。设置后自动切换到 PostgreSQL 后端。

格式: postgresql://user:password@host:5432/database
未设置或为空时使用 SQLite 后端（向后兼容）。
"""

PGVECTOR_URL = os.environ.get("PGVECTOR_URL", "") or DATABASE_URL
"""pgvector 专用连接字符串。默认与 DATABASE_URL 相同。"""


def use_postgresql() -> bool:
    """判断是否启用 PostgreSQL 后端。"""
    return bool(DATABASE_URL)


def get_database_url() -> str:
    """获取当前数据库连接字符串。"""
    return DATABASE_URL


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


def get_videos_dir() -> Path:
    """获取视频存储目录 (~/.paper_search/videos/)。

    Directory layout:
      ~/.paper_search/videos/
        ├── {video_id}.mp4              # 下载的视频文件
        ├── {video_id}.wav              # 提取的音频文件
        └── {video_id}_transcript.txt   # 转录文本
    """
    return get_data_dir() / "videos"


def get_cookie_dir() -> Path:
    """获取浏览器 cookie 缓存目录 (~/.paper_search/cookies/)。

    CloakBrowser 导出的 Netscape 格式 cookie 文件存放于此。
    各平台独立文件: douyin_cookies.txt, bilibili_cookies.txt 等。
    """
    return get_data_dir() / "cookies"


# ═══════════════════════════════════════════════════════════════
# Model Routing — 多模型路由表 (火山引擎多模型, 同一 VOLCANO_API_KEY)
# ═══════════════════════════════════════════════════════════════
#
# 7 个模型全部走火山引擎同一个 API Key, 只是 model ID 不同。
# 每个 Agent 节点配 (primary, fallback): 主模型失败(异常/超时/限流/JSON
# 校验失败)时 LLMClientV2 自动切 fallback 重试一次。
# 详见 llm_client_v2.LLMClientV2.chat / chat_json / chat_stream 的 node 参数。

MODEL_ROUTES: dict[str, tuple[str, str]] = {
    # ── 主 Agent 节点 ──────────────────────────────────────
    "safety_filter":         ("doubao-seed-2.0-mini", "doubao-seed-2.0-lite"),
    "safety_llm_confirm":    ("doubao-seed-2.0-mini", "doubao-seed-2.0-lite"),  # v2: 安全异步并行二次确认
    "fast_triage":           ("doubao-seed-2.0-mini", "doubao-seed-2.0-lite"),
    "inline_reply":          ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "lightweight_plan_ops":  ("doubao-seed-2.0-code", "glm-5.2"),
    "lightweight_plan_meta": ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "scenario_plan":         ("glm-5.2",              "deepseek-v4-pro"),
    "evaluate_completion":   ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "final_reply":           ("glm-5.2",              "deepseek-v4-pro"),
    # ── 记忆系统（Phase 2 三件套）─────────────────────────
    "summary":               ("doubao-seed-2.0-lite", "deepseek-v4-flash"),  # 档 2 滚动摘要
    "extract_long_term":     ("glm-5.2",              "deepseek-v4-pro"),    # 档 3 长期抽取
    "topic_consolidate":     ("doubao-seed-2.0-lite", "deepseek-v4-flash"),  # topic 粗粒度合并
    # ── 子 Agent 内部 ──────────────────────────────────────
    "ingest_evaluate":   ("doubao-seed-2.0-mini", "doubao-seed-2.0-lite"),
    "ingest_survey":     ("doubao-seed-2.0-pro",  "glm-5.2"),
    "cluster_label":     ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "gap_discovery":     ("glm-5.2",              "deepseek-v4-pro"),
    "citation_filter":   ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "translation":       ("glm-5.2",              "doubao-seed-2.0-pro"),
    "video_summarize":   ("doubao-seed-2.0-pro",  "glm-5.2"),
    "video_analyze":     ("glm-5.2",              "deepseek-v4-pro"),
    "rad_query_route":   ("doubao-seed-2.0-lite", "deepseek-v4-flash"),
    "rad_query_answer":  ("doubao-seed-2.0-pro",  "glm-5.2"),
}

# 默认模型 (向后兼容: 未传 node 时, LLMClientV2 用 provider 自带 model)
DEFAULT_MODEL_PRIMARY: str = "doubao-seed-2.0-lite"
DEFAULT_MODEL_FALLBACK: str = "deepseek-v4-flash"


def get_model_for_node(node_name: str) -> tuple[str, str]:
    """返回 (primary, fallback) 模型 ID。

    未知节点返回默认 (doubao-seed-2.0-lite, deepseek-v4-flash)。
    所有模型均走火山引擎 (同一 VOLCANO_API_KEY), 只是 model ID 不同,
    一个 SDK 即可切换, provider / base_url / api_key 不变。
    """
    return MODEL_ROUTES.get(
        node_name, (DEFAULT_MODEL_PRIMARY, DEFAULT_MODEL_FALLBACK)
    )
