import asyncio

from .base import BaseActionExecutor, ExecutionResult


class ModifyAttributeExecutor(BaseActionExecutor):
    """修改本体实例的属性值。

    改造说明：原实现只返回一句 "Set X = Y"，从未真正写库。现在通过
    `action_writeback.update_object_attribute` 走 ExecuteService，因此必须
    同时满足：ACTION_WRITEBACK_ENABLED、Connection.writable、以及
    Action Governance 的写回白名单。
    """

    async def execute(self, type_config: dict, params: dict, dry_run: bool = False) -> ExecutionResult:
        target_entity_id = type_config.get("target_entity_id")
        target_attribute = type_config.get("target_attribute")
        value_expression = type_config.get("value_expression", "")
        object_key = params.get("object_key") or type_config.get("object_key")

        # value_expression 可以是参数名，也可以是字面量
        resolved_value = params.get(value_expression, value_expression)

        if dry_run:
            return ExecutionResult(
                success=True,
                message=f"[Dry Run] Would set {target_attribute} = {resolved_value}",
                output={
                    "entity_id": target_entity_id,
                    "attribute": target_attribute,
                    "value": resolved_value,
                    "object_key": object_key,
                },
            )

        if not target_entity_id:
            return ExecutionResult(False, "缺少 target_entity_id，无法确定写回目标实体")
        if not target_attribute:
            return ExecutionResult(False, "缺少 target_attribute")
        if not object_key:
            return ExecutionResult(
                False,
                "缺少 object_key：写回必须指定目标实例，避免误改整表",
            )

        result = await asyncio.to_thread(
            self._write, target_entity_id, object_key, target_attribute, resolved_value,
        )
        return ExecutionResult(
            success=bool(result.get("ok")),
            message=result.get("message", ""),
            output=result.get("output"),
        )

    @staticmethod
    def _write(entity_id: str, object_key: str, attribute: str, value) -> dict:
        from app.database import SessionLocal
        from app.services.action_writeback import update_object_attribute

        db = SessionLocal()
        try:
            return update_object_attribute(db, entity_id, object_key, attribute, value)
        finally:
            db.close()

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "target_entity_id": {"type": "string", "required": True, "description": "目标实体 ID"},
            "target_attribute": {"type": "string", "required": True, "description": "目标属性名"},
            "value_expression": {"type": "string", "required": True, "description": "赋值表达式或参数名"},
        }

    @classmethod
    def get_label(cls) -> str:
        return "修改实体属性"

    @classmethod
    def get_description(cls) -> str:
        return "修改本体实例的属性值（走受治理的写回通道）"
