import asyncio

from .base import BaseActionExecutor, ExecutionResult


class CallFunctionExecutor(BaseActionExecutor):
    """调用已注册的本体函数。

    改造说明：原实现只返回 `"Called function <id>"` 且 `result: None` —— 函数一次都没执行过。
    现在通过 `FunctionExecutor` 真正执行，支持三种 logic_type：
        expression  受限 eval（参数走 params）
        sql         在实体绑定资产上跑只读 SQL，取第一行第一列
        python      exec（受限 builtins）
    执行成功会累计 `execution_count` 并刷新 `last_executed`。

    参数映射：`param_mapping = {"函数参数名": "动作参数名"}`；
    未配置映射时，把动作参数（去掉 `__` 开头的内部键）整体传给函数。
    """

    async def execute(self, type_config: dict, params: dict, dry_run: bool = False) -> ExecutionResult:
        function_id = type_config.get("function_id")
        param_mapping = type_config.get("param_mapping") or {}

        if param_mapping:
            mapped_params = {
                target: params.get(source, source)
                for target, source in param_mapping.items()
            }
        else:
            mapped_params = {
                k: v for k, v in params.items() if not k.startswith("__")
            }

        if dry_run:
            return ExecutionResult(
                success=True,
                message=f"[Dry Run] Would call function {function_id}",
                output={"function_id": function_id, "params": mapped_params},
            )

        if not function_id:
            return ExecutionResult(False, "缺少 function_id，无法确定要调用的函数")

        result = await asyncio.to_thread(self._call, function_id, mapped_params)

        if not result.get("ok"):
            return ExecutionResult(
                success=False,
                message=f"函数 {result.get('name') or function_id} 执行失败：{result.get('error')}",
                output={"function_id": function_id, "error": result.get("error")},
            )

        return ExecutionResult(
            success=True,
            message=f"函数 {result.get('name')} 执行完成（{result.get('ms', 0):.1f}ms）",
            output={
                "function_id": function_id,
                "value": result.get("value"),
                "execution_ms": result.get("ms"),
            },
        )

    @staticmethod
    def _call(function_id: str, mapped_params: dict) -> dict:
        from datetime import datetime

        from app.database import SessionLocal
        from app.models.function import OntologyFunction
        from app.services.function_executor import FunctionExecutor

        db = SessionLocal()
        try:
            func = db.get(OntologyFunction, function_id)
            if func is None:
                return {"ok": False, "error": f"函数不存在: {function_id}"}
            if func.status != "active":
                return {"ok": False, "error": "函数未激活", "name": func.name}

            result = FunctionExecutor(db).execute(func, mapped_params)

            if result.success:
                func.execution_count = (func.execution_count or 0) + 1
                func.last_executed = datetime.utcnow()
                try:
                    db.commit()
                except Exception:  # noqa: BLE001 - 计数失败不该影响动作结果
                    db.rollback()
                return {
                    "ok": True,
                    "value": result.value,
                    "ms": result.execution_ms,
                    "name": func.name,
                }
            return {"ok": False, "error": result.error, "name": func.name}
        finally:
            db.close()

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "function_id": {"type": "string", "required": True, "description": "引用的函数 ID"},
            "param_mapping": {
                "type": "object", "required": False,
                "description": "参数映射：{函数参数名: 动作参数名}",
            },
        }

    @classmethod
    def get_label(cls) -> str:
        return "调用函数"

    @classmethod
    def get_description(cls) -> str:
        return "执行已注册的计算函数（expression / sql / python）"
