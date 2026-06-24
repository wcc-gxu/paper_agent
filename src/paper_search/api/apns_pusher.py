"""APNs Pusher — 离线 iOS 推送（Phase 1: 骨架就位，实现后补）。

骨架完整：
  - 设备 token 表 device_tokens（已在 db.py 定义）
  - REST 端点 /api/devices/register（已在 routes.py 定义）
  - APNsPusher.push() 接口：实际现在仅 logger.info 记录
  - outbox_poller 在用户离线时调用 push（带 priority 过滤）

后补步骤（用户配置好 APNs key 后）：
  1. pip install aioapns>=3.1
  2. .env 增加 APNS_KEY_PATH / APNS_KEY_ID / APNS_TEAM_ID / APNS_TOPIC / APNS_USE_SANDBOX
  3. 把 APNsPusher.push 的 logger.info 替换为真 aioapns 调用
  4. E2E 测试（需要 Apple developer 账户 + 真机）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── 消息预览生成 ──────────────────────────────────────────────

def make_preview(envelope: dict) -> tuple[str, str]:
    """从 envelope 生成 APNs 预览（title, body）。"""
    msg_type = envelope.get("type", "")
    sub_type = envelope.get("subType", "")
    payload = envelope.get("payload", {})

    # v10: message/reply（v9 message/text 也兼容）
    if msg_type == "message" and sub_type in ("reply", "text"):
        content = (payload.get("content") or "")[:120]
        return ("Paper Agent", content or "新消息")

    # v10: tool/result（v9 tool/sub_result 也兼容）
    if msg_type == "tool" and sub_type in ("result", "sub_result"):
        name = payload.get("name", "任务")
        status = payload.get("status", "")
        summary = (payload.get("summary") or "")[:100]
        if status == "done":
            return (f"✅ {name} 完成", summary or "任务已完成")
        if status == "failed":
            return (f"❌ {name} 失败", summary or "任务失败")
        return (f"{name}", summary)

    # v10: ask（合并 ask_user_question + propose_plan）
    if msg_type == "ask":
        kind = payload.get("kind", "")
        prompt = (payload.get("prompt") or "")[:100]
        if kind == "plan":
            return ("📋 待审批方案", prompt or "请回到 App 中查看方案")
        return ("Paper Agent 需要您的回答", prompt or "请回到 App 中查看问题")

    # v9 兼容
    if msg_type == "tool" and sub_type == "ask_user_question":
        return ("Paper Agent 需要您的回答", "请回到 App 中查看问题")
    if msg_type == "tool" and sub_type == "propose_plan":
        plan_summary = (payload.get("summary") or "")[:100]
        return ("📋 待审批方案", plan_summary or "请回到 App 中查看方案")

    if msg_type == "error":
        return ("⚠️ 错误", (payload.get("message") or "")[:100] or "发生错误")

    return ("Paper Agent", "新消息")


# ── Pusher ───────────────────────────────────────────────────


class APNsPusher:
    """APNs 推送客户端（Phase 1: 仅占位，后补真实实现）。"""

    def __init__(self, db: Any = None):
        self._db = db
        self._client: Optional[Any] = None
        self._enabled = bool(
            os.getenv("APNS_KEY_PATH")
            and os.getenv("APNS_KEY_ID")
            and os.getenv("APNS_TEAM_ID")
            and os.getenv("APNS_TOPIC")
        )
        if self._enabled:
            logger.info("APNs Pusher initialized (config detected, but aioapns wiring TODO)")
        else:
            logger.info("APNs Pusher initialized (no config; running in stub mode)")

    async def _ensure_client(self):
        """延迟初始化 aioapns 客户端。

        Phase 1: 占位。后补时:
            from aioapns import APNs
            self._client = APNs(
                key=os.getenv("APNS_KEY_PATH"),
                key_id=os.getenv("APNS_KEY_ID"),
                team_id=os.getenv("APNS_TEAM_ID"),
                topic=os.getenv("APNS_TOPIC"),
                use_sandbox=os.getenv("APNS_USE_SANDBOX", "1") == "1",
            )
        """
        return None

    async def push(self, agent_id: str, envelope: dict,
                   silent: bool = False) -> bool:
        """推送一条 envelope 到 agent_id 关联的所有活跃设备。

        Phase 1 行为: 仅记录日志，不真发推送。
        将 ws_messages.apns_sent_at 标记为已尝试，避免重复触发。

        Args:
            agent_id: Agent ID
            envelope: 完整 v9.0 信封
            silent: True 时 APNs 用 "content-available"（静默 / 后台唤醒），
                   False 时带 alert / sound

        Returns:
            True 表示已"派发"（即便是 stub mode 也算成功，避免阻塞主链路）
        """
        if not self._db:
            logger.warning("APNsPusher.push: no db, skipping")
            return False

        tokens = []
        try:
            tokens = self._db.get_active_device_tokens(agent_id)
        except Exception as e:
            logger.warning(f"APNsPusher.push: get_active_device_tokens failed: {e}")

        msg_id = envelope.get("msg_id", "")
        title, body = make_preview(envelope)

        if not tokens:
            logger.info(
                "[APNS STUB] no devices registered for agent=%s msg=%s title=%r",
                agent_id, msg_id[:8], title,
            )
            if msg_id:
                try:
                    self._db.mark_message_apns_sent(msg_id)
                except Exception:
                    pass
            return True  # 无设备视为"派发"成功（不影响 outbox poller 流程）

        # Phase 1: 仅 log，不真推送
        for tok in tokens:
            logger.info(
                "[APNS STUB] would push to agent=%s device=%s... silent=%s | %r / %r",
                agent_id, tok["device_token"][:12], silent, title, body,
            )

        # 后补 (Phase TBD)：实际推送
        # await self._ensure_client()
        # for tok in tokens:
        #     try:
        #         await self._client.send_notification(
        #             NotificationRequest(
        #                 device_token=tok["device_token"],
        #                 message={"aps": {"alert": {"title": title, "body": body}, "sound": "default"}, "data": {"msg_id": msg_id}},
        #             )
        #         )
        #     except UnregisteredException:
        #         self._db.deactivate_device_token(agent_id, tok["device_token"])
        #     except Exception as e:
        #         logger.warning(f"APNs push failed for {tok['device_token'][:12]}: {e}")

        if msg_id:
            try:
                self._db.mark_message_apns_sent(msg_id)
            except Exception:
                pass
        return True


# ── 单例 ───────────────────────────────────────────────────────


_pusher: Optional[APNsPusher] = None


def get_apns_pusher(db: Any = None) -> APNsPusher:
    """获取 APNsPusher 单例。"""
    global _pusher
    if _pusher is None:
        _pusher = APNsPusher(db=db)
    return _pusher
