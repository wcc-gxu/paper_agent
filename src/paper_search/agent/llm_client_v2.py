"""LLM Client V2 — 多供应商 Anthropic 兼容接口 + 流式 + 重试 + 结构化输出.

升级要点:
- 多供应商支持: 火山引擎 / OpenAI / Anthropic / 任何 Anthropic 兼容 API
- 指数退避重试: 临时故障自动重试 (最多3次)
- 流式输出: SSE 解析, 适合长文本生成
- 结构化输出: 内置 JSON Schema 约束
- 速率限制: 每供应商可配置 RPM/TPM
- 向后兼容: 保留所有旧版数据类和 prompt

使用方式:
    client = LLMClientV2()                          # 默认火山引擎
    client = LLMClientV2(provider="openai")         # OpenAI
    client.add_provider("custom", base_url="...", api_key="...")

    # 简单对话
    response = await client.chat(messages=[...])

    # 流式对话
    async for chunk in client.chat_stream(messages=[...]):
        print(chunk, end="")

    # 带工具调用
    response = await client.chat(messages=[...], tools=[...])

    # 结构化 JSON 输出
    result = await client.chat_json(messages=[...], schema=MyModel)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, ClassVar, Optional

import httpx

from .llm_client import (
    ContinueDecision,
    RelevanceJudgment,
    SearchIntent,
)
from ..config import get_model_for_node

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Provider Configuration
# ═══════════════════════════════════════════════════════════════


@dataclass
class ProviderConfig:
    """单个 LLM 提供商的配置."""

    name: str
    base_url: str
    api_key: str = ""
    model: str = "default"
    # 速率限制
    max_rpm: int = 60  # 每分钟最大请求数
    max_tpm: int = 100_000  # 每分钟最大 token 数
    # 重试
    max_retries: int = 3
    retry_base_delay: float = 1.0  # 指数退避基数 (秒)
    # 超时
    request_timeout: float = 120.0
    # 额外 HTTP headers
    extra_headers: dict[str, str] = field(default_factory=dict)


# 内置供应商模板
_PROVIDER_TEMPLATES: dict[str, ProviderConfig] = {
    "volcano": ProviderConfig(
        name="volcano",
        base_url="https://ark.cn-beijing.volces.com/api/plan",
        model="ark-code-latest",
        max_rpm=60,
        max_tpm=100_000,
        extra_headers={"x-api-key": ""},  # 运行时填充
    ),
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
        max_rpm=500,
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-6",
        max_rpm=50,
    ),
}


# ═══════════════════════════════════════════════════════════════
# Tool / Function Calling Types
# ═══════════════════════════════════════════════════════════════


@dataclass
class ToolDef:
    """工具定义 — 符合 Anthropic tool_use 格式."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema
    handler: Optional[Callable] = None  # 实际执行函数 (可选, 用于本地执行)

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ToolCall:
    """LLM 返回的工具调用."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    """统一消息格式."""

    role: str  # system | user | assistant | tool
    content: str
    name: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None


@dataclass
class ChatResponse:
    """聊天响应."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | max_tokens | tool_use
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════


class RateLimiter:
    """简单的滑动窗口速率限制器."""

    def __init__(self, max_requests_per_minute: int = 60):
        self.max_rpm = max_requests_per_minute
        self._window: list[float] = []

    async def acquire(self):
        """等待直到可以发出请求."""
        now = time.monotonic()
        # 清理旧记录
        cutoff = now - 60.0
        self._window = [t for t in self._window if t > cutoff]

        if len(self._window) >= self.max_rpm:
            # 需要等待最旧的记录过期
            wait_time = self._window[0] - cutoff + 0.1
            if wait_time > 0:
                logger.debug(f"Rate limiter: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
            # 递归重试
            return await self.acquire()

        self._window.append(time.monotonic())


# ═══════════════════════════════════════════════════════════════
# SSE Parser
# ═══════════════════════════════════════════════════════════════


class SSEParser:
    """Server-Sent Events 解析器 — 处理流式响应."""

    def __init__(self):
        self._buffer = ""

    def feed(self, chunk: str) -> list[dict]:
        """喂入数据块, 返回完整的事件列表."""
        self._buffer += chunk
        events = []
        while "\n\n" in self._buffer:
            event_str, self._buffer = self._buffer.split("\n\n", 1)
            parsed = self._parse_event(event_str)
            if parsed:
                events.append(parsed)
        return events

    def _parse_event(self, raw: str) -> Optional[dict]:
        """解析单个 SSE 事件."""
        data = {}
        for line in raw.split("\n"):
            if line.startswith("data: "):
                json_str = line[6:]
                if json_str.strip() == "[DONE]":
                    return {"type": "done"}
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    pass
            elif line.startswith("event: "):
                data["_event"] = line[7:]
        return data if data else None


# ═══════════════════════════════════════════════════════════════
# LLM Client V2
# ═══════════════════════════════════════════════════════════════


class LLMClientV2:
    """多供应商 Anthropic 兼容 LLM 客户端.

    核心能力:
    - chat(): 标准对话 (支持 tool use)
    - chat_stream(): 流式对话
    - chat_json(): 结构化 JSON 输出
    - 自动重试 + 速率限制 + 供应商切换
    """

    # ── 与旧版兼容的 Prompt 常量 ──────────────────────────

    INTENT_SYSTEM_PROMPT: ClassVar[str] = """你是一个学术搜索意图解析器。用户会用自然语言描述搜索需求，
你需要输出结构化的搜索计划。

可用搜索来源:
- arxiv: 预印本 (CS/AI/数学/物理)
- semantic_scholar: 综合学术搜索
- pubmed: 生物医学
- sciencedirect: Elsevier 综合期刊
- ieee: IEEE 电子/计算机工程
- cnki: 中国知网 (中文学术)

领域提示:
- "AI安全" / "对抗攻击" / "adversarial" → 侧重 arxiv, ieee, semantic_scholar
- "网络安全" / "cybersecurity" → 侧重 ieee, arxiv
- "医学" / "生物" → 侧重 pubmed
- 中文关键词 → 必须包含 cnki

对于时间范围:
- "最近半年" → year_from = 当前年份-1 到当前年份
- "2023年以来" → year_from = 2023
- 未提及 → 不设时间限制

输出纯 JSON（不要 markdown 代码块）:
{
  "sub_queries": ["拆解的子查询1", "子查询2"],
  "year_from": 2024,
  "year_to": 2026,
  "sources": ["arxiv", "semantic_scholar"],
  "entities": ["识别到的作者名", "论文DOI"],
  "domain_hint": "AI security"
}"""

    RELEVANCE_SYSTEM_PROMPT: ClassVar[str] = """你是一个论文学术价值评估器。给定用户的研究需求和一篇论文的元数据，
判断这篇论文与用户需求的相关性。

评分标准:
- 1.0: 完美匹配，论文核心就是用户要找的
- 0.7-0.9: 高度相关，值得精读
- 0.5-0.6: 部分相关，可作为参考
- 0.3-0.4: 勉强相关，领域接近但主题有偏差
- 0.0-0.2: 不相关

输出纯 JSON（不要代码块）:
{
  "score": 0.85,
  "reason": "一句话解释为什么给这个分数（中文）",
  "is_relevant": true
}"""

    CONTINUE_SYSTEM_PROMPT: ClassVar[str] = """你是一个学术搜索策略评估器。用户进行了一次文献搜索，
你需要判断是否已经搜够了，还是需要调整策略继续搜索。

判断标准:
- 已经找到10+篇高质量相关论文 → 可以停止
- 只找到0-3篇相关论文 → 需要调整搜索词继续
- 结果集中在某几个子领域，可能遗漏了其他方面 → 建议新的搜索方向
- 结果太少可能是搜索词太窄，建议放宽关键词

输出纯 JSON:
{
  "should_continue": true,
  "reason": "只找到2篇相关论文，建议用更宽泛的关键词重试或扩展到其他来源",
  "new_queries": ["broader keyword 1", "alternative keyword 2"],
  "new_sources": ["ieee"]
}"""

    DIGEST_SYSTEM_PROMPT: ClassVar[str] = """你是一个学术论文摘要提炼器。给定论文元数据，输出结构化摘要:

输出纯 JSON:
{
  "digest": ["要点1", "要点2", "要点3", "要点4", "要点5"],
  "one_liner": "一句话总结这篇论文的贡献",
  "method_tags": ["方法1", "方法2"],
  "dataset_info": "用的数据集/基准",
  "reading_level": "skim"
}"""

    REPORT_SYSTEM_PROMPT: ClassVar[str] = """你是一个学术搜索报告生成器。根据搜索结果生成结构化的文献综述摘要。

输出应包含以下部分（使用 Markdown 格式）:

## 搜索概况
- 原始需求
- 搜索来源
- 找到论文数 / 相关论文数

## 关键论文
对每篇高相关性论文 (>=0.7) 做简短描述:
- **标题**: 一句话概括
- **方法/贡献**: 一句话
- **来源/年份**: 出处

## 研究方向分类
将相关论文按主题分组（2-4 个组），每组一句话说明侧重点

## 建议
根据搜索结果，给出进一步研究的建议（2-3 条）"""

    def __init__(
        self,
        provider: str = "volcano",
        max_tokens: int = 4096,
        default_temperature: float = 0.3,
    ):
        self._providers: dict[str, ProviderConfig] = {}
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._current_provider = provider
        self.max_tokens = max_tokens
        self.default_temperature = default_temperature

        # 加载内置模板
        for name, tmpl in _PROVIDER_TEMPLATES.items():
            cfg = ProviderConfig(
                name=tmpl.name,
                base_url=tmpl.base_url,
                model=tmpl.model,
                max_rpm=tmpl.max_rpm,
                max_tpm=tmpl.max_tpm,
                max_retries=tmpl.max_retries,
                retry_base_delay=tmpl.retry_base_delay,
                request_timeout=tmpl.request_timeout,
                extra_headers=dict(tmpl.extra_headers),
            )
            self._providers[name] = cfg

        # 自动加载 API keys
        self._load_api_keys()

    def _load_api_keys(self):
        """从环境变量自动加载 API keys."""
        # 确保 .env 已加载
        try:
            from dotenv import load_dotenv as _load
            env_path = Path(__file__).parent.parent.parent.parent / ".env"
            if env_path.exists():
                _load(env_path)
        except ImportError:
            pass

        # 火山引擎
        if key := os.environ.get("VOLCANO_API_KEY"):
            self._providers["volcano"].api_key = key
            self._providers["volcano"].extra_headers["x-api-key"] = key

        # OpenAI
        if key := os.environ.get("OPENAI_API_KEY"):
            self._providers["openai"].api_key = key

        # Anthropic
        if key := os.environ.get("ANTHROPIC_API_KEY"):
            self._providers["anthropic"].api_key = key

    # ── Provider Management ───────────────────────────────

    @property
    def provider(self) -> ProviderConfig:
        return self._providers[self._current_provider]

    def add_provider(self, name: str, **kwargs) -> ProviderConfig:
        """动态添加供应商."""
        cfg = ProviderConfig(name=name, **kwargs)
        self._providers[name] = cfg
        return cfg

    def switch_provider(self, name: str):
        """切换当前供应商."""
        if name not in self._providers:
            raise ValueError(f"Unknown provider: {name}. Available: {list(self._providers)}")
        self._current_provider = name
        logger.info(f"Switched to provider: {name}")

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def _get_rate_limiter(self) -> RateLimiter:
        p = self.provider
        if p.name not in self._rate_limiters:
            self._rate_limiters[p.name] = RateLimiter(p.max_rpm)
        return self._rate_limiters[p.name]

    # ── HTTP Client ────────────────────────────────────────

    def _make_client(self) -> httpx.AsyncClient:
        p = self.provider
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {p.api_key}",
            **p.extra_headers,
        }
        return httpx.AsyncClient(
            timeout=p.request_timeout,
            headers=headers,
        )

    # ── Message Conversion ─────────────────────────────────

    def _to_anthropic_messages(
        self, messages: list[ChatMessage | dict]
    ) -> list[dict]:
        """将统一消息转换为 Anthropic API 格式."""
        result = []
        for msg in messages:
            if isinstance(msg, dict):
                result.append(msg)
                continue

            entry: dict[str, Any] = {"role": msg.role}

            if msg.role == "tool":
                entry.update({
                    "content": msg.content,
                    "tool_call_id": msg.tool_call_id,
                })
            elif msg.role == "assistant" and msg.tool_calls:
                content_blocks = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                entry["content"] = content_blocks
            else:
                entry["content"] = msg.content

            if msg.name:
                entry["name"] = msg.name

            result.append(entry)
        return result

    def _to_anthropic_tools(self, tools: list[ToolDef]) -> list[dict]:
        return [t.to_anthropic() for t in tools]

    # ── 核心: 非流式调用 ───────────────────────────────────

    def _resolve_models(
        self,
        node: Optional[str],
        model: Optional[str],
    ) -> tuple[str, Optional[str]]:
        """解析 (primary, fallback) 模型对。

        优先级: model > node > provider 默认。
        - model 直接指定: 返回 (model, None) — 单模型无降级
        - node 指定: 查 MODEL_ROUTES, 返回 (primary, fallback)
        - 都不指定: 返回 (self.provider.model, None) — 向后兼容
        """
        if model:
            return model, None
        if node:
            primary, fallback = get_model_for_node(node)
            return primary, fallback
        return self.provider.model, None

    async def chat(
        self,
        messages: list[ChatMessage | dict],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        node: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ChatResponse:
        """发送消息, 返回 ChatResponse.

        Args:
            messages: 对话消息列表
            tools: 可用工具列表 (Anthropic tool_use 格式)
            temperature: 采样温度 (默认 0.3)
            max_tokens: 最大 token 数
            system: system prompt (如果 messages 中没有 system 角色)
            node: 路由节点名 — 查 MODEL_ROUTES 得 (primary, fallback),
                主模型失败(异常/超时/限流)自动切 fallback 重试一次
            model: 直接指定模型 ID (优先级高于 node; 单模型, 无降级)

        Returns:
            ChatResponse with content, tool_calls, usage

        多模型降级: 传 node 时 primary 失败自动换 fallback 重试一次,
        两种模型都失败才抛异常。不传 node/model 时用 provider 默认模型 (向后兼容)。
        """
        primary, fallback = self._resolve_models(node, model)
        last_exc: Optional[Exception] = None
        for i, mdl in enumerate((primary, fallback)):
            if mdl is None:
                continue
            try:
                return await self._chat_once(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system=system,
                    model=mdl,
                )
            except Exception as e:
                last_exc = e
                if fallback and i == 0:
                    logger.warning(
                        f"chat: primary model '{primary}' failed "
                        f"({type(e).__name__}: {e}), trying fallback '{fallback}'"
                    )
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def _chat_once(
        self,
        messages: list[ChatMessage | dict],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
        force_tool: bool = False,
    ) -> ChatResponse:
        """单模型 chat — chat() 降级层之下的一次尝试 (含原有重试).

        force_tool=True 时强制 LLM 必须调用 tools[0]（Anthropic tool_choice 硬约束），
        用于结构化输出场景（参见 _chat_json_once）。
        火山方舟若不支持 tool_choice，将由底层 4xx 错误抛出，调用方应回退。
        """
        p = self.provider
        mdl = model if model is not None else p.model
        temp = temperature if temperature is not None else self.default_temperature
        mt = max_tokens if max_tokens is not None else self.max_tokens

        anthropic_msgs = self._to_anthropic_messages(messages)

        payload: dict[str, Any] = {
            "model": mdl,
            "max_tokens": mt,
            "temperature": temp,
            "messages": anthropic_msgs,
        }

        if system:
            payload["system"] = system

        if tools:
            payload["tools"] = self._to_anthropic_tools(tools)
            if force_tool:
                # Anthropic 协议：强制 LLM 必须调用指定 tool（不允许自由文本回复）
                # 让结构化输出可靠性从 ~90% 提升到 ≥99%
                payload["tool_choice"] = {"type": "tool", "name": tools[0].name}

        # 速率限制
        await self._get_rate_limiter().acquire()

        # 带重试的请求
        last_error = None
        for attempt in range(p.max_retries + 1):
            try:
                async with self._make_client() as client:
                    resp = await client.post(
                        f"{p.base_url}/v1/messages",
                        json=payload,
                    )

                    if resp.status_code == 429:
                        # 速率限制 — 使用 Retry-After 或指数退避
                        retry_after = resp.headers.get("Retry-After", str(2 ** attempt))
                        wait = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                        logger.warning(f"Rate limited (429), waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    if resp.status_code >= 500:
                        # 服务器错误 — 指数退避
                        delay = p.retry_base_delay * (2 ** attempt)
                        logger.warning(f"Server error {resp.status_code}, retry {attempt+1}/{p.max_retries} after {delay}s")
                        await asyncio.sleep(delay)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    return self._parse_response(data)

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_error = e
                if attempt < p.max_retries:
                    delay = p.retry_base_delay * (2 ** attempt)
                    logger.warning(f"Network error: {e}, retry {attempt+1}/{p.max_retries} after {delay}s")
                    await asyncio.sleep(delay)
                else:
                    raise

            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise  # 4xx 不重试
                last_error = e
                if attempt < p.max_retries:
                    delay = p.retry_base_delay * (2 ** attempt)
                    logger.warning(f"HTTP {e.response.status_code}, retry {attempt+1}/{p.max_retries}")
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_error  # type: ignore[misc]

    def _parse_response(self, data: dict) -> ChatResponse:
        """解析 Anthropic/兼容 API 响应."""
        content_text = ""
        tool_calls = []

        content = data.get("content", [])
        if isinstance(content, str):
            content_text = content
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    content_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    ))

        usage = data.get("usage", {})
        if isinstance(usage, dict):
            # 转换为统一格式
            pass

        return ChatResponse(
            content=content_text,
            tool_calls=tool_calls,
            stop_reason=data.get("stop_reason", "end_turn"),
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
            model=data.get("model", ""),
            raw=data,
        )

    # ── 流式调用 ───────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[ChatMessage | dict],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        node: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """流式对话 — 逐块返回 delta. 支持多模型降级 (传 node 时).

        Yields:
            {"type": "text_delta", "text": "..."}
            {"type": "tool_use_start", "id": "...", "name": "..."}
            {"type": "tool_use_delta", "id": "...", "input": {...}}
            {"type": "message_stop", "stop_reason": "..."}
            {"type": "error", "message": "..."}

        多模型降级: 传 node 时, 若 primary 在产出任何有效输出前就报错
        (网络/超时/5xx 重试耗尽), 自动切 fallback 重试一次。一旦 primary
        已产出有效 delta, 后续错误不再切换 (避免拼接错乱)。
        """
        primary, fallback = self._resolve_models(node, model)
        models_to_try = [m for m in (primary, fallback) if m]

        last_error_msg: Optional[str] = None
        for i, mdl in enumerate(models_to_try):
            is_last = i == len(models_to_try) - 1
            produced_output = False
            had_early_error = False
            try:
                async for chunk in self._chat_stream_once(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system=system,
                    model=mdl,
                ):
                    if (
                        isinstance(chunk, dict)
                        and chunk.get("type") == "error"
                        and not produced_output
                        and not is_last
                    ):
                        # primary 在产出输出前就报错 → 切 fallback
                        had_early_error = True
                        last_error_msg = chunk.get("message", "")
                        logger.warning(
                            f"chat_stream: model '{mdl}' errored before output "
                            f"({last_error_msg}), trying next model"
                        )
                        break
                    produced_output = True
                    yield chunk
            except Exception as e:
                if not produced_output and not is_last:
                    had_early_error = True
                    last_error_msg = str(e)
                    logger.warning(
                        f"chat_stream: model '{mdl}' raised ({type(e).__name__}: {e}), "
                        f"trying next model"
                    )
                else:
                    yield {"type": "error", "message": str(e)}
                    return

            if not had_early_error:
                return  # 成功完成 (或已把错误 yield 给上游)

        # 所有模型都在产出前失败
        yield {"type": "error", "message": last_error_msg or "all models failed"}

    async def _chat_stream_once(
        self,
        messages: list[ChatMessage | dict],
        tools: Optional[list[ToolDef]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """单模型 chat_stream — chat_stream() 降级层之下的一次尝试 (含原有重试)."""
        p = self.provider
        mdl = model if model is not None else p.model
        temp = temperature if temperature is not None else self.default_temperature
        mt = max_tokens if max_tokens is not None else self.max_tokens

        anthropic_msgs = self._to_anthropic_messages(messages)

        payload: dict[str, Any] = {
            "model": mdl,
            "max_tokens": mt,
            "temperature": temp,
            "messages": anthropic_msgs,
            "stream": True,
        }

        if system:
            payload["system"] = system

        if tools:
            payload["tools"] = self._to_anthropic_tools(tools)

        last_error = None

        for attempt in range(p.max_retries + 1):
            try:
                await self._get_rate_limiter().acquire()

                async with self._make_client() as client:
                    async with client.stream(
                        "POST",
                        f"{p.base_url}/v1/messages",
                        json=payload,
                    ) as response:
                        if response.status_code == 429:
                            retry_after = response.headers.get("Retry-After", str(2 ** attempt))
                            wait = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                            logger.warning(f"Stream rate limited (429), waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue

                        if response.status_code >= 500:
                            delay = p.retry_base_delay * (2 ** attempt)
                            logger.warning(f"Stream server error {response.status_code}, retry {attempt+1}")
                            await asyncio.sleep(delay)
                            continue

                        response.raise_for_status()

                        parser = SSEParser()
                        tool_use_buffers: dict[str, dict] = {}

                        async for line in response.aiter_lines():
                            if not line:
                                continue

                            events = parser.feed(line + "\n")
                            for event in events:
                                if event.get("type") == "done":
                                    yield {"type": "message_stop", "stop_reason": "end_turn"}
                                    continue

                                delta_type = event.get("type")

                                if delta_type == "content_block_delta":
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        yield {"type": "text_delta", "text": delta.get("text", "")}
                                    elif delta.get("type") == "input_json_delta":
                                        idx = event.get("index", 0)
                                        if idx not in tool_use_buffers:
                                            tool_use_buffers[idx] = {"partial_json": ""}
                                        tool_use_buffers[idx]["partial_json"] += delta.get("partial_json", "")

                                elif delta_type == "content_block_start":
                                    block = event.get("content_block", {})
                                    if block.get("type") == "tool_use":
                                        idx = event.get("index", 0)
                                        tool_use_buffers[idx] = {
                                            "id": block.get("id", ""),
                                            "name": block.get("name", ""),
                                            "partial_json": "",
                                        }
                                        yield {
                                            "type": "tool_use_start",
                                            "id": block.get("id", ""),
                                            "name": block.get("name", ""),
                                        }

                                elif delta_type == "content_block_stop":
                                    idx = event.get("index", 0)
                                    if idx in tool_use_buffers:
                                        buf = tool_use_buffers[idx]
                                        try:
                                            input_data = json.loads(buf["partial_json"])
                                        except json.JSONDecodeError:
                                            input_data = {}
                                        yield {
                                            "type": "tool_use_delta",
                                            "id": buf.get("id", ""),
                                            "name": buf.get("name", ""),
                                            "input": input_data,
                                        }

                                elif delta_type == "message_stop":
                                    yield {"type": "message_stop", "stop_reason": "end_turn"}

                                elif delta_type == "error":
                                    yield {"type": "error", "message": str(event.get("error", event))}

                        return  # 成功完成流式响应

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_error = e
                if attempt < p.max_retries:
                    delay = p.retry_base_delay * (2 ** attempt)
                    logger.warning(f"Stream network error: {e}, retry {attempt+1}/{p.max_retries}")
                    await asyncio.sleep(delay)
                else:
                    yield {"type": "error", "message": f"Stream failed after {p.max_retries} retries: {e}"}

            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    yield {"type": "error", "message": f"Stream HTTP {e.response.status_code}: {e}"}
                    return
                last_error = e
                if attempt < p.max_retries:
                    delay = p.retry_base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                else:
                    yield {"type": "error", "message": f"Stream failed: {e}"}

        if last_error:
            yield {"type": "error", "message": str(last_error)}

    # ── 结构化 JSON 输出 ───────────────────────────────────

    async def chat_json(
        self,
        messages: list[ChatMessage | dict],
        schema: Optional[type | dict] = None,
        temperature: float = 0.1,
        system: Optional[str] = None,
        node: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """结构化 JSON 输出 — 通过 tool_use 强制输出符合 schema 的 JSON.

        Args:
            messages: 对话消息
            schema: Pydantic model 或 JSON Schema dict
            temperature: 低温度确保确定性
            system: system prompt
            node: 路由节点名 — 查 MODEL_ROUTES 得 (primary, fallback)
            model: 直接指定模型 ID (优先级高于 node; 单模型无降级)

        Returns:
            解析后的 dict

        多模型降级: 传 node 时, primary 失败 (异常/超时/限流/JSON 校验失败)
        自动切 fallback 重试一次。JSON 校验失败指返回 {"error": ...} (parse
        失败或重试耗尽)。两种模型都失败才放弃 (返回最后一个 error dict 或抛异常)。
        """
        primary, fallback = self._resolve_models(node, model)
        last_exc: Optional[Exception] = None
        last_result: Optional[dict] = None

        for i, mdl in enumerate((primary, fallback)):
            if mdl is None:
                continue
            try:
                result = await self._chat_json_once(
                    messages=messages,
                    schema=schema,
                    temperature=temperature,
                    system=system,
                    model=mdl,
                )
                # JSON 校验失败 (parse_failed / max_retries_exceeded) → 触发降级
                if (
                    isinstance(result, dict)
                    and "error" in result
                    and fallback
                    and i == 0
                ):
                    logger.warning(
                        f"chat_json: primary model '{primary}' returned error "
                        f"({result.get('error')}), trying fallback '{fallback}'"
                    )
                    last_result = result
                    continue
                return result
            except Exception as e:
                last_exc = e
                if fallback and i == 0:
                    logger.warning(
                        f"chat_json: primary model '{primary}' failed "
                        f"({type(e).__name__}: {e}), trying fallback '{fallback}'"
                    )
                    continue
                raise

        # fallback 也失败: 优先返回 error dict (让上游走 fail-closed), 否则抛异常
        if last_result is not None:
            return last_result
        raise last_exc  # type: ignore[misc]

    async def _chat_json_once(
        self,
        messages: list[ChatMessage | dict],
        schema: Optional[type | dict] = None,
        temperature: float = 0.1,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """单模型 chat_json — chat_json() 降级层之下的一次尝试 (含原有重试)."""
        # 构建 schema
        json_schema = self._resolve_schema(schema)

        # 如果没有显式的 schema, 使用 text 模式并提取 JSON
        if json_schema is None:
            p = self.provider
            mdl = model if model is not None else p.model
            anthropic_msgs = self._to_anthropic_messages(messages)
            payload = {
                "model": mdl,
                "max_tokens": self.max_tokens,
                "temperature": temperature,
                "messages": anthropic_msgs,
            }
            if system:
                payload["system"] = system

            last_error = None
            for attempt in range(p.max_retries + 1):
                try:
                    await self._get_rate_limiter().acquire()
                    async with self._make_client() as client:
                        resp = await client.post(
                            f"{p.base_url}/v1/messages",
                            json=payload,
                        )

                        if resp.status_code == 429:
                            retry_after = resp.headers.get("Retry-After", str(2 ** attempt))
                            wait = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                            logger.warning(f"chat_json rate limited (429), waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue

                        if resp.status_code >= 500:
                            delay = p.retry_base_delay * (2 ** attempt)
                            logger.warning(f"chat_json server error {resp.status_code}, retry {attempt+1}")
                            await asyncio.sleep(delay)
                            continue

                        resp.raise_for_status()
                        data = resp.json()
                        text = self._extract_text(data)
                        return self._parse_json(text)

                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                    last_error = e
                    if attempt < p.max_retries:
                        delay = p.retry_base_delay * (2 ** attempt)
                        logger.warning(f"chat_json network error: {e}, retry {attempt+1}/{p.max_retries}")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"chat_json failed after {p.max_retries} retries: {e}")

                except httpx.HTTPStatusError as e:
                    if e.response.status_code < 500:
                        raise
                    last_error = e
                    if attempt < p.max_retries:
                        delay = p.retry_base_delay * (2 ** attempt)
                        await asyncio.sleep(delay)

            return {"error": "max_retries_exceeded", "detail": str(last_error) if last_error else "unknown"}

        # 使用 tool_use 强制结构化输出 (走 _chat_once 避免嵌套降级)
        tool = ToolDef(
            name="output",
            description="输出结构化结果",
            input_schema=json_schema,
        )

        response = await self._chat_once(
            messages=messages,
            tools=[tool],
            temperature=temperature,
            system=system,
            model=model,
            force_tool=True,  # ★ Anthropic tool_choice 硬强制，确保 LLM 必返结构化数据
        )

        if response.tool_calls:
            return response.tool_calls[0].arguments
        else:
            # 降级: 尝试从文本提取 JSON
            return self._parse_json(response.content)

    def _resolve_schema(self, schema: type | dict | None) -> Optional[dict]:
        """将 schema 转换为 JSON Schema dict."""
        if schema is None:
            return None
        if isinstance(schema, dict):
            return schema
        # Pydantic model
        if hasattr(schema, "model_json_schema"):
            return schema.model_json_schema()
        if hasattr(schema, "schema"):
            return schema.schema()
        return None

    def _extract_text(self, data: dict) -> str:
        """从 API 响应提取文本."""
        content = data.get("content", [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            )
        return str(content)

    def _parse_json(self, text: str) -> dict:
        """从文本中提取 JSON."""
        text = text.strip()
        # 移除 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取第一个 JSON 对象
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            # 尝试提取数组
            match = re.search(r"\[[\s\S]*\]", text)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            logger.warning(f"Failed to parse JSON from: {text[:300]}")
            return {"error": "parse_failed", "raw": text[:500]}

    # ── 高级: 对话 + 工具循环 ──────────────────────────────

    async def chat_with_tools(
        self,
        messages: list[ChatMessage | dict],
        tools: list[ToolDef],
        system: Optional[str] = None,
        max_tool_rounds: int = 5,
        temperature: Optional[float] = None,
    ) -> ChatResponse:
        """对话 + 工具调用循环 — 自动执行工具并继续对话.

        Args:
            messages: 初始对话消息
            tools: 可用工具列表
            system: system prompt
            max_tool_rounds: 最大工具调用轮数 (防止无限循环)
            temperature: 温度

        Returns:
            最终的 ChatResponse (无 tool_calls 或达到上限)
        """
        current_msgs = list(messages)

        for _ in range(max_tool_rounds):
            response = await self.chat(
                messages=current_msgs,
                tools=tools,
                temperature=temperature,
                system=system,
            )

            if not response.tool_calls:
                return response

            # 添加 assistant 消息
            current_msgs.append(ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))

            # 执行每个工具调用
            for tc in response.tool_calls:
                tool = next((t for t in tools if t.name == tc.name), None)
                if tool and tool.handler:
                    try:
                        result = await tool.handler(**tc.arguments) if asyncio.iscoroutinefunction(tool.handler) else tool.handler(**tc.arguments)
                        result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)})
                else:
                    result_str = json.dumps({"error": f"Tool '{tc.name}' not found or no handler"})

                current_msgs.append(ChatMessage(
                    role="tool",
                    content=result_str,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))

        logger.warning(f"Max tool rounds ({max_tool_rounds}) reached")
        return response

    # ── 便捷方法 (向后兼容) ─────────────────────────────────

    async def parse_intent(self, user_query: str) -> SearchIntent:
        """Stage 1: 解析用户自然语言为结构化搜索意图."""
        today = datetime.now()
        result = await self.chat_json(
            messages=[ChatMessage(role="user", content=(
                f"当前日期: {today.strftime('%Y-%m-%d')}\n"
                f"用户搜索: {user_query}\n\n"
                f"请解析搜索意图。"
            ))],
            system=self.INTENT_SYSTEM_PROMPT,
        )

        if "error" in result:
            return SearchIntent(
                original_query=user_query,
                sub_queries=[user_query],
                sources=["arxiv", "semantic_scholar"],
            )

        return SearchIntent(
            original_query=user_query,
            sub_queries=result.get("sub_queries", [user_query]),
            year_from=result.get("year_from"),
            year_to=result.get("year_to"),
            sources=result.get("sources", ["arxiv", "semantic_scholar"]),
            entities=result.get("entities", []),
            domain_hint=result.get("domain_hint", ""),
        )

    async def evaluate_relevance(self, paper, user_query: str) -> RelevanceJudgment:
        """Stage 4: 评估单篇论文的相关性."""
        result = await self.chat_json(
            messages=[ChatMessage(role="user", content=(
                f"用户研究需求: {user_query}\n\n"
                f"论文标题: {paper.title}\n"
                f"作者: {', '.join(paper.authors[:5])}\n"
                f"年份: {paper.year or '未知'}\n"
                f"期刊/会议: {paper.venue or '未知'}\n"
                f"摘要: {(paper.abstract or '无')[:500]}\n"
            ))],
            system=self.RELEVANCE_SYSTEM_PROMPT,
        )

        if "error" in result:
            # L4 fail-closed：评估失败 → 默认不相关（不再保留垃圾论文进入语料库）
            logger.warning(
                f"相关性评估失败 ({result.get('error')}), FAIL-CLOSED → is_relevant=False (剔除该篇)"
            )
            return RelevanceJudgment(
                score=0.0,
                reason=f"评估失败 ({result.get('error', 'unknown')})，按不相关处理",
                is_relevant=False,
            )

        return RelevanceJudgment(
            score=float(result.get("score", 0.5)),
            reason=result.get("reason", ""),
            is_relevant=result.get("is_relevant", True),
        )

    async def evaluate_batch(
        self, papers: list, user_query: str, max_concurrent: int = 5,
        batch_timeout: float = 300.0,
    ) -> list[RelevanceJudgment]:
        """并发评估多篇论文（含整体超时保护）。

        Args:
            papers: 论文列表
            user_query: 用户查询
            max_concurrent: 最大并发数
            batch_timeout: 整体超时秒数 (默认 5 分钟)
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def evaluate_one(paper):
            async with semaphore:
                try:
                    return await self.evaluate_relevance(paper, user_query)
                except Exception as e:
                    logger.warning(f"Evaluate failed for {getattr(paper, 'title', '?')}: {e}")
                    return RelevanceJudgment(score=0.0, reason=f"评估失败 ({type(e).__name__})，按不相关处理", is_relevant=False)

        try:
            return await asyncio.wait_for(
                asyncio.gather(*[evaluate_one(p) for p in papers]),
                timeout=batch_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"evaluate_batch timeout after {batch_timeout}s for {len(papers)} papers, FAIL-CLOSED")
            # L4 fail-closed：整体超时 → 全部按不相关处理，避免把未评估的论文当合格品入库
            return [RelevanceJudgment(score=0.0, reason="批量评估超时，按不相关处理", is_relevant=False) for _ in papers]

    async def should_continue_search(
        self,
        user_query: str,
        current_round: int,
        total_found: int,
        relevant_count: int,
        sample_titles: list[str],
    ) -> ContinueDecision:
        """Stage 3: 判断是否需要继续搜索."""
        result = await self.chat_json(
            messages=[ChatMessage(role="user", content=(
                f"用户原始需求: {user_query}\n"
                f"当前搜索轮次: 第 {current_round} 轮\n"
                f"共找到论文: {total_found} 篇\n"
                f"其中相关论文: {relevant_count} 篇\n"
                f"相关论文标题样本:\n" + "\n".join(f"- {t}" for t in sample_titles[:10])
            ))],
            system=self.CONTINUE_SYSTEM_PROMPT,
        )

        if "error" in result:
            return ContinueDecision(should_continue=False, reason="判断失败，停止搜索")

        return ContinueDecision(
            should_continue=result.get("should_continue", False),
            reason=result.get("reason", ""),
            new_queries=result.get("new_queries", []),
            new_sources=result.get("new_sources", []),
        )

    async def extract_digest(self, paper, journal_level: str = None) -> dict:
        """提取论文关键要点和方法标签."""
        result = await self.chat_json(
            messages=[ChatMessage(role="user", content=(
                f"标题: {paper.title}\n"
                f"作者: {', '.join(paper.authors[:5])}\n"
                f"年份: {paper.year or '?'}\n"
                f"期刊/会议: {paper.venue or '未知'} (等级: {journal_level or '未评级'})\n"
                f"摘要: {(paper.abstract or '无')[:800]}\n"
            ))],
            system=self.DIGEST_SYSTEM_PROMPT,
        )

        if "error" in result:
            return {"digest": [], "one_liner": "", "method_tags": [], "dataset_info": "", "reading_level": "skim"}

        return result

    async def generate_report(
        self,
        user_query: str,
        papers: list,
        judgments: list,
        db=None,
        project_id: Optional[str] = None,
    ) -> str:
        """Stage 6: 生成搜索报告.

        L2 反幻觉：传入 db 时，对生成的报告做 CitationVerifier 引用校验
        （parse + match，不做 fact-check 以控成本）。校验失败的引用会被标记
        ⚠️[verify] 或删除，报告末尾附审计段。
        """
        scored = sorted(
            zip(papers, judgments),
            key=lambda x: x[1].score,
            reverse=True,
        )

        paper_summaries = []
        for p, j in scored[:30]:
            paper_summaries.append(
                f"- [{j.score:.2f}] {p.title} ({p.year or '?'}) | {p.source.value} | {p.venue or ''}\n"
                f"  理由: {j.reason}"
            )

        try:
            response = await self.chat(
                messages=[ChatMessage(role="user", content=(
                    f"用户搜索: {user_query}\n\n"
                    f"搜索结果 (共 {len(papers)} 篇，展示前30):\n" + "\n".join(paper_summaries)
                ))],
                system=self.REPORT_SYSTEM_PROMPT,
                temperature=0.5,
            )
            report = response.content
        except Exception as e:
            logger.error(f"报告生成失败 ({type(e).__name__})", exc_info=True)
            return (
                f"# 搜索报告\n\n搜索: {user_query}\n找到 {len(papers)} 篇论文\n\n"
                f"(报告生成失败：{type(e).__name__}，详见服务日志)"
            )

        # L2 反幻觉：CitationVerifier 校验引用
        if db is not None:
            from .verifier import verify_and_wrap_report
            report = await verify_and_wrap_report(report, db, project_id)

        return report
