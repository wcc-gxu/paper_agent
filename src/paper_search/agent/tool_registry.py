"""统一工具注册中心 — 所有 CLI/MCP/新工具的统一管理和发现.

设计:
- 装饰器注册: @register_tool 自动注册
- 分类管理: search / download / convert / index / analyze / export / manage
- 格式导出: Anthropic tool_use / OpenAI function calling
- LLM 工具发现: 根据场景按需加载工具集

使用方式:
    from paper_search.agent.tool_registry import registry, register_tool

    @register_tool(
        name="search_papers",
        description="跨多源搜索学术论文",
        category="search",
        cost_estimate=500,
    )
    async def search_papers(keywords: str, ...) -> dict:
        ...

    # 获取 Anthropic 格式的工具列表
    tools = registry.to_anthropic()

    # 按分类获取
    search_tools = registry.get_by_category("search")

    # 执行工具
    result = await registry.execute("search_papers", keywords="transformer")
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Tool Category enum
# ═══════════════════════════════════════════════════════════════


class ToolCategory:
    """工具分类常量."""

    SEARCH = "search"  # 论文搜索
    DOWNLOAD = "download"  # PDF 下载
    CONVERT = "convert"  # PDF→MD 转换
    INDEX = "index"  # 向量索引
    ANALYZE = "analyze"  # 分析与评估
    EXPORT = "export"  # 导出
    MANAGE = "manage"  # 管理 (状态/清理)
    KB = "knowledge_base"  # 知识库操作
    SUBSCRIPTION = "subscription"  # 订阅管理

    ALL = [
        SEARCH, DOWNLOAD, CONVERT, INDEX, ANALYZE, EXPORT, MANAGE, KB, SUBSCRIPTION
    ]


# ═══════════════════════════════════════════════════════════════
# Tool Definition
# ═══════════════════════════════════════════════════════════════


@dataclass
class RegisteredTool:
    """统一工具定义."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable  # 实际执行函数 (async 或 sync)
    category: str = ToolCategory.SEARCH
    cost_estimate: int = 100  # 预估 token 消耗
    is_idempotent: bool = False  # 是否幂等 (用于重试/恢复)
    is_long_running: bool = False  # 是否可能长时间运行
    requires_confirmation: bool = False  # 是否需要用户确认
    tags: list[str] = field(default_factory=list)

    # ── 导出租户 ──────────────────────────────────────────

    def to_anthropic(self) -> dict:
        """导出为 Anthropic tool_use 格式."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._clean_schema(self.parameters),
        }

    def to_openai(self) -> dict:
        """导出为 OpenAI function calling 格式."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._clean_schema(self.parameters),
            },
        }

    def _clean_schema(self, schema: dict) -> dict:
        """清理 JSON Schema，移除 Python 特定字段."""
        result = {
            "type": schema.get("type", "object"),
            "properties": {},
        }
        if "required" in schema:
            result["required"] = schema["required"]

        for prop_name, prop_def in schema.get("properties", {}).items():
            clean_prop = {}
            for k, v in prop_def.items():
                if k in ("type", "description", "enum", "default", "items",
                         "minimum", "maximum", "minLength", "maxLength",
                         "pattern", "format", "properties", "required",
                         "anyOf", "oneOf", "allOf"):
                    clean_prop[k] = v
            result["properties"][prop_name] = clean_prop

        return result

    # ── 执行 ──────────────────────────────────────────────

    async def execute(self, **kwargs) -> Any:
        """执行工具, 自动判断 async/sync."""
        try:
            if asyncio.iscoroutinefunction(self.handler):
                return await self.handler(**kwargs)
            else:
                # 在线程池中执行同步函数
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, lambda: self.handler(**kwargs))
        except Exception as e:
            logger.error(f"Tool '{self.name}' execution failed: {e}")
            raise


# ═══════════════════════════════════════════════════════════════
# Tool Registry
# ═══════════════════════════════════════════════════════════════


class ToolRegistry:
    """统一工具注册中心.

    单例模式, 管理所有注册的工具.
    """

    _instance: Optional[ToolRegistry] = None

    def __new__(cls) -> ToolRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: dict[str, RegisteredTool] = {}
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._tools: dict[str, RegisteredTool] = {}
        self._initialized = True

    # ── 注册 ───────────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] = None,
        *,
        category: str = ToolCategory.SEARCH,
        cost_estimate: int = 100,
        is_idempotent: bool = False,
        is_long_running: bool = False,
        requires_confirmation: bool = False,
        tags: list[str] = None,
    ) -> Callable:
        """装饰器: 注册工具.

        Usage:
            @registry.register(
                name="search_papers",
                description="搜索学术论文",
                parameters={...},
                category="search",
            )
            async def search_papers(keywords: str, ...):
                ...
        """
        def decorator(handler: Callable) -> Callable:
            params = parameters
            if params is None:
                params = _infer_schema_from_handler(handler, name)

            tool = RegisteredTool(
                name=name,
                description=description,
                parameters=params,
                handler=handler,
                category=category,
                cost_estimate=cost_estimate,
                is_idempotent=is_idempotent,
                is_long_running=is_long_running,
                requires_confirmation=requires_confirmation,
                tags=tags or [],
            )
            self._tools[name] = tool
            logger.debug(f"Registered tool: {name} [{category}]")
            return handler

        return decorator

    def register_direct(self, tool: RegisteredTool):
        """直接注册已构建的 RegisteredTool 实例."""
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool (direct): {tool.name} [{tool.category}]")

    def unregister(self, name: str):
        """移除工具注册."""
        self._tools.pop(name, None)

    # ── 查询 ───────────────────────────────────────────────

    def get(self, name: str) -> Optional[RegisteredTool]:
        """按名称获取工具."""
        return self._tools.get(name)

    def get_by_category(self, category: str) -> list[RegisteredTool]:
        """按分类获取工具列表."""
        return [t for t in self._tools.values() if t.category == category]

    def get_by_tag(self, tag: str) -> list[RegisteredTool]:
        """按标签获取工具列表."""
        return [t for t in self._tools.values() if tag in t.tags]

    def list_all(self) -> list[RegisteredTool]:
        """列出所有工具."""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_categories(self) -> list[str]:
        cats = set(t.category for t in self._tools.values())
        return sorted(cats)

    # ── 导出 ───────────────────────────────────────────────

    def to_anthropic(self, categories: list[str] = None, tools: list[str] = None) -> list[dict]:
        """导出为 Anthropic tool_use 格式.

        Args:
            categories: 按分类筛选 (None = 全部)
            tools: 按名称筛选 (None = 全部)
        """
        selected = self._select(categories, tools)
        return [t.to_anthropic() for t in selected]

    def to_openai(self, categories: list[str] = None, tools: list[str] = None) -> list[dict]:
        """导出为 OpenAI function calling 格式."""
        selected = self._select(categories, tools)
        return [t.to_openai() for t in selected]

    def _select(self, categories: list[str] = None, tools: list[str] = None) -> list[RegisteredTool]:
        """按条件筛选工具."""
        result = list(self._tools.values())
        if categories:
            result = [t for t in result if t.category in categories]
        if tools:
            result = [t for t in result if t.name in tools]
        return result

    # ── 执行 ───────────────────────────────────────────────

    async def execute(self, name: str, **kwargs) -> Any:
        """按名称执行工具."""
        tool = self.get(name)
        if not tool:
            raise ValueError(f"Tool not found: {name}")
        return await tool.execute(**kwargs)

    # ── 工具摘要 (供 LLM system prompt) ────────────────────

    def generate_tool_prompt(self, categories: list[str] = None) -> str:
        """生成供 LLM system prompt 使用的工具描述."""
        selected = self._select(categories)
        lines = ["可用工具:\n"]
        for t in selected:
            lines.append(f"### {t.name}")
            lines.append(f"描述: {t.description}")
            lines.append(f"分类: {t.category}")
            lines.append(f"参数: {json.dumps(t.parameters, ensure_ascii=False)}")
            lines.append(f"预估消耗: ~{t.cost_estimate} tokens")
            if t.requires_confirmation:
                lines.append("⚠️ 此工具需要用户确认")
            lines.append("")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Helper: Infer JSON Schema from Function Signature
# ═══════════════════════════════════════════════════════════════

import json


def _infer_schema_from_handler(handler: Callable, tool_name: str) -> dict:
    """从函数签名的类型注解自动推断 JSON Schema."""
    sig = inspect.signature(handler)

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            json_type = "string"
        else:
            json_type = type_map.get(annotation, "string")

        prop_def = {"type": json_type, "description": f"{param_name} 参数"}

        if param.default is not inspect.Parameter.empty:
            if param.default is not None:
                prop_def["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = prop_def

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

registry = ToolRegistry()


# 模块级装饰器快捷方式
def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any] = None,
    *,
    category: str = ToolCategory.SEARCH,
    cost_estimate: int = 100,
    is_idempotent: bool = False,
    is_long_running: bool = False,
    requires_confirmation: bool = False,
    tags: list[str] = None,
) -> Callable:
    """模块级工具注册装饰器."""
    return registry.register(
        name=name,
        description=description,
        parameters=parameters,
        category=category,
        cost_estimate=cost_estimate,
        is_idempotent=is_idempotent,
        is_long_running=is_long_running,
        requires_confirmation=requires_confirmation,
        tags=tags,
    )
