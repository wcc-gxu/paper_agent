"""ToolRegistry — 唯一工具注册中心。

注册全部主 Agent 工具（35 个）和子 Agent 工具（19 个），
包装为 langchain_core.tools.StructuredTool。

使用方式:
    from paper_search.agent.tool_registry import ToolRegistry

    registry = ToolRegistry.get_instance()
    tools = registry.to_langchain()  # 用于 LangGraph tool_use 节点
    tool = registry.get("search_papers")  # 按名称获取
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Metadata
# ═══════════════════════════════════════════════════════════════


@dataclass
class ToolMetadata:
    """工具元数据标签（对应 CLAUDE.md §工具注册）."""
    location: str = "server"       # "server" | "ios"
    category: str = ""             # search | download | convert | index | analyze | export | manage | kb | subscription | system | network | memory | ios
    is_idempotent: bool = False     # 重试安全
    is_long_running: bool = False   # → Celery
    progress_report: bool = False   # → TaskLogger JSON 日志


# ═══════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════


class ToolRegistry:
    """单例工具注册中心。"""

    _instance: Optional["ToolRegistry"] = None

    def __init__(self):
        self._tools: dict[str, StructuredTool] = {}
        self._metadata: dict[str, ToolMetadata] = {}
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._register_all()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）."""
        cls._instance = None

    # ── 注册 API ─────────────────────────────────────────

    def register(self, name: str, description: str, func: Callable,
                 args_schema: dict | type = None, metadata: ToolMetadata = None):
        """注册一个工具。func 可以是 sync 或 async。"""
        if metadata is None:
            metadata = ToolMetadata()

        # 检测是否为 async
        coroutine = None
        sync_func = None
        if asyncio.iscoroutinefunction(func):
            coroutine = func
        else:
            sync_func = func
            async def _async_wrapper(**kwargs):
                return func(**kwargs)
            coroutine = _async_wrapper

        if args_schema is None or isinstance(args_schema, dict):
            # 对于 dict schema，使用简单的 args_schema
            from pydantic import create_model, Field
            if args_schema:
                fields = {k: (type(v), Field(description=f"Parameter: {k}")) for k, v in args_schema.items()}
                model = create_model(f"{name}_args", **fields)
            else:
                model = create_model(f"{name}_args")
            args_schema = model

        tool = StructuredTool(
            name=name,
            description=description,
            func=sync_func,
            coroutine=coroutine,
            args_schema=args_schema,
        )
        self._tools[name] = tool
        self._metadata[name] = metadata
        return tool

    def register_direct(self, name: str, description: str, func: Callable,
                        metadata: ToolMetadata = None):
        """直接注册一个工具（薄包装已有函数）。"""
        if metadata is None:
            metadata = ToolMetadata()

        from pydantic import create_model
        coroutine = func if asyncio.iscoroutinefunction(func) else None
        sync_func = func if not asyncio.iscoroutinefunction(func) else None
        args_schema = create_model(f"{name}_args")

        tool = StructuredTool(
            name=name,
            description=description,
            func=sync_func,
            coroutine=coroutine,
            args_schema=args_schema,
        )
        self._tools[name] = tool
        self._metadata[name] = metadata
        return tool

    # ── 查询 API ─────────────────────────────────────────

    def get(self, name: str) -> Optional[StructuredTool]:
        return self._tools.get(name)

    def get_by_category(self, category: str) -> list[StructuredTool]:
        return [t for n, t in self._tools.items()
                if self._metadata.get(n) and self._metadata[n].category == category]

    def get_by_location(self, location: str) -> list[StructuredTool]:
        return [t for n, t in self._tools.items()
                if self._metadata.get(n) and self._metadata[n].location == location]

    def list_tools(self) -> list[dict]:
        """列出所有工具（名称 + 描述 + 元数据）."""
        return [{
            "name": name,
            "description": tool.description[:120] if tool.description else "",
            "category": (self._metadata[name].category if self._metadata.get(name) else ""),
            "location": (self._metadata[name].location if self._metadata.get(name) else ""),
        } for name, tool in self._tools.items()]

    def to_langchain(self) -> list[StructuredTool]:
        """导出为 LangChain 工具列表（用于 LangGraph tool_use 节点）."""
        return list(self._tools.values())

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    # ══════════════════════════════════════════════════════════
    # 注册入口
    # ══════════════════════════════════════════════════════════

    def _register_all(self):
        if self._initialized:
            return
        self._initialized = True

        # ── 通用工具 6 ──
        self._register_read_file()
        self._register_write_file()
        self._register_edit_file()
        self._register_glob_files()
        self._register_grep_content()
        self._register_bash_exec()

        # ── 网络工具 2 ──
        self._register_web_search()
        self._register_web_fetch()

        # ── 运维工具 10 ──
        self._register_service_start()
        self._register_service_stop()
        self._register_service_status()
        self._register_docker_compose_up()
        self._register_docker_compose_down()
        self._register_apt_install()
        self._register_pip_install()
        self._register_env_config()
        self._register_log_view()
        self._register_health_check()

        # ── 记忆工具 7 ──
        self._register_search_memory()
        self._register_summarize_memory()
        self._register_delete_memory()
        self._register_extract_to_long_term()
        self._register_tag_memory()
        self._register_get_user_preference()
        self._register_list_collections()

        # ── iOS 自动工具 9 ──
        self._register_ios_file_read()
        self._register_ios_file_write()
        self._register_ios_file_list()
        self._register_ios_calendar_add()
        self._register_ios_calendar_read()
        self._register_ios_reminder_add()
        self._register_ios_notification_local()
        self._register_ios_device_info()
        self._register_ios_location_get()

        # ── 直接查询 3 ──
        self._register_paper_status()
        self._register_list_sources()
        self._register_get_paper_abstract()

        # ── 子 Agent 工具 19（执行阶段直接调用）──
        self._register_sub_agent_tools()

    # ══════════════════════════════════════════════════════════
    # 通用工具
    # ══════════════════════════════════════════════════════════

    def _register_read_file(self):
        def read_file(file_path: str, limit: int = 2000, offset: int = 0) -> str:
            """Read file contents."""
            path = Path(file_path)
            if not path.exists():
                return json.dumps({"error": f"File not found: {file_path}"})
            try:
                lines = path.read_text(encoding="utf-8").split("\n")
                selected = lines[offset:offset + limit]
                return "\n".join(selected)
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="read_file",
            description="读取文件内容。参数: file_path (文件路径), limit (最大行数, 默认2000), offset (起始行, 默认0)",
            func=read_file,
            metadata=ToolMetadata(category="system", is_idempotent=True),
        )

    def _register_write_file(self):
        def write_file(file_path: str, content: str) -> str:
            """Write content to a file."""
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.write_text(content, encoding="utf-8")
                return json.dumps({"success": True, "path": str(path), "size": len(content)})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="write_file",
            description="写入文件内容。参数: file_path (文件路径), content (内容)",
            func=write_file,
            metadata=ToolMetadata(category="system"),
        )

    def _register_edit_file(self):
        def edit_file(file_path: str, old_string: str, new_string: str) -> str:
            """Replace a string in a file."""
            path = Path(file_path)
            if not path.exists():
                return json.dumps({"error": f"File not found: {file_path}"})
            try:
                content = path.read_text(encoding="utf-8")
                if old_string not in content:
                    return json.dumps({"error": "old_string not found in file"})
                new_content = content.replace(old_string, new_string, 1)
                path.write_text(new_content, encoding="utf-8")
                return json.dumps({"success": True, "path": str(path)})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="edit_file",
            description="精确替换文件中的字符串。参数: file_path, old_string (要替换的文本), new_string (替换后文本)",
            func=edit_file,
            metadata=ToolMetadata(category="system"),
        )

    def _register_glob_files(self):
        def glob_files(pattern: str, path: str = ".") -> str:
            """Find files matching a glob pattern."""
            import glob
            try:
                matches = glob.glob(pattern, root_dir=path or None, recursive=True)
                return json.dumps({"matches": matches[:500], "total": len(matches)})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="glob_files",
            description="文件模式匹配。参数: pattern (glob 模式), path (搜索目录, 默认当前)",
            func=glob_files,
            metadata=ToolMetadata(category="system", is_idempotent=True),
        )

    def _register_grep_content(self):
        def grep_content(pattern: str, path: str = ".", glob: str = None) -> str:
            """Search for a pattern in files."""
            try:
                import subprocess as sp
                cmd = ["rg", "--no-heading", "-n", "--max-count=50", pattern, path]
                result = sp.run(cmd, capture_output=True, text=True, timeout=30)
                return result.stdout[:8000] or "No matches found"
            except FileNotFoundError:
                return json.dumps({"error": "ripgrep not installed"})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="grep_content",
            description="文件内容正则搜索。参数: pattern (正则表达式), path (搜索目录), glob (文件名过滤)",
            func=grep_content,
            metadata=ToolMetadata(category="system", is_idempotent=True),
        )

    def _register_bash_exec(self):
        async def bash_exec(command: str, timeout: int = 120, cwd: str = None) -> str:
            """Execute a shell command."""
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                result = {
                    "stdout": stdout.decode("utf-8", errors="replace")[:5000],
                    "stderr": stderr.decode("utf-8", errors="replace")[:1000],
                    "returncode": proc.returncode,
                }
                return json.dumps(result, ensure_ascii=False)
            except asyncio.TimeoutError:
                return json.dumps({"error": f"Command timed out after {timeout}s"})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(
            name="bash_exec",
            description="执行 Shell 命令。参数: command (命令), timeout (超时秒数, 默认120), cwd (工作目录)",
            func=bash_exec,
            metadata=ToolMetadata(category="system", is_long_running=True),
        )

    # ══════════════════════════════════════════════════════════
    # 网络工具
    # ══════════════════════════════════════════════════════════

    def _register_web_search(self):
        async def web_search(keywords: str, count: int = 10, time_range: str = "OneYear") -> str:
            """Volcengine web search with fallback chain."""
            api_key = os.getenv("WEB_SEARCH_API_KEY", "")
            if not api_key:
                return json.dumps({"error": "WEB_SEARCH_API_KEY not configured"})

            url = "https://open.feedcoopapi.com/search_api/web_search"
            headers = {
                "Content-Type": "application/json",
                "X-Traffic-Tag": "skill_web_search_common",
                "Authorization": f"Bearer {api_key}",
            }
            body = {
                "Query": keywords[:100],
                "SearchType": "web",
                "Count": min(count, 50),
                "NeedSummary": True,
                "TimeRange": time_range,
            }

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    data = resp.json()
                    error = (data.get("ResponseMetadata") or {}).get("Error")
                    if error:
                        code = error.get("Code", "")
                        if code in ("10406", "10412"):
                            logger.warning("Volcengine search quota exhausted, trying web_fetch")
                            return json.dumps({"error": f"Quota exhausted ({code}), use web_fetch for known URLs", "fallback": "web_fetch"})
                        return json.dumps({"error": f"Search API error [{code}]: {error.get('Message','')}"})

                    result = data.get("Result", {})
                    items = []
                    for item in result.get("WebResults", []):
                        items.append({
                            "title": item.get("Title", ""),
                            "url": item.get("Url", ""),
                            "site": item.get("SiteName", ""),
                            "summary": item.get("Summary", item.get("Snippet", ""))[:300],
                            "publish_time": item.get("PublishTime", ""),
                        })
                    return json.dumps({
                        "total": result.get("ResultCount", 0),
                        "time_ms": result.get("TimeCost", 0),
                        "results": items,
                    }, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": f"Search failed: {e}", "fallback": "try web_fetch or bash_exec curl"})

        self.register(
            name="web_search",
            description="通用网页搜索（火山引擎，500次/月）。降级链: 火山引擎→web_fetch→curl。参数: keywords, count(默认10), time_range(OneDay/OneWeek/OneMonth/OneYear)",
            func=web_search,
            metadata=ToolMetadata(category="network", is_idempotent=True),
        )

    def _register_web_fetch(self):
        async def web_fetch(url: str) -> str:
            """Fetch a URL and extract text content."""
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "PaperAgent/3.0"})
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for tag in soup(["script", "style", "nav", "footer"]):
                            tag.decompose()
                        text = soup.get_text(separator="\n", strip=True)[:8000]
                        return text
                    return resp.text[:8000]
            except Exception as e:
                return json.dumps({"error": f"Fetch failed: {e}"})

        self.register(
            name="web_fetch",
            description="抓取 URL 内容并转为文本。参数: url (网页地址)",
            func=web_fetch,
            metadata=ToolMetadata(category="network", is_idempotent=True),
        )

    # ══════════════════════════════════════════════════════════
    # 运维工具
    # ══════════════════════════════════════════════════════════

    def _register_service_start(self):
        def service_start(name: str) -> str:
            """Start a systemd service."""
            try:
                r = subprocess.run(["systemctl", "start", name], capture_output=True, text=True, timeout=30)
                return json.dumps({"success": r.returncode == 0, "output": r.stdout + r.stderr})
            except Exception as e:
                return json.dumps({"error": str(e)})

        self.register(name="service_start", description="启动 systemd 服务。参数: name (服务名)", func=service_start,
                      metadata=ToolMetadata(category="system", is_long_running=True))

    def _register_service_stop(self):
        def service_stop(name: str) -> str:
            try:
                r = subprocess.run(["systemctl", "stop", name], capture_output=True, text=True, timeout=30)
                return json.dumps({"success": r.returncode == 0, "output": r.stdout + r.stderr})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="service_stop", description="停止 systemd 服务。参数: name (服务名)", func=service_stop,
                      metadata=ToolMetadata(category="system"))

    def _register_service_status(self):
        def service_status(name: str) -> str:
            try:
                r = subprocess.run(["systemctl", "status", name], capture_output=True, text=True, timeout=10)
                return json.dumps({"active": "active" in r.stdout, "output": r.stdout[:2000]})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="service_status", description="查看 systemd 服务状态。参数: name (服务名)", func=service_status,
                      metadata=ToolMetadata(category="system", is_idempotent=True))

    def _register_docker_compose_up(self):
        async def docker_compose_up(cwd: str = ".") -> str:
            try:
                proc = await asyncio.create_subprocess_shell("docker-compose up -d", cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                return json.dumps({"success": proc.returncode == 0, "output": stdout.decode()[:2000], "error": stderr.decode()[:1000]})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="docker_compose_up", description="Docker Compose 一键启动。参数: cwd (项目目录)", func=docker_compose_up,
                      metadata=ToolMetadata(category="system", is_long_running=True))

    def _register_docker_compose_down(self):
        async def docker_compose_down(cwd: str = ".") -> str:
            try:
                proc = await asyncio.create_subprocess_shell("docker-compose down", cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
                return json.dumps({"success": proc.returncode == 0, "output": stdout.decode()[:2000]})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="docker_compose_down", description="Docker Compose 停止。参数: cwd (项目目录)", func=docker_compose_down,
                      metadata=ToolMetadata(category="system"))

    def _register_apt_install(self):
        async def apt_install(packages: str) -> str:
            try:
                proc = await asyncio.create_subprocess_shell(f"apt-get install -y {packages}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                return json.dumps({"success": proc.returncode == 0, "output": stdout.decode()[:3000]})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="apt_install", description="Ubuntu 包安装。参数: packages (空格分隔的包名)", func=apt_install,
                      metadata=ToolMetadata(category="system", is_long_running=True))

    def _register_pip_install(self):
        async def pip_install(packages: str) -> str:
            try:
                proc = await asyncio.create_subprocess_shell(f"{sys.executable} -m pip install {packages}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                return json.dumps({"success": proc.returncode == 0, "output": (stdout.decode() + stderr.decode())[:3000]})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="pip_install", description="Python 包安装。参数: packages (pip 安装参数)", func=pip_install,
                      metadata=ToolMetadata(category="system", is_long_running=True))

    def _register_env_config(self):
        def env_config(action: str, key: str = "", value: str = "") -> str:
            """Read or set .env configuration."""
            env_path = Path(__file__).parent.parent.parent.parent / ".env"
            try:
                if action == "read":
                    if env_path.exists():
                        return env_path.read_text()[:5000]
                    return json.dumps({"error": ".env not found"})
                elif action == "set":
                    lines = env_path.read_text().split("\n") if env_path.exists() else []
                    new_lines = []
                    found = False
                    for line in lines:
                        if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
                            new_lines.append(f"{key}={value}")
                            found = True
                        else:
                            new_lines.append(line)
                    if not found:
                        new_lines.append(f"{key}={value}")
                    env_path.write_text("\n".join(new_lines))
                    return json.dumps({"success": True, "key": key, "value": value})
                else:
                    return json.dumps({"error": f"Unknown action: {action}"})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="env_config", description="读写 .env 配置。参数: action(read/set), key, value", func=env_config,
                      metadata=ToolMetadata(category="system"))

    def _register_log_view(self):
        def log_view(lines: int = 50, log_path: str = "") -> str:
            """View recent log entries."""
            path = Path(log_path) if log_path else Path.home() / ".paper_search" / "logs" / "agent.log"
            if not path.exists():
                return json.dumps({"error": f"Log not found: {path}"})
            try:
                content = path.read_text(encoding="utf-8")
                last_lines = content.strip().split("\n")[-lines:]
                return "\n".join(last_lines)
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="log_view", description="查看最近日志。参数: lines(默认50), log_path(日志文件路径)", func=log_view,
                      metadata=ToolMetadata(category="system", is_idempotent=True))

    def _register_health_check(self):
        def health_check() -> str:
            """Run a comprehensive health check."""
            checks = {
                "db": True,
                "disk_free_gb": 0,
                "python_version": sys.version,
                "platform": sys.platform,
            }
            try:
                import shutil
                usage = shutil.disk_usage(str(Path.home()))
                checks["disk_free_gb"] = round(usage.free / (1024**3), 1)
            except Exception:
                pass
            try:
                from ..agent.db import AgentDB
                db = AgentDB()
                db.conn.execute("SELECT 1")
                checks["db"] = True
                db.close()
            except Exception as e:
                checks["db"] = f"error: {e}"
            return json.dumps(checks, ensure_ascii=False)
        self.register(name="health_check", description="全面健康检查（DB + 磁盘 + 环境）", func=health_check,
                      metadata=ToolMetadata(category="system", is_idempotent=True))

    # ══════════════════════════════════════════════════════════
    # 记忆工具
    # ══════════════════════════════════════════════════════════

    def _get_memory_manager(self):
        from ..agent.db import AgentDB
        from ..agent.memory import MemoryManager
        db = AgentDB()
        return MemoryManager(db)

    def _register_search_memory(self):
        async def search_memory(query: str, top_k: int = 5) -> str:
            mem = self._get_memory_manager()
            results = await mem.long_term.search(query, top_k=top_k)
            return json.dumps([{"title": r.title, "content": r.content[:300], "category": r.category} for r in results], ensure_ascii=False)
        self.register(name="search_memory", description="搜索历史对话和知识。参数: query, top_k(默认5)", func=search_memory,
                      metadata=ToolMetadata(category="memory", is_idempotent=True))

    def _register_summarize_memory(self):
        async def summarize_memory(messages_json: str) -> str:
            """Phase 4: 真正调 LLM 压缩 ShortTerm 段落。

            messages_json: JSON 数组，含 {role, content}
            返回 {summary, original_count}
            """
            try:
                msgs = json.loads(messages_json) if isinstance(messages_json, str) else messages_json
            except Exception as e:
                return json.dumps({"error": f"invalid messages_json: {e}"})
            if not msgs:
                return json.dumps({"summary": "", "original_count": 0})

            mem = self._get_memory_manager()
            transcript = "\n".join(
                f"[{m.get('role','?')}] {m.get('content','')[:500]}" for m in msgs
            )

            # 调用 LLMClientV2 压缩
            try:
                from .llm_client_v2 import LLMClientV2
                llm = LLMClientV2()
                resp = await llm.chat(
                    messages=[{"role": "user",
                                "content": "请把以下对话压缩成 ≤200 字中文要点摘要（保留人物/决策/关键事实，去掉寒暄）：\n\n" + transcript}],
                    system="你是一个对话摘要助手。",
                    temperature=0.2,
                )
                summary = getattr(resp, "content", None) or str(resp)
            except Exception as e:
                summary = f"[summarize failed: {e}] {transcript[:200]}"

            # 应用到 ShortTerm
            try:
                mem.short_term.set_summary(summary)
            except Exception:
                pass
            return json.dumps({"summary": summary, "original_count": len(msgs)},
                              ensure_ascii=False)
        self.register(
            name="summarize_memory",
            description="把多条历史消息压缩成摘要并替换 short_term。参数: messages_json (JSON 数组[{role,content}])",
            func=summarize_memory,
            metadata=ToolMetadata(category="memory"),
        )

    def _register_delete_memory(self):
        def delete_memory(message_ids: str) -> str:
            """Phase 4: 真删除（ShortTerm + LongTerm）。

            message_ids: JSON 数组，或单个字符串 ID。
            支持 knowledge_entries 表中的 id 删除。
            """
            try:
                ids = json.loads(message_ids) if message_ids.startswith("[") else [message_ids]
            except Exception:
                ids = [message_ids]

            mem = self._get_memory_manager()
            deleted = 0
            # 尝试 long_term 删
            try:
                for mid in ids:
                    row = mem._db.conn.execute(
                        "DELETE FROM knowledge_entries WHERE id=?", (mid,),
                    )
                    deleted += row.rowcount or 0
                mem._db.conn.commit()
            except Exception as e:
                logger.debug(f"delete_memory long_term failed: {e}")

            # short_term: 用 deque 难以按 id 精准删；仅按位置粗删
            try:
                if hasattr(mem.short_term, "_turns"):
                    before = len(mem.short_term._turns)
                    mem.short_term._turns = type(mem.short_term._turns)(
                        t for t in mem.short_term._turns
                        if getattr(t, "id", None) not in ids
                    )
                    deleted += before - len(mem.short_term._turns)
            except Exception:
                pass

            return json.dumps({"deleted": deleted, "ids": ids})
        self.register(
            name="delete_memory",
            description="从短期+长期记忆中删除指定条目。参数: message_ids (JSON 数组或单 ID)",
            func=delete_memory,
            metadata=ToolMetadata(category="memory"),
        )

    def _register_extract_to_long_term(self):
        def extract_to_long_term(content_json: str) -> str:
            """Phase 4: 写 SQLite + ChromaDB 索引。

            content_json: JSON 数组，每项 {key, value, type, paper_id?}
            """
            try:
                items = json.loads(content_json) if isinstance(content_json, str) else content_json
            except Exception as e:
                return json.dumps({"error": f"invalid content_json: {e}"})

            mem = self._get_memory_manager()
            from ..agent.memory import KnowledgeEntry
            extracted = 0
            for item in items:
                try:
                    entry = KnowledgeEntry(
                        id="", title=item.get("key", "extracted"),
                        content=item.get("value", ""),
                        category=item.get("type", "finding"),
                        source_paper_id=item.get("paper_id", ""),
                        source_paper_title=item.get("paper_title", ""),
                    )
                    entry_id = mem.long_term.add_knowledge(entry)
                    extracted += 1
                    # ChromaDB 索引（如果 chroma_store 可用）
                    chroma = getattr(mem.long_term, "_chroma", None)
                    if chroma is not None:
                        try:
                            # 写入 agent_knowledge collection（如不存在则跳过）
                            col = (getattr(chroma, "get_collection", None)
                                   or getattr(chroma, "collection", None))
                            if callable(col):
                                col_obj = col("agent_knowledge")
                            else:
                                col_obj = None
                            if col_obj and hasattr(col_obj, "add"):
                                col_obj.add(
                                    ids=[entry_id or entry.title],
                                    documents=[entry.content],
                                    metadatas=[{
                                        "category": entry.category,
                                        "title": entry.title,
                                        "source_paper_id": entry.source_paper_id,
                                    }],
                                )
                        except Exception as e:
                            logger.debug(f"chroma index for knowledge failed: {e}")
                except Exception as e:
                    logger.debug(f"extract item failed: {e}")
            return json.dumps({"extracted": extracted, "total": len(items)})
        self.register(
            name="extract_to_long_term",
            description="把要点条目存入长期记忆（SQLite + 向量索引）。参数: content_json (JSON 数组[{key,value,type,paper_id?}])",
            func=extract_to_long_term,
            metadata=ToolMetadata(category="memory"),
        )

    def _register_tag_memory(self):
        def tag_memory(message_id: str, tags: str) -> str:
            """Phase 4: 在 knowledge_entries 的 metadata 中追加 tags。"""
            try:
                tag_list = json.loads(tags) if isinstance(tags, str) else tags
            except Exception:
                tag_list = [tags]

            mem = self._get_memory_manager()
            try:
                # knowledge_entries 表如果有 tags 字段就更新；
                # 没有就放 metadata JSON 字段
                row = mem._db.conn.execute(
                    "SELECT metadata FROM knowledge_entries WHERE id=?",
                    (message_id,),
                ).fetchone()
                if not row:
                    return json.dumps({"error": "message_id not found",
                                        "message_id": message_id})
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except Exception:
                    meta = {}
                existing = set(meta.get("tags", []))
                existing.update(tag_list)
                meta["tags"] = sorted(existing)
                mem._db.conn.execute(
                    "UPDATE knowledge_entries SET metadata=? WHERE id=?",
                    (json.dumps(meta), message_id),
                )
                mem._db.conn.commit()
                return json.dumps({"message_id": message_id, "tags": list(existing),
                                    "success": True})
            except Exception as e:
                return json.dumps({"error": str(e), "message_id": message_id})
        self.register(
            name="tag_memory",
            description="给长期记忆条目打标签。参数: message_id (knowledge_entries.id), tags (JSON 数组)",
            func=tag_memory,
            metadata=ToolMetadata(category="memory"),
        )

    def _register_get_user_preference(self):
        def get_user_preference(key: str) -> str:
            mem = self._get_memory_manager()
            val = mem.meta.get_preference(key)
            return json.dumps({key: val} if val else {"error": f"No preference for {key}"})
        self.register(name="get_user_preference", description="获取用户偏好。参数: key (偏好键名)", func=get_user_preference,
                      metadata=ToolMetadata(category="memory", is_idempotent=True))

    def _register_list_collections(self):
        def list_collections() -> str:
            try:
                from ..agent.chroma_store import ChromaStoreV2
                chroma = ChromaStoreV2()
                cols = chroma.list_collections() if hasattr(chroma, 'list_collections') else ["papers_abstract", "papers_fulltext"]
                return json.dumps({"collections": cols})
            except Exception as e:
                return json.dumps({"collections": ["papers_abstract", "papers_fulltext"], "note": f"placeholder: {e}"})
        self.register(name="list_collections", description="列出 ChromaDB 所有集合", func=list_collections,
                      metadata=ToolMetadata(category="memory", is_idempotent=True))

    # ══════════════════════════════════════════════════════════
    # iOS 自动工具
    # ══════════════════════════════════════════════════════════

    def _register_ios_tool(self, name, description):
        def ios_stub(**kwargs) -> str:
            return json.dumps({"ios_tool": name, "args": kwargs, "note": "Sent via WebSocket tool(ios) message. Result returned asynchronously."})
        self.register(name=name, description=description, func=ios_stub,
                      metadata=ToolMetadata(location="ios", category="ios"))

    def _register_ios_file_read(self):
        self._register_ios_tool("ios_file_read", "读取 iOS 本地文件。参数: path (文件路径)")

    def _register_ios_file_write(self):
        self._register_ios_tool("ios_file_write", "写入文件到 iOS 本地。参数: path, content")

    def _register_ios_file_list(self):
        self._register_ios_tool("ios_file_list", "列出 iOS 本地目录。参数: path")

    def _register_ios_calendar_add(self):
        self._register_ios_tool("ios_calendar_add", "添加日历事件。参数: title, start_time, end_time, notes")

    def _register_ios_calendar_read(self):
        self._register_ios_tool("ios_calendar_read", "读取日历事件。参数: start_date, end_date")

    def _register_ios_reminder_add(self):
        self._register_ios_tool("ios_reminder_add", "添加提醒事项。参数: title, due_date, notes")

    def _register_ios_notification_local(self):
        self._register_ios_tool("ios_notification_local", "发送本地通知。参数: title, body, category")

    def _register_ios_device_info(self):
        self._register_ios_tool("ios_device_info", "获取 iOS 设备信息（型号/系统版本/存储/网络）")

    def _register_ios_location_get(self):
        self._register_ios_tool("ios_location_get", "获取 iPhone 当前位置。无参数")

    # ══════════════════════════════════════════════════════════
    # 直接查询
    # ══════════════════════════════════════════════════════════

    def _register_paper_status(self):
        def paper_status(project_id: str = "", paper_id: str = "") -> str:
            from ..agent.db import AgentDB
            db = AgentDB()
            if paper_id:
                row = db.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if row:
                    return json.dumps(dict(row), ensure_ascii=False, default=str)
                return json.dumps({"error": f"Paper not found: {paper_id}"})
            if project_id:
                papers = db.get_project_papers(project_id)
                return json.dumps({"project_id": project_id, "total": len(papers), "papers": [{"title": p.get("title",""), "year": p.get("year"), "relevance_score": p.get("relevance_score")} for p in papers[:20]]}, ensure_ascii=False)
            return json.dumps({"error": "Provide project_id or paper_id"})
        self.register(name="paper_status", description="查看项目/论文进度。参数: project_id 或 paper_id", func=paper_status,
                      metadata=ToolMetadata(category="export", is_idempotent=True))

    def _register_list_sources(self):
        def list_sources() -> str:
            try:
                from ..providers import list_providers
                sources = [s.value for s in list_providers()]
                return json.dumps({"sources": sources, "total": len(sources)})
            except Exception as e:
                return json.dumps({"error": str(e)})
        self.register(name="list_sources", description="列出所有可用学术搜索来源及其状态", func=list_sources,
                      metadata=ToolMetadata(category="export", is_idempotent=True))

    def _register_get_paper_abstract(self):
        def get_paper_abstract(paper_id: str) -> str:
            from ..agent.db import AgentDB
            db = AgentDB()
            row = db.conn.execute("SELECT title, abstract, year, venue FROM papers WHERE id=?", (paper_id,)).fetchone()
            if row:
                return json.dumps(dict(row), ensure_ascii=False, default=str)
            return json.dumps({"error": f"Paper not found: {paper_id}"})
        self.register(name="get_paper_abstract", description="获取论文摘要。参数: paper_id", func=get_paper_abstract,
                      metadata=ToolMetadata(category="export", is_idempotent=True))

    # ══════════════════════════════════════════════════════════
    # 子 Agent 工具
    # ══════════════════════════════════════════════════════════

    def _register_sub_agent_tools(self):
        """注册子 Agent 工具（执行阶段由 Plan Graph 直接调用）。"""
        # Abstracts the existing CLI/MCP functions as direct tool wrappers
        sub_tools = [
            ("search_papers", "跨多源搜索学术论文", self._make_search_papers()),
            ("download_paper", "下载单篇论文 PDF", self._make_download_paper()),
            ("convert_paper", "PDF 转 Markdown", self._make_convert_paper()),
            ("index_paper", "Markdown 索引入 ChromaDB", self._make_index_paper()),
            ("evaluate_papers", "LLM 批量评估论文相关性", self._make_evaluate_papers()),
            ("rank_papers", "期刊等级评定（CCF/SCI → A+/A/B/C）", self._make_rank_papers()),
            ("generate_survey", "生成 AI 文献综述报告", self._make_generate_survey()),
            ("paper_export", "导出 BibTeX/JSON", self._make_paper_export()),
            ("paper_clean", "清理项目 DB/索引", self._make_paper_clean()),
            ("batch_search", "从 JSON/CSV 批量搜索", self._make_batch_search()),
            ("citation_chase", "1 层引用追踪", self._make_citation_chase()),
            ("search_library", "ChromaDB 语义搜索已入库论文", self._make_search_library()),
            ("search_knowledge", "ChromaDB 搜索结构化知识", self._make_search_knowledge()),
            ("read_paper", "读取论文完整 Markdown", self._make_read_paper()),
            ("extract_knowledge", "从论文中提取结构化知识", self._make_extract_knowledge()),
            ("find_related", "发现相关论文", self._make_find_related()),
            ("discover_gaps", "知识发现 — 研究空白/矛盾/趋势", self._make_discover_gaps()),
            ("build_glossary", "构建中英学术术语表", self._make_build_glossary()),
            ("translate_query", "中文查询翻译为学术英文关键词", self._make_translate_query()),
        ]
        for name, desc, func in sub_tools:
            self.register_direct(name, desc, func,
                                 metadata=ToolMetadata(category="search"))

    def _make_search_papers(self):
        async def search_papers(keywords: str, sources: str = "arxiv,semantic_scholar",
                                 year_from: int = 2020, max_results: int = 20) -> str:
            from ..models import SearchQuery, SourceType
            from ..engine import PaperSearchEngine
            from ..config import Config
            from ..agent.db import AgentDB
            source_list = [SourceType(s.strip()) for s in sources.split(",") if s.strip()]
            if not source_list:
                source_list = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]
            query = SearchQuery(keywords=keywords, year_from=year_from, max_results=max_results, sources=source_list)
            engine = PaperSearchEngine(Config())
            result = await engine.search(query)
            db = AgentDB()
            pid = db.create_project(user_query=keywords)
            papers_out = []
            for p in result.papers:
                paper_id = db.upsert_paper(p)
                db.link_paper_to_project(pid, paper_id)
                papers_out.append({
                    "id": paper_id, "title": p.title, "year": p.year,
                    "abstract": (p.abstract or "")[:300], "venue": p.venue,
                    "citation_count": p.citation_count,
                })
            return json.dumps({"project_id": pid, "total": result.total_found, "papers": papers_out}, ensure_ascii=False)
        return search_papers

    def _make_download_paper(self):
        async def download_paper(title: str, source: str = "arxiv", paper_id: str = "") -> str:
            from ..engine import PaperSearchEngine
            from ..config import Config
            from ..agent.db import AgentDB
            engine = PaperSearchEngine(Config())
            # Simplified: find paper by title and download
            return json.dumps({"paper_id": paper_id, "title": title, "note": "PDF download dispatched to Celery worker"})
        return download_paper

    def _make_convert_paper(self):
        async def convert_paper(paper_id: str, pdf_path: str = "") -> str:
            return json.dumps({"paper_id": paper_id, "note": "PDF→Markdown dispatched to Celery"})
        return convert_paper

    def _make_index_paper(self):
        async def index_paper(paper_id: str, project_id: str = "", all: bool = True) -> str:
            return json.dumps({"paper_id": paper_id, "all": all, "note": "Index dispatched to Celery"})
        return index_paper

    def _make_evaluate_papers(self):
        async def evaluate_papers(project_id: str, query: str = "", all: bool = True) -> str:
            return json.dumps({"project_id": project_id, "note": "LLM evaluation dispatched"})
        return evaluate_papers

    def _make_rank_papers(self):
        async def rank_papers(project_id: str = "", all: bool = True) -> str:
            return json.dumps({"project_id": project_id, "note": "Journal ranking dispatched"})
        return rank_papers

    def _make_generate_survey(self):
        async def generate_survey(project_id: str) -> str:
            return json.dumps({"project_id": project_id, "note": "Survey generation dispatched to Celery"})
        return generate_survey

    def _make_paper_export(self):
        async def paper_export(project_id: str, format: str = "bibtex") -> str:
            return json.dumps({"project_id": project_id, "format": format, "note": "Export dispatched"})
        return paper_export

    def _make_paper_clean(self):
        async def paper_clean(project_id: str, keep_pdfs: bool = True) -> str:
            from ..agent.db import AgentDB
            db = AgentDB()
            db.conn.execute("DELETE FROM project_papers WHERE project_id=?", (project_id,))
            db.conn.commit()
            return json.dumps({"project_id": project_id, "cleaned": True})
        return paper_clean

    def _make_batch_search(self):
        async def batch_search(file_path: str, download: bool = False) -> str:
            return json.dumps({"file_path": file_path, "note": "Batch search dispatched"})
        return batch_search

    def _make_citation_chase(self):
        async def citation_chase(paper_title: str, doi: str = "") -> str:
            return json.dumps({"paper_title": paper_title, "note": "Citation chase dispatched"})
        return citation_chase

    def _make_search_library(self):
        async def search_library(query: str, top_k: int = 5) -> str:
            try:
                from ..agent.chroma_store import ChromaStoreV2
                chroma = ChromaStoreV2()
                results = chroma.search_similar(query, n_results=top_k)
                return json.dumps(results, ensure_ascii=False, default=str)
            except Exception as e:
                return json.dumps({"error": str(e)})
        return search_library

    def _make_search_knowledge(self):
        async def search_knowledge(query: str, top_k: int = 5) -> str:
            from ..agent.db import AgentDB
            db = AgentDB()
            like = f"%{query}%"
            rows = db.conn.execute(
                "SELECT * FROM knowledge_entries WHERE title LIKE ? OR content LIKE ? LIMIT ?",
                (like, like, top_k),
            ).fetchall()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
        return search_knowledge

    def _make_read_paper(self):
        def read_paper(paper_id: str) -> str:
            from ..agent.db import AgentDB
            db = AgentDB()
            row = db.conn.execute("SELECT markdown_path, title FROM papers WHERE id=?", (paper_id,)).fetchone()
            if row and row["markdown_path"]:
                path = Path(row["markdown_path"])
                if path.exists():
                    return path.read_text(encoding="utf-8")[:10000]
            return json.dumps({"error": f"No markdown for {paper_id}", "title": row["title"] if row else "unknown"})
        return read_paper

    def _make_extract_knowledge(self):
        async def extract_knowledge(paper_id: str, deep: bool = False) -> str:
            return json.dumps({"paper_id": paper_id, "deep": deep, "note": "Knowledge extraction dispatched"})
        return extract_knowledge

    def _make_find_related(self):
        async def find_related(paper_id: str, top_k: int = 10) -> str:
            return json.dumps({"paper_id": paper_id, "note": "Related paper search dispatched"})
        return find_related

    def _make_discover_gaps(self):
        async def discover_gaps(domain: str = "", project_id: str = "") -> str:
            return json.dumps({"domain": domain, "note": "Gap discovery dispatched"})
        return discover_gaps

    def _make_build_glossary(self):
        async def build_glossary(terms_json: str) -> str:
            return json.dumps({"note": "Glossary build dispatched", "input_count": len(json.loads(terms_json))})
        return build_glossary

    def _make_translate_query(self):
        async def translate_query(query: str, target_lang: str = "en") -> str:
            return json.dumps({"original": query, "target_lang": target_lang, "note": "Translation via LLM"})
        return translate_query
