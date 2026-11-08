import asyncio
from string import Template

from .base import BaseActionExecutor, ExecutionResult


class SqlExecExecutor(BaseActionExecutor):
    """在数据资产上执行 SQL。

    改造说明：原实现只把模板替换完就返回 "Executed SQL ... rows_affected: 0"，
    真正一条语句都没跑。现在通过 `action_writeback.execute_asset_sql` 走
    ExecuteService（AST 审查 + 表白名单 + 写回 purpose 白名单 + Connection.writable）。

    执行作用域是 **Asset**（数据面以 Asset 为单位做白名单与审计归因），
    因此配置项从 `connection_id` 改为 `asset_id`；旧配置会得到明确报错而不是假成功。
    """

    async def execute(self, type_config: dict, params: dict, dry_run: bool = False) -> ExecutionResult:
        asset_id = type_config.get("asset_id")
        connection_id = type_config.get("connection_id")
        sql_template = type_config.get("sql", "")
        sql = Template(sql_template).safe_substitute(params)

        if dry_run:
            return ExecutionResult(
                success=True,
                message="[Dry Run] SQL 已渲染，未执行",
                output={"sql": sql, "asset_id": asset_id, "connection_id": connection_id},
            )

        if not asset_id:
            hint = (
                "（配置里只有 connection_id；数据面以 Asset 为执行作用域，请改配 asset_id）"
                if connection_id else ""
            )
            return ExecutionResult(
                False, f"缺少 asset_id，无法执行 SQL{hint}",
                {"sql": sql, "connection_id": connection_id},
            )

        result = await asyncio.to_thread(self._run, asset_id, sql, params)
        return ExecutionResult(
            success=bool(result.get("ok")),
            message=result.get("message", ""),
            output=result.get("output"),
        )

    @staticmethod
    def _run(asset_id: str, sql: str, params: dict) -> dict:
        from app.database import SessionLocal
        from app.services.action_writeback import execute_asset_sql

        db = SessionLocal()
        try:
            return execute_asset_sql(db, asset_id, sql, params)
        finally:
            db.close()

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "asset_id": {"type": "string", "required": True, "description": "执行作用域的资产 ID"},
            "sql": {"type": "string", "required": True, "description": "SQL 语句，支持 $param 占位符"},
        }

    @classmethod
    def get_label(cls) -> str:
        return "执行 SQL"

    @classmethod
    def get_description(cls) -> str:
        return "在指定数据资产上执行 SQL（受 AST 审查与表白名单约束）"
