"""动作通知查询 API。

动作执行 `notification` 类执行器后会写一条记录到 `action_notifications`，
这里把它读出来，让"动作到底发了什么"可追溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.notification_service import list_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: str
    action_id: str | None = None
    action_name: str | None = None
    channel: str
    recipient: str | None = None
    message: str
    status: str
    error: str | None = None
    meta_json: dict | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[NotificationOut])
def get_notifications(
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, description="delivered / failed / not_implemented"),
    db: Session = Depends(get_db),
) -> list[Any]:
    return list_notifications(db, limit=limit, status=status)
