"""AgentError — 统一错误信封，跨 agent 边界传递错误上下文。

所有 agent（主 agent + 子 agent）在发生异常时都应创建 AgentError，
通过 Reporter.publish_agent_error() 推送到 Redis Pub/Sub，
主 agent daemon 消费后透传给用户。

用法::

    from .agent_error import AgentError
    import traceback

    try:
        ...
    except Exception as e:
        err = AgentError.from_exception(
            agent="KnowledgeAgent",
            node="_search_node",
            exception=e,
            context={"question": question, "top_k": top_k},
            recoverable=True,
            retry_count=state.get("retrieval_rounds", 0),
            max_retries=state.get("max_rounds", 3),
        )
        # 发布到 Redis
        reporter.publish_agent_error(agent_id, err)
        # 或直接返回给调用方
        return {"error": err.to_dict()}
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AgentError:
    """所有 agent 内部异常的标准化信封。

    Attributes:
        agent: 发生错误的 agent 标识。格式建议:
               - 子 agent: "KnowledgeAgent", "WritingAgent", ...
               - 主 agent 节点: "MainGraph/execute", "MainGraph/plan", ...
               - 工具: "tool:agent_knowledge_ask", "tool:agent_search_papers", ...
        node: 发生错误的节点/函数名。如 "_search_node", "_dispatch_tool", ...
        error_type: 异常类型。如 "TypeError", "ValueError", "HTTPStatusError"
        message: 原始异常消息 str(exc)
        traceback: 完整 traceback (traceback.format_exc())
        context: 关键输入参数。如 {"question": "...", "top_k": None}
        timestamp: ISO 8601 时间戳
        recoverable: 重试可能有效？
        retry_count: 当前已重试次数
        max_retries: 最大允许重试次数
    """

    agent: str
    node: str
    error_type: str
    message: str
    traceback: str = ""
    context: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    recoverable: bool = False
    retry_count: int = 0
    max_retries: int = 0

    # ── 工厂方法 ──────────────────────────────────────────────

    @classmethod
    def from_exception(
        cls,
        agent: str,
        node: str,
        exception: Exception,
        context: dict | None = None,
        recoverable: bool = False,
        retry_count: int = 0,
        max_retries: int = 0,
    ) -> "AgentError":
        """从异常对象创建 AgentError。

        Args:
            agent: agent 标识
            node: 节点/函数名
            exception: 原始异常对象
            context: 关键输入参数
            recoverable: 重试可能有效？
            retry_count: 已重试次数
            max_retries: 最大重试次数
        """
        return cls(
            agent=agent,
            node=node,
            error_type=type(exception).__name__,
            message=str(exception),
            traceback=traceback.format_exc(),
            context=context or {},
            recoverable=recoverable,
            retry_count=retry_count,
            max_retries=max_retries,
        )

    # ── 序列化 ────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """序列化为 JSON-safe dict。"""
        return {
            "agent": self.agent,
            "node": self.node,
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback,
            "context": self.context,
            "timestamp": self.timestamp,
            "recoverable": self.recoverable,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentError":
        """从 JSON-safe dict 反序列化。"""
        return cls(
            agent=data.get("agent", "unknown"),
            node=data.get("node", "unknown"),
            error_type=data.get("error_type", "Exception"),
            message=data.get("message", ""),
            traceback=data.get("traceback", ""),
            context=data.get("context", {}),
            timestamp=data.get("timestamp", _now()),
            recoverable=data.get("recoverable", False),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 0),
        )

    # ── 格式化 ────────────────────────────────────────────────

    def format_for_user(self) -> str:
        """生成面向用户的可读错误消息。

        包含: agent.node → error_type → message → 重试信息 → 建议

        Example output:
            KnowledgeAgent._search_node 出错 (TypeError):
            unsupported operand type(s) for *: 'NoneType' and 'int'
            → 输入: top_k=None, question="对抗性攻击是什么？"
            → 已重试 5 次（最大 3 次），已停止
        """
        parts = [
            f"**{self.agent}.{self.node}** 出错 ({self.error_type}):",
            f"```\n{self.message}\n```",
        ]

        # 添加上下文
        if self.context:
            ctx_parts = []
            for k, v in self.context.items():
                val_str = str(v)
                if len(val_str) > 100:
                    val_str = val_str[:100] + "..."
                ctx_parts.append(f"{k}={val_str}")
            if ctx_parts:
                parts.append(f"→ 输入: {', '.join(ctx_parts)}")

        # 添加重试信息
        if self.retry_count > 0 or self.max_retries > 0:
            if self.retry_count >= self.max_retries:
                parts.append(
                    f"→ 已重试 {self.retry_count} 次（最大 {self.max_retries} 次），已停止"
                )
            else:
                parts.append(
                    f"→ 已重试 {self.retry_count}/{self.max_retries} 次"
                )

        # 添加不可恢复标记
        if not self.recoverable and self.retry_count >= self.max_retries:
            parts.append("→ 此错误不可恢复，请检查配置或数据")

        return "\n".join(parts)

    def format_for_log(self) -> str:
        """生成面向日志的完整错误消息（含 traceback）。"""
        lines = [
            f"[AgentError] agent={self.agent} node={self.node} "
            f"type={self.error_type} recoverable={self.recoverable} "
            f"retry={self.retry_count}/{self.max_retries}",
            f"  message: {self.message}",
        ]
        if self.context:
            lines.append(f"  context: {self.context}")
        if self.traceback:
            lines.append(f"  traceback:\n{self.traceback}")
        return "\n".join(lines)
