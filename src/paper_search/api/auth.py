"""认证模块 — JWT 认证 (REST + WebSocket + SSE)。

v4.2: 移除 API_KEY 兜底模式，统一使用 JWT。
  - REST/SSE: Authorization: Bearer <jwt> header
  - WebSocket: ?token=<jwt> query param (浏览器 WebSocket API 不支持自定义 header)

配置 (环境变量):
  JWT_SECRET                 — JWT 签名密钥 (必需)
  JWT_ALGORITHM=HS256        — 签名算法
  ACCESS_TOKEN_EXPIRE_MINUTES=30      — access token 过期时间
  REFRESH_TOKEN_EXPIRE_DAYS=7         — refresh token 过期时间
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Query, Request, Security, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import PyJWTError

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

# ── JWT 配置 ────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "agent-user-default")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))


def _hash_password(password: str) -> str:
    """SHA-256 哈希密码（生产环境应使用 bcrypt）。"""
    salt = os.urandom(16).hex()
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"sha256:{salt}:{h}"


def _verify_password(password: str, password_hash: str) -> bool:
    """验证密码哈希。"""
    if not password_hash or ":" not in password_hash:
        return False
    try:
        algo, salt, h = password_hash.split(":", 2)
        if algo != "sha256":
            return False
        expected = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return h == expected
    except Exception:
        return False


def _create_jwt(user_id: str, username: str, token_type: str = "access", agent_id: str = None) -> str:
    """创建 JWT token。v3.2: 可选 agent_id 表示客户端当前活跃智能体。"""
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET not configured")
    now = datetime.now(timezone.utc)
    if token_type == "access":
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    else:
        expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode = {
        "sub": user_id,
        "username": username,
        "type": token_type,
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    if agent_id:
        to_encode["agent_id"] = agent_id
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_jwt(token: str) -> dict:
    """解码并验证 JWT token。返回 payload dict。"""
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET not configured")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")


# ═══════════════════════════════════════════════════════════════
# 共用 JWT 校验 — REST + WebSocket 复用
# ═══════════════════════════════════════════════════════════════


def _validate_jwt(token: str) -> str:
    """校验 JWT access token → 返回 user_id。

    REST (verify_api_key) 和 WebSocket (verify_ws_token) 共用。
    """
    payload = _decode_jwt(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")
    return payload["sub"]


# ═══════════════════════════════════════════════════════════════
# REST / SSE 鉴权 — router 级依赖
# ═══════════════════════════════════════════════════════════════


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """JWT Bearer Token 验证 → 返回 user_id，注入 request.state.user_id。

    router 级依赖：所有 /api/* 端点到达时已完成鉴权。
    FastAPI 依赖缓存：同一请求多次 Depends 只执行一次。
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing token")

    token = credentials.credentials
    user_id = _validate_jwt(token)
    request.state.user_id = user_id
    return user_id


# ═══════════════════════════════════════════════════════════════
# WebSocket 鉴权
# ═══════════════════════════════════════════════════════════════


async def verify_ws_token(
    websocket: WebSocket,
    agent_id: str,
    token: Optional[str] = None,
) -> tuple[str, str]:
    """WebSocket JWT 验证，返回 (user_id, resolved_agent_id)。

    浏览器 WebSocket API 不支持自定义 header，token 通过 ?token=<jwt> 传递。
    JWT 校验逻辑与 REST 完全一致（共用 _validate_jwt）。
    """
    from ..agent.pgdb import PostgresAgentDB

    if not token:
        await websocket.close(code=4001, reason="Missing JWT token")
        raise HTTPException(status_code=401, detail="Missing JWT token")

    user_id = _validate_jwt(token)

    # JWT 有 agent_id → 验证归属
    payload = _decode_jwt(token)
    jwt_agent_id = payload.get("agent_id", "")
    if jwt_agent_id:
        db = PostgresAgentDB()
        if not db.agent_belongs_to_user(jwt_agent_id, user_id):
            await websocket.close(code=4003, reason="agent_id does not belong to this user")
            raise HTTPException(status_code=403, detail="agent_id not owned by user")
        return user_id, jwt_agent_id

    # JWT 无 agent_id → 取用户默认 agent
    db = PostgresAgentDB()
    default_agent = db.get_default_agent(user_id)
    if default_agent:
        return user_id, default_agent["id"]

    # 兼容旧格式: URL 中的 agent_id
    if agent_id.startswith("agent-"):
        if not db.agent_belongs_to_user(agent_id, user_id):
            await websocket.close(code=4003, reason="agent_id mismatch with JWT")
            raise HTTPException(status_code=403, detail="agent_id mismatch")
        return user_id, agent_id

    await websocket.close(code=4003, reason="No valid agent found")
    raise HTTPException(status_code=403, detail="No valid agent")


# ═══════════════════════════════════════════════════════════════
# 超级管理员依赖
# ═══════════════════════════════════════════════════════════════


async def verify_super_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """验证超级管理员权限 → 返回 user_id。"""
    user_id = await verify_api_key(request, credentials)
    from ..agent.pgdb import PostgresAgentDB
    db = PostgresAgentDB()
    user = db.get_user(user_id)
    if not user or user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user_id


# ═══════════════════════════════════════════════════════════════
# 认证业务逻辑
# ═══════════════════════════════════════════════════════════════


def auth_register(username: str, password: str, display_name: str = "") -> dict:
    """注册新用户 → 自动创建默认智能体 → 返回 tokens + agent_id。"""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="username must be >= 3 chars")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be >= 6 chars")

    from ..agent.pgdb import PostgresAgentDB
    db = PostgresAgentDB()

    # 检查用户名是否已存在
    existing = db.get_user_by_username(username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    pwd_hash = _hash_password(password)
    display = display_name or username
    role = "super_admin" if username == "wcc" else "researcher"
    user_id = db.create_user(username, display, role=role, password_hash=pwd_hash)

    agent_id = db.create_agent(user_id=user_id)

    access_token = _create_jwt(user_id, username, "access", agent_id=agent_id)
    refresh_token = _create_jwt(user_id, username, "refresh", agent_id=agent_id)

    return {
        "user_id": user_id,
        "username": username,
        "display_name": display,
        "agent_id": agent_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def auth_login(username: str, password: str) -> dict:
    """用户登录 → 返回 tokens + 默认 agent_id。
    若用户无 agent 则自动创建；wcc 自动升级为 super_admin。
    """
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    from ..agent.pgdb import PostgresAgentDB
    db = PostgresAgentDB()

    user = db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is deactivated")

    pwd_hash = user.get("password_hash", "")
    if not _verify_password(password, pwd_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if username == "wcc" and user.get("role") != "super_admin":
        db.set_user_role(user["id"], "super_admin")

    agent_id = None
    default_agent = db.get_default_agent(user["id"])
    if default_agent:
        agent_id = default_agent["id"]
    else:
        agent_id = db.create_agent(user_id=user["id"])

    access_token = _create_jwt(user["id"], username, "access", agent_id=agent_id)
    refresh_token = _create_jwt(user["id"], username, "refresh", agent_id=agent_id)

    return {
        "user_id": user["id"],
        "username": username,
        "display_name": user.get("display_name", username),
        "agent_id": agent_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def auth_refresh(refresh_token: str) -> dict:
    """使用 refresh_token 获取新的 access_token + agent_id。"""
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token required")

    payload = _decode_jwt(refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token must be refresh token")

    user_id = payload["sub"]
    username = payload.get("username", "")
    agent_id = payload.get("agent_id")
    new_access = _create_jwt(user_id, username, "access", agent_id=agent_id)
    new_refresh = _create_jwt(user_id, username, "refresh", agent_id=agent_id)

    return {
        "user_id": user_id,
        "username": username,
        "agent_id": agent_id,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
    }
