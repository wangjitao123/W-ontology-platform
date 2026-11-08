from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OntologyEntity
from app.models.rule import EntityAction
from app.schemas.action import (
    ActionCreate, ActionUpdate, ActionOut,
    ActionExecuteRequest, ActionExecuteResult, ActionTypeInfo,
)
from app.repositories.action_repo import ActionRepository
from app.core.deps import get_current_user
from app.models.user import User
from app.services.audit import write_audit
from app.services.action_executors import get_executor, get_all_type_info
from app.services.action_governance import ActionBlocked, govern

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("/types", response_model=list[ActionTypeInfo])
def list_action_types():
    return get_all_type_info()


@router.get("", response_model=list[ActionOut])
def list_actions(
    entity_id: str | None = Query(None),
    status: str | None = Query(None),
    action_type: str | None = Query(None),
    category: str | None = Query(None),
    search: str | None = Query(None),
    db: Session = Depends(get_db),
):
    repo = ActionRepository(db)
    actions = repo.list_with_filters(
        entity_id=entity_id, status=status,
        action_type=action_type, category=category, search=search,
    )
    results = []
    for a in actions:
        out = ActionOut.model_validate(a)
        if a.entity_id:
            out.entity_name = repo.get_entity_name(a.entity_id)
        results.append(out)
    return results


@router.get("/{action_id}", response_model=ActionOut)
def get_action(action_id: str, db: Session = Depends(get_db)):
    repo = ActionRepository(db)
    a = repo.get_by_id(action_id)
    if not a:
        raise HTTPException(status_code=404, detail="动作不存在")
    out = ActionOut.model_validate(a)
    if a.entity_id:
        out.entity_name = repo.get_entity_name(a.entity_id)
    return out


@router.post("", response_model=ActionOut, status_code=201)
def create_action(
    data: ActionCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    if data.category == "domain" and not data.entity_id:
        raise HTTPException(status_code=400, detail="领域行动必须绑定实体")
    if data.category == "system" and data.entity_id:
        raise HTTPException(status_code=400, detail="系统行动不应绑定实体")

    if data.entity_id:
        entity = db.get(OntologyEntity, data.entity_id)
        if not entity:
            raise HTTPException(status_code=400, detail="关联实体不存在")

    repo = ActionRepository(db)
    action = EntityAction(**data.model_dump())
    repo.create(action)

    write_audit(
        db, user_id=user.id if user else None,
        user_name=user.name if user else None,
        action="create", target_type="action",
        target_id=action.id, target_name=action.name,
    )
    repo.commit()

    out = ActionOut.model_validate(action)
    if action.entity_id:
        out.entity_name = repo.get_entity_name(action.entity_id)
    return out


@router.put("/{action_id}", response_model=ActionOut)
def update_action(
    action_id: str, data: ActionUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    repo = ActionRepository(db)
    action = repo.get_by_id(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")

    changes = []
    for field, value in data.model_dump(exclude_unset=True).items():
        old = getattr(action, field)
        if old != value:
            changes.append({"field": field, "oldValue": old, "newValue": value})
            setattr(action, field, value)

    if changes:
        write_audit(
            db, user_id=user.id if user else None,
            user_name=user.name if user else None,
            action="update", target_type="action",
            target_id=action.id, target_name=action.name, changes=changes,
        )
    repo.commit()

    out = ActionOut.model_validate(action)
    if action.entity_id:
        out.entity_name = repo.get_entity_name(action.entity_id)
    return out


@router.delete("/{action_id}", status_code=204)
def delete_action(
    action_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    repo = ActionRepository(db)
    action = repo.get_by_id(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")

    write_audit(
        db, user_id=user.id if user else None,
        user_name=user.name if user else None,
        action="delete", target_type="action",
        target_id=action.id, target_name=action.name,
    )
    repo.delete(action)
    repo.commit()


@router.post("/{action_id}/validate")
def validate_action(
    action_id: str,
    data: ActionExecuteRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """只跑治理三道闸，不执行动作。

    用于前端在"确认执行"前预检：必填参数是否齐、前置条件是否满足、
    写回属性是否在白名单内。
    """
    repo = ActionRepository(db)
    action = repo.get_by_id(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")

    try:
        return govern(
            db, action,
            data.params if data else {},
            data.object_key if data else None,
        )
    except ActionBlocked as e:
        return {"ok": False, **e.to_detail()}


@router.post("/{action_id}/execute", response_model=ActionExecuteResult)
async def execute_action(
    action_id: str,
    data: ActionExecuteRequest | None = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    repo = ActionRepository(db)
    action = repo.get_by_id(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")

    params = data.params if data else {}
    object_key = data.object_key if data else None
    dry_run = data.dry_run if data else False

    # ── 三道闸：参数校验 → 前置条件 → 写回白名单 ──
    # 未通过时一律不进入执行器，避免"校验归校验、执行归执行"的漏洞。
    try:
        report = govern(db, action, params, object_key)
    except ActionBlocked as e:
        raise HTTPException(status_code=400, detail=e.to_detail())

    # 用规范化后的参数执行（已套用默认值、已做类型转换）；
    # object_key 一并透传，写回类执行器靠它定位目标实例。
    exec_params = dict(report["params"])
    if object_key:
        exec_params.setdefault("object_key", object_key)
    # 动作上下文：`__` 前缀表示内部键，供 notification 等执行器追溯来源
    exec_params.setdefault("__action_id", action.id)
    exec_params.setdefault("__action_name", action.name)

    executor = get_executor(action.action_type)
    result = await executor.execute(action.type_config or {}, exec_params, dry_run)

    if not dry_run:
        action.impact_count = (action.impact_count or 0) + 1
        write_audit(
            db, user_id=user.id if user else None,
            user_name=user.name if user else None,
            action="execute", target_type="action",
            target_id=action.id, target_name=action.name,
            changes=[{
                "field": "execution",
                "oldValue": None,
                "newValue": {
                    "objectKey": object_key,
                    "params": exec_params,
                    "success": result.success,
                    "preconditions": report.get("preconditions"),
                },
            }],
        )
        repo.commit()

    return ActionExecuteResult(
        success=result.success,
        message=result.message,
        output=result.output,
        governance=report,
    )
