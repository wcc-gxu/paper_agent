"""API Key 认证 — 简单 Bearer Token，支持多用户。

配置:
  API_KEY=your-secret-key  (.env)  — 设置后启用认证模式
  未设置 → 开放访问，所有请求使用 "anonymous" 用户

多用户模式:
  设置 API_KEY 后，系统从 users 表中按 api_token 查找用户。
  默认用户 "user-default" 的 token 为 "tok-migrated-default"。
  可通过 POST /api/users 创建新用户并获取 token。

用法:
  from .auth import verify_api_key
  user_id: str = Depends(verify_api_key)
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)
API_KEY = os.getenv("API_KEY", "")


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """验证 Bearer Token，返回 user_id。

    行为:
    - API_KEY 未设置 → 返回 "anonymous"（开放访问模式）
    - API_KEY 已设置 + 有效 token → 返回对应的 user_id
    - API_KEY 已设置 + 无效/缺失 token → 401/403
    """
    if not API_KEY:
        # 未配置 API_KEY → 开放访问，所有请求使用 anonymous 用户
        return "anonymous"

    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing API key")

    token = credentials.credentials

    # 向后兼容：如果 token 等于 API_KEY，返回默认用户
    if token == API_KEY:
        return "user-default"

    # 多用户模式：从 users 表中查找
    try:
        from ..config import use_postgresql

        if use_postgresql():
            from ..agent.pgdb import PostgresAgentDB
            db = PostgresAgentDB()
        else:
            from ..agent.db import AgentDB
            db = AgentDB()

        user = db.get_user_by_token(token)
        if user:
            return user["id"]
    except Exception as e:
        logger.warning(f"Auth lookup failed: {e}")
        # 如果 DB 不可用，回退到单 token 模式
        if token == API_KEY:
            return "user-default"

    raise HTTPException(status_code=403, detail="Invalid API key")
