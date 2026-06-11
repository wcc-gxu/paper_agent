"""Provider 注册表 — 管理所有论文搜索来源."""

from .base import BaseProvider
from ..models import SourceType

_registry: dict[SourceType, type[BaseProvider]] = {}


def register(source_type: SourceType):
    """装饰器：将 Provider 类注册到注册表。"""
    def decorator(cls: type[BaseProvider]):
        _registry[source_type] = cls
        return cls
    return decorator


def get_provider(source_type: SourceType, **kwargs) -> BaseProvider:
    """根据来源类型获取 Provider 实例。"""
    cls = _registry.get(source_type)
    if cls is None:
        raise ValueError(f"未知来源: {source_type}")
    return cls(**kwargs)


def list_providers() -> list[SourceType]:
    """返回所有已注册的来源类型。"""
    return list(_registry.keys())
