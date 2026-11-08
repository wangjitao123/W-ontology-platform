import asyncio

from .base import BaseActionExecutor, ExecutionResult


class CustomScriptExecutor(BaseActionExecutor):
    """执行用户自定义 Python 脚本。

    改造说明（原来的实现是服务端最严重的风险点）：
        原代码 `exec(script, {"__builtins__": __builtins__}, local_vars)`
          - 把**完整 builtins** 交给脚本 → import os / 读写任意文件 / 发网络请求
          - 在 async handler 里**同步**执行 → 一个 `while True:` 就冻结整个服务
          - 没有任何超时或隔离

    现在的执行链：
        ACTION_SCRIPT_ENABLED 开关（默认关）
          → AST 白名单校验（禁 import 白名单外模块、禁 exec/eval/open/getattr、禁 dunder）
          → 子进程执行（`app.services._script_runner`），带受限 builtins
          → 硬超时后 kill 子进程

    脚本约定：从 `params` 读入参，把结果写进 `result` 字典。
    """

    async def execute(self, type_config: dict, params: dict, dry_run: bool = False) -> ExecutionResult:
        from app.config import settings
        from app.services.script_sandbox import (
            ScriptRejected, run_script_isolated, validate_script,
        )

        script = type_config.get("script", "")

        if dry_run:
            violations = validate_script(script)
            return ExecutionResult(
                success=not violations,
                message=(
                    "[Dry Run] 脚本静态校验通过，未执行"
                    if not violations
                    else "[Dry Run] 脚本静态校验未通过：" + "; ".join(violations)
                ),
                output={
                    "script_length": len(script),
                    "violations": violations,
                    "scriptEnabled": settings.ACTION_SCRIPT_ENABLED,
                },
            )

        try:
            out = await asyncio.to_thread(
                run_script_isolated,
                script,
                params,
                settings.ACTION_SCRIPT_TIMEOUT_SECONDS,
            )
        except ScriptRejected as e:
            return ExecutionResult(
                success=False,
                message=f"脚本未执行：{e.message}",
                output={"reason": e.reason},
            )

        return ExecutionResult(
            success=True,
            message="脚本执行完成（子进程隔离）",
            output={
                "result": out.get("result") or {},
                "stdout": out.get("stdout") or "",
            },
        )

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "script": {
                "type": "string", "required": True,
                "description": "Python 脚本：从 params 读入参，结果写入 result；"
                               "禁 import（白名单外）/exec/eval/open/dunder",
            },
        }

    @classmethod
    def get_label(cls) -> str:
        return "自定义脚本"

    @classmethod
    def get_description(cls) -> str:
        return "在隔离子进程中运行受限 Python 脚本（默认关闭）"
