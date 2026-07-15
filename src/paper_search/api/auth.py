"""认证模块 — JWT (首选) + Bearer Token (兼容)。

JWT 认证:
  - POST /api/auth/register → 注册 + 返回 access_token
  - POST /api/auth/login    → 登录 + 返回 access_token
  - WebSocket /ws/chat/{agent_id}/{session_id}?token=<jwt>  → 验证 JWT

Bearer Token (兼容):
  - API_KEY=xxx  (.env) → 静态 API Key 模式
  - 多用户: users 表 api_token 字段

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

from fastapi import HTTPException, Query, Security, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import PyJWTError

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)
API_KEY = os.getenv("API_KEY", "")

# ── JWT 配置 ────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "")
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
# REST API 依赖 — Bearer Token / JWT 验证
# ═══════════════════════════════════════════════════════════════


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """验证 Bearer Token，返回 user_id。

    同时支持 JWT 和静态 API Key。
    行为:
    - JWT_SECRET 已设置 + Bearer JWT → 解码 JWT 返回 user_id
    - API_KEY 未设置 → 返回 "anonymous"（开放访问模式）
    - API_KEY 已设置 + token==API_KEY → 返回 "user-default"
    - API_KEY 已设置 + DB 多用户 → 查找 users 表
    - 否则 → 403
    """
    # 优先 JWT 验证
    if credentials is not None:
        token = credentials.credentials
        if JWT_SECRET and token.count(".") == 2:
            # 看起来像 JWT（header.payload.signature）
            try:
                payload = _decode_jwt(token)
                if payload.get("type") == "access":
                    return payload["sub"]
            except HTTPException:
                pass  # JWT 验证失败，回退到 API Key 模式

        # API Key 模式
        if token == API_KEY:
            return "user-default"
        if API_KEY:
            try:
                from ..agent.pgdb import PostgresAgentDB
                db = PostgresAgentDB()
                user = db.get_user_by_token(token)
                if user:
                    return user["id"]
            except Exception as e:
                logger.warning(f"Auth lookup failed: {e}")

        raise HTTPException(status_code=403, detail="Invalid token")

    if not API_KEY:
        return "anonymous"

    raise HTTPException(status_code=401, detail="Missing token")


# ═══════════════════════════════════════════════════════════════
# WebSocket JWT 验证
# ═══════════════════════════════════════════════════════════════


async def verify_ws_token(
    websocket: WebSocket,
    agent_id: str,
    token: Optional[str] = None,
) -> tuple[str, str]:
    """WebSocket 连接时验证 JWT token，返回 (user_id, resolved_agent_id)。

    v3.2 变更: agent_id 由 DB 验证而非从 user_id 派生，支持多智能体。
    兼容旧格式: 无 JWT 时从 agent_id 提取 user_id。

    返回: (user_id, agent_id) 元组
    """
    from ..agent.pgdb import PostgresAgentDB

    if not JWT_SECRET:
        # 开放模式: 从 agent_id 提取 user_id
        if agent_id.startswith("agent-"):
            extracted = agent_id[6:]
            if extracted and extracted != "001":
                return extracted, agent_id
        return "default", agent_id

    if not token:
        await websocket.close(code=4001, reason="Missing JWT token (?token=<jwt>)")
        raise HTTPException(status_code=401, detail="Missing JWT token")

    try:
        payload = _decode_jwt(token)
    except HTTPException:
        await websocket.close(code=4001, reason="Invalid or expired JWT token")
        raise

    if payload.get("type") != "access":
        await websocket.close(code=4001, reason="Token must be access token")
        raise HTTPException(status_code=401, detail="Not an access token")

    user_id = payload["sub"]
    jwt_agent_id = payload.get("agent_id", "")

    # v3.2: DB-backed agent 验证
    db = PostgresAgentDB()

    # 如果 JWT 有 agent_id，验证其属于该用户
    if jwt_agent_id:
        if not db.agent_belongs_to_user(jwt_agent_id, user_id):
            await websocket.close(code=4003, reason="agent_id does not belong to this user")
            raise HTTPException(status_code=403, detail="agent_id not owned by user")
        return user_id, jwt_agent_id

    # JWT 无 agent_id → 使用用户默认智能体
    default_agent = db.get_default_agent(user_id)
    if default_agent:
        return user_id, default_agent["id"]

    # 向后兼容: 旧 agent_id 格式 (agent-{user_id} / agent-001)
    if agent_id.startswith("agent-"):
        if not db.agent_belongs_to_user(agent_id, user_id):
            await websocket.close(code=4003, reason="agent_id mismatch with JWT")
            raise HTTPException(status_code=403, detail="agent_id mismatch")
        return user_id, agent_id

    await websocket.close(code=4003, reason="No valid agent found")
    raise HTTPException(status_code=403, detail="No valid agent")


# ═══════════════════════════════════════════════════════════════
# 超级管理员依赖 (v3.2)
# ═══════════════════════════════════════════════════════════════


async def verify_super_admin(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """验证超级管理员权限 → 返回 user_id。普通用户返回 403。"""
    user_id = await verify_api_key(credentials)
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
    """注册新用户 → 返回 tokens。

    返回: {user_id, username, access_token, refresh_token, token_type:"bearer"}
    """
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
    user_id = db.create_user(username, display, password_hash=pwd_hash)

    access_token = _create_jwt(user_id, username, "access")
    refresh_token = _create_jwt(user_id, username, "refresh")

    return {
        "user_id": user_id,
        "username": username,
        "display_name": display,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def auth_login(username: str, password: str) -> dict:
    """用户登录 → 返回 tokens。

    返回: {user_id, username, access_token, refresh_token, token_type:"bearer"}
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

    access_token = _create_jwt(user["id"], username, "access")
    refresh_token = _create_jwt(user["id"], username, "refresh")

    return {
        "user_id": user["id"],
        "username": username,
        "display_name": user.get("display_name", username),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def auth_refresh(refresh_token: str) -> dict:
    """使用 refresh_token 获取新的 access_token。"""
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token required")

    payload = _decode_jwt(refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token must be refresh token")

    user_id = payload["sub"]
    username = payload.get("username", "")
    new_access = _create_jwt(user_id, username, "access")
    new_refresh = _create_jwt(user_id, username, "refresh")

    return {
        "user_id": user_id,
        "username": username,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
    }
