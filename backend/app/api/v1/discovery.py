"""本体自动发现 API：扫表结构 → 推候选 → 采纳进草稿版本。

## 和现有 AI 建模功能的区别

`/ai-builder/extract-ontology` 是**对话式 LLM 建模**：先问业务场景、再问材料，
prompt 里写死了联通领域。它不看数仓里实际有什么表。

这里反过来：**先看数据，再谈本体**。纯元数据推导，不调 LLM，
每一条候选都能说清"根据哪几张表的哪些列推出来的"。

## 采纳不直接写库

采纳 = 生成一份草稿版本，走 ③ 的 diff / 影响面 / 审批 / 回滚。
直接往 `ontology_entities` 插行的话，上面这些保障全都没有。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.discovery import service

router = APIRouter(prefix="/discovery", tags=["discovery"])


class AdoptRequest(BaseModel):
    # 候选 id，形如 ent::material / snap::dwh_ads::xxx / rel::material::plant
    proposal_ids: list[str] = Field(..., min_length=1)
    version_name: str | None = None
    include_snapshots: bool = True


@router.get("/scan")
def scan_tables(
    include_snapshots: bool = Query(True),
    refresh: bool = Query(False, description="忽略缓存重新扫描"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """扫描 Doris 表结构并推导对象类型候选。

    返回三组候选（entities / snapshots / relations），每条带 `reason` 和
    `confidence`，前端按置信度分组展示。

    缓存 2 分钟：元数据查询约 1-2 秒，采纳时要按 id 回查，不宜每次重扫。
    """
    try:
        return service.get_proposals(
            db, include_snapshots=include_snapshots, force=refresh,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


@router.post("/adopt")
def adopt_proposals(req: AdoptRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """把选中的候选写成一份草稿版本。

    返回新版本的 id；之后用 `/ontology-publish/versions/{id}/diff`
    看差异，审批通过才真正创建对象类型。
    """
    try:
        result = service.adopt(
            db,
            req.proposal_ids,
            version_name=req.version_name,
            include_snapshots=req.include_snapshots,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "采纳失败")
    return result
