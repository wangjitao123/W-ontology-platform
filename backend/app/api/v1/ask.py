"""问数 API —— 方案 B 的入口。

一个问题进来，平台自己决定：
    · 能用已定义指标回答 → 走 Cube，返回 governed=true
    · 否则             → 走 Auto-Ontology，返回 governed=false
    · 兜底引擎不可用     → 如实返回 engine_unavailable

前端必须把 governed 显示出来 —— 这是方案 B 的全部意义：
**允许两套引擎并存，但不允许用户分不清手里的数来自哪一套。**
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services import adhoc_client, ask_service
from app.services.metrics_cube import CubeClient, CubeError

router = APIRouter(prefix="/ask", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    # 只影响临时分析路径：要作用在哪个库上。不传则用 ADHOC_TARGET_DB。
    target_db: str | None = Field(default=None, max_length=100)
    # 跳过语义护栏的硬拦截。仅用于人工复核时查看原始结果——
    # 默认 False，因为拦的都是已经证实会算错数的查询。
    force: bool = Field(default=False)


@router.post("")
def ask(body: AskRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """问一个问题。返回里带 route / governed / source，前端据此标注。"""
    return ask_service.answer(
        db, body.question, target_db=body.target_db, force=body.force,
    )


@router.get("/databases")
def ask_databases() -> dict[str, Any]:
    """临时分析可选的数据库列表（来自 Auto-Ontology 已注册的连接）。

    引擎不可用时返回空列表 + 说明，不报错——选库只是辅助 UI。
    """
    databases = adhoc_client.list_databases()
    return {
        "databases": databases,
        "default": settings.ADHOC_TARGET_DB or None,
        "note": None if databases else "临时分析引擎未就绪或没有已注册的数据库",
    }


@router.get("/health")
def ask_health() -> dict[str, Any]:
    """两个引擎各自是否可用。"""
    cube: dict[str, Any]
    try:
        cubes = CubeClient().list_cubes()
        cube = {
            "ok": True,
            "cubeCount": len(cubes),
            "measureCount": sum(len(c["measures"]) for c in cubes),
        }
    except CubeError as e:
        cube = {"ok": False, "reason": e.reason, "message": e.message}

    return {
        "cube": cube,
        "adHoc": adhoc_client.health(),
        "note": (
            "Cube 不可用时无法回答受治理的问题；"
            "临时分析引擎还需要 embedding 模型（DeepSeek 不提供 embeddings 接口）。"
        ),
    }
