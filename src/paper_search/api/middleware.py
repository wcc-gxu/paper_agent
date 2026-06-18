"""速率限制中间件 — 简单滑动窗口。

配置:
  RATE_LIMIT_REQUESTS=100  (每分钟最大请求数，默认 100)
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

RATE_LIMIT = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """简单滑动窗口速率限制。"""

    def __init__(self, app, max_requests: int = RATE_LIMIT):
        super().__init__(app)
        self._max = max_requests
        self._windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        # 跳过 WebSocket
        if request.url.path.startswith("/ws"):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - 60.0

        # 清理旧记录
        window = [t for t in self._windows[client] if t > cutoff]
        self._windows[client] = window

        if len(window) >= self._max:
            logger.warning(f"Rate limit exceeded for {client}")
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests", "retry_after": 60},
            )

        window.append(now)
        return await call_next(request)
