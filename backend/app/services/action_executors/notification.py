import asyncio
from string import Template

from .base import BaseActionExecutor, ExecutionResult


class NotificationExecutor(BaseActionExecutor):
    """发送通知。

    改造说明：原实现只返回一句 "Notification sent to ... via ..."，没有任何投递行为。
    现在按渠道真实处理：
        system   落库到 action_notifications（站内消息，可查询）
        webhook  真的 POST（httpx，3s 超时）
        email/sms/im
                 记录为 not_implemented —— 明确失败，不假装成功

    动作上下文（action id/name）由 `actions.py` 以 `__action_id` / `__action_name`
    注入到 params，便于把通知追溯到具体动作。
    """

    async def execute(self, type_config: dict, params: dict, dry_run: bool = False) -> ExecutionResult:
        channel = (type_config.get("channel") or "system").strip().lower()
        recipient = Template(type_config.get("recipient", "")).safe_substitute(params)
        message = Template(type_config.get("message_template", "")).safe_substitute(params)

        action_id = params.get("__action_id")
        action_name = params.get("__action_name")

        if dry_run:
            return ExecutionResult(
                success=True,
                message=f"[Dry Run] Would send to {recipient or '(站内消息)'} via {channel}",
                output={
                    "channel": channel, "recipient": recipient, "message": message,
                },
            )

        result = await asyncio.to_thread(
            self._deliver, action_id, action_name, channel, recipient, message,
        )

        if result.get("status") == "not_implemented":
            return ExecutionResult(
                success=False,
                message=f"渠道 {channel} 未接入：{result.get('error')}",
                output={
                    "channel": channel, "status": "not_implemented",
                    "notification_id": result.get("id"),
                },
            )

        return ExecutionResult(
            success=bool(result.get("ok")),
            message=(
                f"通知已投递（{channel}，id={result.get('id')}）"
                if result.get("ok")
                else f"通知投递失败（{channel}）：{result.get('error')}"
            ),
            output={
                "channel": channel,
                "recipient": recipient,
                "status": result.get("status"),
                "notification_id": result.get("id"),
            },
        )

    @staticmethod
    def _deliver(action_id, action_name, channel, recipient, message) -> dict:
        from app.database import SessionLocal
        from app.services.notification_service import deliver

        db = SessionLocal()
        try:
            return deliver(
                db,
                action_id=action_id,
                action_name=action_name,
                channel=channel,
                recipient=recipient or None,
                message=message,
            )
        finally:
            db.close()

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "channel": {
                "type": "string", "required": True,
                "description": "渠道：system（站内）/ webhook（需填 URL）",
            },
            "recipient": {
                "type": "string", "required": False,
                "description": "接收方：webhook 渠道填目标 URL",
            },
            "message_template": {
                "type": "string", "required": True,
                "description": "消息模板，支持 $param 占位符",
            },
        }

    @classmethod
    def get_label(cls) -> str:
        return "发送通知"

    @classmethod
    def get_description(cls) -> str:
        return "投递通知：站内消息落库，webhook 真实发送"
