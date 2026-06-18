"""API Key 认证 — 简单 Bearer Token。

配置:
  API_KEY=your-secret-key  (.env)

用法:
  from .auth import verify_api_key
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)
API_KEY = os.getenv("API_KEY", "")


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """验证 Bearer Token。"""
    if not API_KEY:
        # 未配置 API_KEY → 开放访问
        return "anonymous"

    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing API key")

    if credentials.credentials != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

    return credentials.credentials
