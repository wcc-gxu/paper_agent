"""Agent 守护进程入口 — bootstrap + MainAgent。

Phase 5: 简化版守护进程
  - AgentManifest: Agent 身份持久化（保留）
  - AgentBootstrap: 创建/恢复 db + llm + tools + memory（不再编译 PlanGraph）
  - 启动 MainAgent (5 节点显式状态机) 主循环

旧 AgentRunLoop 类已删除（事件循环现在在 MainAgent.run 里）。

使用方式::

    python -m paper_search.agent.daemon
    python -m paper_search.agent.daemon --data-dir /path
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "agent-001"
DEFAULT_DISPLAY_NAME = "Paper Agent"
MANIFEST_VERSION = "1.0"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# Manifest
# ═══════════════════════════════════════════════════════════════


class AgentManifest:
    """Agent 身份证 — JSON 序列化数据结构。

    v3 Phase 1: 支持多用户，每个 user_id 独立 manifest 文件。
    """

    def __init__(self, data_dir: Path, user_id: str = "default"):
        self.user_id = user_id
        manifests_dir = data_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        self.path = manifests_dir / f"{user_id}.json"
        self.data: dict = {}
        self._legacy_path = data_dir / "agent_manifest.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        # 向后兼容：从旧 manifest 路径迁移
        if not self.path.exists() and self._legacy_path.exists():
            logger.info(f"Migrating legacy manifest from {self._legacy_path} to {self.path}")
            old_data = json.loads(self._legacy_path.read_text(encoding="utf-8"))
            old_data.setdefault("agent", {})["agent_id"] = f"agent-{self.user_id}"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(old_data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            # 保留旧文件作为备份，不删除
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self.data

    def save(self, data: dict):
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info(f"Manifest saved: {self.path}")

    @property
    def agent_id(self) -> str:
        return (self.data.get("agent") or {}).get("agent_id", f"agent-{self.user_id}")

    @property
    def llm_provider(self) -> str:
        return ((self.data.get("runtime") or {}).get("llm") or {}).get("provider", "deepseek")


# ═══════════════════════════════════════════════════════════════
# AgentManager — v3.2 多智能体路由
# ═══════════════════════════════════════════════════════════════


class AgentManager:
    """管理一个用户的所有智能体实例。

    v3.2: 一个 daemon 进程处理一个用户的所有智能体。
    BRPOP agent:ws:{user_id} → 读 target_agent_id → 路由到对应 MainAgent。
    """

    def __init__(self, user_id: str, redis_url: str, db, llm, registry, memory, vector_store):
        self.user_id = user_id
        self.redis_url = redis_url
        self.db = db
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.vector_store = vector_store
        self._agents: dict[str, "MainAgent"] = {}
        self._default_agent_id: str = ""

    async def load_agents(self, graph_builder) -> None:
        """从 DB 加载该用户所有活跃智能体."""
        agents = self.db.list_user_agents(self.user_id)
        if not agents:
            logger.warning("No agents found for user=%s, creating default", self.user_id)
            agent_id = self.db.create_agent(user_id=self.user_id, name="Default Agent")
            agents = self.db.list_user_agents(self.user_id)

        for ag in agents:
            agent_id = ag["id"]
            system_prompt = ag.get("system_prompt", "") or ""
            llm_provider = ag.get("llm_provider", "deepseek")
            logger.info("Loading agent: %s (name=%s provider=%s prompt_len=%d)",
                        agent_id, ag.get("name", ""), llm_provider, len(system_prompt))

            from .main_agent import MainAgent
            # Build graph with agent-specific system prompt
            compiled_graph = graph_builder(
                llm=self.llm,
                registry=self.registry,
                db=self.db,
                push_fn=self._make_push_fn(agent_id),
                get_user_fn=self._make_get_user_fn(),
                agent_system_prompt=system_prompt,
            )
            ma = MainAgent(
                agent_id=agent_id,
                redis_url=self.redis_url,
                user_id=self.user_id,
                llm=self.llm,
                db=self.db,
                memory=self.memory,
                registry=self.registry,
                graph=compiled_graph,
                system_prompt=system_prompt,
            )
            self._agents[agent_id] = ma
            if self._default_agent_id == "":
                self._default_agent_id = agent_id

        logger.info("AgentManager: loaded %d agents for user=%s (default=%s)",
                     len(self._agents), self.user_id, self._default_agent_id)

    def get_agent(self, agent_id: str) -> "MainAgent":
        """获取智能体实例，不存在则回退到默认智能体."""
        return self._agents.get(agent_id) or self._agents.get(self._default_agent_id)

    def _make_push_fn(self, agent_id: str):
        """创建 push 回调 — 将消息推送到 user-scoped outbox."""
        from .outbox import outbox_publish as _publish

        async def push(session_id, msg_type, sub_type, role, payload, priority_kind="normal"):
            envelope = {
                "type": msg_type,
                "subType": sub_type,
                "role": role,
                "agentId": agent_id,
                "sessionId": session_id,
                "payload": payload,
                "priorityKind": priority_kind,
            }
            import redis.asyncio as aioredis
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                await _publish(r, self.db, envelope, user_id=self.user_id)
            finally:
                await r.aclose()

        return push

    def _make_get_user_fn(self):
        """创建 get_user 回调 — 从 user-scoped 队列获取用户回复.

        Handles multiple reply types:
          - ask_reply: user response to ask card (clarification/choice)
          - plan_approve: user approves a plan
          - plan_revise: user requests plan revision with feedback

        Matches by session_id and ask_id/review_id.
        Non-matching messages are parked for later processing.
        Returns None on timeout.
        """
        import redis.asyncio as aioredis
        import json as _json
        import asyncio as _asyncio

        async def get_user(session_id, ask_id="", timeout=60):
            ws_key = f"agent:ws:{self.user_id}"
            parked_key = f"agent:ws:{self.user_id}:parked"
            deadline = _asyncio.get_event_loop().time() + timeout

            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                while True:
                    remaining = deadline - _asyncio.get_event_loop().time()
                    if remaining <= 0:
                        return None
                    # Check parked first
                    raw = await r.rpop(parked_key)
                    if raw is None:
                        raw = await r.brpop(ws_key, timeout=int(min(remaining, 30)))
                        if raw is None:
                            continue
                        raw = raw[1] if isinstance(raw, (list, tuple)) else raw

                    try:
                        msg = _json.loads(raw)
                    except _json.JSONDecodeError:
                        continue

                    p = msg.get("payload") or {}
                    msg_type = msg.get("type", "")

                    # Match: ask_reply — matching session and ask_id
                    if (msg_type == "ask_reply"
                            and msg.get("_session_id") == session_id
                            and p.get("ask_id") == ask_id):
                        p["_type"] = "ask_reply"
                        return p

                    # Match: plan_approve — matching session and review_id
                    if (msg_type == "plan_approve"
                            and msg.get("_session_id") == session_id
                            and p.get("plan_id", "")):
                        review_plan_id = ask_id.replace("review-", "") if ask_id.startswith("review-") else ""
                        if p.get("plan_id") == review_plan_id or ask_id.startswith(f"review-{p.get('plan_id')}"):
                            p["_type"] = "plan_approve"
                            return p

                    # Match: plan_revise — matching session and review_id
                    if (msg_type == "plan_revise"
                            and msg.get("_session_id") == session_id
                            and p.get("plan_id", "")):
                        review_plan_id = ask_id.replace("review-", "") if ask_id.startswith("review-") else ""
                        if p.get("plan_id") == review_plan_id or ask_id.startswith(f"review-{p.get('plan_id')}"):
                            p["_type"] = "plan_revise"
                            return p

                    # Not matching — park it for the main loop to pick up later
                    try:
                        await r.lpush(parked_key, raw)
                    except Exception:
                        pass
            finally:
                await r.aclose()

        return get_user

    async def run(self):
        """主循环：BRPOP agent:ws:{user_id} → dispatch to target agent."""
        import redis.asyncio as aioredis
        import json as _json
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        ws_queue = f"agent:ws:{self.user_id}"
        parked_queue = f"agent:ws:{self.user_id}:parked"

        logger.info("AgentManager running: user=%s queue=%s agents=%d",
                     self.user_id, ws_queue, len(self._agents))

        while True:
            try:
                raw = await r.brpop(ws_queue, timeout=0)
            except Exception as e:
                logger.error("AgentManager BRPOP error: %s, retrying...", e)
                await asyncio.sleep(1)
                continue

            msg_list = [_json.loads(raw[1])]
            # Drain backlog
            while True:
                more = await r.rpop(ws_queue)
                if more is None:
                    break
                msg_list.append(_json.loads(more))
            # Drain parked
            while True:
                more = await r.rpop(parked_queue)
                if more is None:
                    break
                msg_list.append(_json.loads(more))

            target_agent_id = msg_list[0].get("target_agent_id", "")
            agent = self.get_agent(target_agent_id)
            if agent is None:
                logger.warning("No agent for target=%s, using default=%s",
                              target_agent_id, self._default_agent_id)
                agent = self._agents.get(self._default_agent_id)
                if agent is None:
                    continue

            session_id = msg_list[0].get("_session_id", "main")
            # Combine message text for the agent
            user_content = agent._combine_user_text(msg_list)
            agent._correlation_id = str(uuid.uuid4())
            agent._current_session_id = session_id
            try:
                await agent._run_turn(session_id, user_content)
            except Exception as e:
                logger.error("Agent %s turn error: %s", agent._agent_id, e, exc_info=True)


# ═══════════════════════════════════════════════════════════════
# Bootstrap
# ═══════════════════════════════════════════════════════════════


class AgentBootstrap:
    """Agent 引导程序 — 启动时调用的唯一入口。

    v3 Phase 1 重构：支持多用户（user_id 参数）。

    构建 4 个核心组件:
      - AgentDB / PostgresAgentDB
      - LLMClientV2
      - ToolRegistry
      - MemoryManager (含 ChromaStore/PgVectorStore 可选)
    """

    def __init__(self, data_dir: Optional[Path] = None, user_id: str = "default"):
        if data_dir is None:
            from ..config import get_data_dir
            data_dir = get_data_dir()
        self.data_dir = Path(data_dir)
        self.user_id = user_id
        self.manifest = AgentManifest(self.data_dir, user_id=user_id)

        # 惰性初始化的组件
        self._db = None
        self._llm = None
        self._tools = None
        self._memory = None

    async def bootstrap(self) -> dict:
        """主入口 — 创建或恢复 Agent。

        Returns:
            {"manifest": dict, "db": AgentDB, "llm": LLMClientV2,
             "tools": ToolRegistry, "memory": MemoryManager}
        """
        if self.manifest.exists():
            logger.info(f"Resuming agent from manifest: {self.manifest.path}")
            return await self._resume()
        logger.info("First boot — creating main agent")
        return await self._create()

    # ── 核心组件构建（创建/恢复共用） ─────────────────

    async def _build_core(self, manifest_data: dict, llm_provider: str = "deepseek"):
        """统一构建 db / llm / tools / memory，避免 _create/_resume 重复代码。"""

        # 1. DB: PostgreSQL (DATABASE_URL 必须设置)
        from ..agent.pgdb import PostgresAgentDB
        self._db = PostgresAgentDB()
        logger.info("Database: PostgreSQL")

        # 2. LLMClientV2 — via singleton (shared across daemon, tools, celery)
        from ..agent.llm_client_v2 import get_llm_client
        self._llm = get_llm_client(provider=llm_provider)
        logger.info("LLM client initialized (singleton, provider=%s)", llm_provider)

        # 3. ToolRegistry — inject DB singleton
        from ..agent.tool_registry import ToolRegistry, set_db
        set_db(self._db, user_id=self.user_id)  # 替代 66 处惰性 AgentDB() 调用
        self._tools = ToolRegistry.get_instance()
        logger.info(f"ToolRegistry: {len(self._tools.tool_names)} tools")

        # 4. MemoryManager (含向量存储, 可选)
        from ..agent.memory import MemoryManager
        chroma = None
        try:
            from ..agent.pgvector_store import PgVectorStore
            chroma = PgVectorStore(user_id=self.user_id)
            logger.info("PgVectorStore initialized")
        except Exception as e:
            logger.warning(f"Vector store unavailable (knowledge tools degrade): {e}")
        self._memory = MemoryManager(self._db, chroma)

    async def _resume(self) -> dict:
        """从已有的 manifest 恢复 Agent。"""
        manifest_data = self.manifest.load()
        llm_provider = ((manifest_data.get("runtime") or {}).get("llm") or {}).get("provider", "deepseek")
        await self._build_core(manifest_data, llm_provider=llm_provider)
        # 更新 manifest 状态
        manifest_data.setdefault("agent", {})["status"] = "active"
        manifest_data["agent"]["updated_at"] = _now()
        self.manifest.save(manifest_data)
        return {
            "manifest": manifest_data,
            "db": self._db, "llm": self._llm,
            "tools": self._tools, "memory": self._memory,
        }

    async def _create(self) -> dict:
        """首次启动 — 从头创建 Agent。"""
        now = _now()
        agent_id = f"agent-{self.user_id}" if self.user_id != "default" else DEFAULT_AGENT_ID

        manifest_data = {
            "manifest_version": MANIFEST_VERSION,
            "agent": {
                "agent_id": agent_id,
                "type": "main",
                "display_name": DEFAULT_DISPLAY_NAME,
                "user_id": self.user_id,
                "created_at": now,
                "updated_at": now,
                "status": "active",
            },
            "runtime": {
                "llm": {"provider": "deepseek"},
            },
        }
        await self._build_core(manifest_data, llm_provider="deepseek")
        self._db.create_session(agent_id, "main", title="新对话", user_id=self.user_id)
        self.manifest.save(manifest_data)
        return {
            "manifest": manifest_data,
            "db": self._db, "llm": self._llm,
            "tools": self._tools, "memory": self._memory,
        }


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None, redis_url: Optional[str] = None,
               user_id: str = "default"):
    """daemon 主入口 — bootstrap + MainAgent。

    Args:
        data_dir: 数据目录路径。
        redis_url: Redis 连接 URL。
        user_id: 用户 ID（v3 多用户支持）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Enable debug protocol: pushes LLM thinking + tool call details to WS client
    os.environ.setdefault("DEBUG_PROTOCOL", "1")
    logger.info("DEBUG_PROTOCOL enabled — LLM thinking + tool calls will be pushed to clients")

    path = Path(data_dir) if data_dir else None
    redis = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 从环境变量读取 user_id（可选覆盖）
    user_id = os.getenv("PAPER_USER_ID", user_id)

    # Bootstrap
    bs = AgentBootstrap(data_dir=path, user_id=user_id)
    result = await bs.bootstrap()

    manifest = result["manifest"]
    agent_id = manifest["agent"]["agent_id"]
    logger.info(f"Agent {agent_id} (user={user_id}) ready, starting MainAgent...")

    # v3.1: Build LangGraph MainGraph
    from .graphs.main_graph import build_main_graph
    from .outbox import outbox_publish
    import redis.asyncio as aioredis

    # Create Redis client for graph outbox pushes
    _redis_client = aioredis.from_url(redis, decode_responses=True)

    async def _graph_push(session_id: str, msg_type: str, subtype: str,
                          role: str, payload: dict = None,
                          priority_kind: str = "normal") -> None:
        """Adapter: graph node push → outbox_publish."""
        envelope = {
            "type": msg_type, "subType": subtype, "role": role,
            "agentId": agent_id, "sessionId": session_id,
            "payload": payload or {},
            "priority": priority_kind,
        }
        await outbox_publish(_redis_client, result["db"], envelope)

    async def _graph_get_user(session_id: str, ask_id: str,
                               timeout: int = 1800) -> dict | None:
        """Wait for user reply to an ask/plan_review card via Redis BRPOP.

        Handles multiple reply types:
          - ask_reply: user response to ask card (clarification/choice)
          - plan_approve: user approves a plan
          - plan_revise: user requests plan revision with feedback

        Matches by session_id and ask_id/review_id.
        Returns payload dict with _type field set to distinguish reply types.
        Non-matching messages are parked for later processing.
        Returns None on timeout.
        """
        ws_queue = f"agent:ws:{agent_id}"
        parked_queue = f"agent:ws:{agent_id}:parked"
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.info("get_user timed out for ask_id=%s", ask_id)
                return None
            try:
                raw = await _redis_client.brpop(
                    ws_queue, timeout=int(min(remaining, 30)),
                )
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if raw is None:
                continue
            try:
                msg = json.loads(raw[1])
            except json.JSONDecodeError:
                continue

            p = msg.get("payload") or {}
            msg_type = msg.get("type", "")

            # Match: ask_reply type, matching session and ask_id
            if (msg_type == "ask_reply"
                    and msg.get("_session_id") == session_id
                    and p.get("ask_id") == ask_id):
                p["_type"] = "ask_reply"
                return p

            # Match: plan_approve type, matching session and review_id (plan_review→approve)
            if (msg_type == "plan_approve"
                    and msg.get("_session_id") == session_id
                    and p.get("plan_id", "")):
                # review_id format: review-plan-{plan_id}
                review_plan_id = ask_id.replace("review-", "") if ask_id.startswith("review-") else ""
                if p.get("plan_id") == review_plan_id or ask_id.startswith(f"review-{p.get('plan_id')}"):
                    p["_type"] = "plan_approve"
                    return p

            # Match: plan_revise type, matching session and review_id (plan_review→revise)
            if (msg_type == "plan_revise"
                    and msg.get("_session_id") == session_id
                    and p.get("plan_id", "")):
                review_plan_id = ask_id.replace("review-", "") if ask_id.startswith("review-") else ""
                if p.get("plan_id") == review_plan_id or ask_id.startswith(f"review-{p.get('plan_id')}"):
                    p["_type"] = "plan_revise"
                    return p

            # Not matching — park it for the main loop to pick up later
            try:
                await _redis_client.lpush(parked_queue, raw[1])
            except Exception:
                pass

    compiled_graph = build_main_graph(
        llm=result["llm"],
        registry=result["tools"],
        db=result["db"],
        push_fn=_graph_push,
        get_user_fn=_graph_get_user,
    )

    # MainAgent (v3.1: delegates to compiled_graph)
    from .main_agent import MainAgent
    main_agent = MainAgent(
        agent_id=agent_id,
        redis_url=redis,
        llm=result["llm"],
        db=result["db"],
        memory=result["memory"],
        registry=result["tools"],
        graph=compiled_graph,
    )

    # ── Agent Error Report 消费者 ─────────────────────────
    # 订阅 agent:reports:{agent_id} Pub/Sub channel，
    # 消费子 agent / tool 通过 Reporter.publish_agent_error() 上报的错误，
    # 推入 MainAgent._error_queue 供当前 turn 检查

    async def _consume_agent_reports():
        """Background task: subscribe to agent error reports from Redis Pub/Sub."""
        import redis.asyncio as aioredis

        sub_redis = aioredis.from_url(redis, decode_responses=True)
        channel_name = f"agent:reports:{agent_id}"

        try:
            pubsub = sub_redis.pubsub()
            await pubsub.subscribe(channel_name)
            logger.info("Agent error report consumer started on channel: %s", channel_name)

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message.get("data", "{}"))
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")
                if msg_type == "agent_error":
                    # 推入 MainAgent 的错误队列
                    main_agent.push_agent_error(data)
                elif msg_type == "agent_retry":
                    logger.info(
                        "[RETRY] agent=%s node=%s retry=%s/%s reason=%s",
                        data.get("agent", "?"),
                        data.get("node", "?"),
                        data.get("retry_count", "?"),
                        data.get("max_retries", "?"),
                        data.get("reason", "")[:100],
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Agent report consumer crashed: {e}", exc_info=True)
        finally:
            try:
                await pubsub.unsubscribe(channel_name)
            except Exception:
                pass
            try:
                await sub_redis.close()
            except Exception:
                pass

    report_consumer_task = asyncio.create_task(
        _consume_agent_reports(), name="agent-report-consumer",
    )

    # 信号处理（优雅退出）
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    try:
        loop_evt = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop_evt.add_signal_handler(sig, _sig_handler)
            except (NotImplementedError, RuntimeError):
                pass  # Windows 不支持
    except RuntimeError:
        pass

    run_task = asyncio.create_task(main_agent.run(), name="main-agent-run")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        pass
    finally:
        run_task.cancel()
        report_consumer_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await report_consumer_task
        except (asyncio.CancelledError, Exception):
            pass

    logger.info("MainAgent stopped, exiting daemon.")
    return result


if __name__ == "__main__":
    # 简单 argv 解析（不引入 argparse 依赖）
    data_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--data-dir" and i + 1 < len(sys.argv) - 1:
            data_dir = sys.argv[i + 2]
            break
        if arg.startswith("--data-dir="):
            data_dir = arg.split("=", 1)[1]
            break

    asyncio.run(main(data_dir=data_dir))
