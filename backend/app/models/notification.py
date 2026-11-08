"""动作通知 —— 动作执行后投递的消息。

为什么新建一张表：
    原来 `notification` 执行器只返回一句 "Notification sent"，什么都没发生。
    项目里唯一的"通知类"存储是 `t_alert`（监控告警：level/service_name/resolved），
    语义是运维告警，拿它存业务通知会污染监控面板，也让"某条动作发了什么通知"
    无法追溯。

投递状态语义：
    delivered      已投递（system 渠道写入本表即算送达；webhook 返回 2xx）
    failed         投递失败（附 error）
    not_implemented 渠道未接入（email / sms 等）—— 明确标记，不假装成功
"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.identifiers import gen_uuid


class ActionNotification(Base):
    __tablename__ = "action_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    # 触发它的动作（动作被删后保留记录，因此不加外键约束）
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="system")
    recipient: Mapped[str | None] = mapped_column(String(500), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="delivered")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 渠道相关的补充信息（webhook 状态码、请求地址等）
    meta_json: Mapped[dict | None] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_action_notif_created", "created_at"),
        Index("ix_action_notif_status", "status"),
    )
