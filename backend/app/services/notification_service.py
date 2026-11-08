"""动作通知投递服务。

渠道支持现状：
    system   落库到 action_notifications（站内消息，可通过 GET /api/v1/notifications 读取）
    webhook  真的 POST 出去（httpx，3 秒超时，记录状态码）
    email / sms / im
             未接入网关 —— 记录为 not_implemented，**不假装成功**

设计原则：宁可明确报告"没做"，也不要返回一句 "Notification sent" 让人以为发出去了。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import ActionNotification

logger = logging.getLogger(__name__)

CHANNEL_SYSTEM = "system"
CHANNEL_WEBHOOK = "webhook"
UNSUPPORTED_CHANNELS = {"email", "sms", "im", "dingtalk", "wecom"}

WEBHOOK_TIMEOUT_SECONDS = 3.0


def deliver(
    db: Session,
    *,
    action_id: str | None,
    action_name: str | None,
    channel: str,
    recipient: str | None,
    message: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """投递一条通知，返回 {"ok", "status", "id", "error"}。"""
    channel = (channel or CHANNEL_SYSTEM).strip().lower()
    record = ActionNotification(
        action_id=action_id,
        action_name=action_name,
        channel=channel,
        recipient=recipient,
        message=message or "",
        status="delivered",
        meta_json=meta or {},
    )

    if channel in UNSUPPORTED_CHANNELS:
        record.status = "not_implemented"
        record.error = f"渠道 {channel} 尚未接入发送网关"
    elif channel == CHANNEL_SYSTEM:
        # 站内消息：写入本表即为送达
        record.status = "delivered"
    elif channel == CHANNEL_WEBHOOK:
        ok, error, extra = _post_webhook(recipient, message, meta)
        record.status = "delivered" if ok else "failed"
        record.error = error
        record.meta_json = {**(meta or {}), **extra}
    else:
        record.status = "not_implemented"
        record.error = f"未知渠道: {channel}"

    db.add(record)
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("通知落库失败: %s", e)
        return {"ok": False, "status": "failed", "id": None, "error": f"落库失败: {e}"}

    return {
        "ok": record.status == "delivered",
        "status": record.status,
        "id": record.id,
        "error": record.error,
    }


def _post_webhook(
    url: str | None, message: str, meta: dict[str, Any] | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """POST 到 webhook 地址。返回 (成功?, 错误, 补充信息)。"""
    if not url:
        return False, "webhook 渠道缺少 recipient（目标 URL）", {}

    try:
        import httpx
    except ImportError:
        return False, "httpx 未安装，无法发送 webhook", {}

    payload = {"message": message, **(meta or {})}
    try:
        resp = httpx.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001
        return False, f"webhook 请求失败: {e}", {"url": url}

    ok = 200 <= resp.status_code < 300
    return ok, None if ok else f"webhook 返回 {resp.status_code}", {
        "url": url,
        "status_code": resp.status_code,
    }


def list_notifications(
    db: Session, *, limit: int = 50, status: str | None = None,
) -> list[ActionNotification]:
    q = db.query(ActionNotification)
    if status:
        q = q.filter(ActionNotification.status == status)
    return q.order_by(ActionNotification.created_at.desc()).limit(max(1, min(limit, 200))).all()
