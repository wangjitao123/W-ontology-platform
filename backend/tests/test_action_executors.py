import pytest
from app.services.action_executors import get_executor, get_all_type_info
from app.services.action_executors.base import ExecutionResult


@pytest.mark.asyncio
async def test_api_call_dry_run():
    executor = get_executor("api_call")
    config = {"url": "https://example.com/api/$id", "method": "POST", "body": {"name": "$name"}}
    result = await executor.execute(config, {"id": "123", "name": "test"}, dry_run=True)
    assert result.success is True
    assert "[Dry Run]" in result.message
    assert result.output["url"] == "https://example.com/api/123"


@pytest.mark.asyncio
async def test_sql_exec_dry_run():
    """sql_exec 现在要求 asset_id（数据面以 Asset 为执行作用域）。"""
    executor = get_executor("sql_exec")
    config = {"asset_id": "asset_1", "sql": "UPDATE users SET status='$status' WHERE id=$id"}
    result = await executor.execute(config, {"status": "active", "id": "42"}, dry_run=True)
    assert result.success is True
    assert result.output["asset_id"] == "asset_1"
    assert "active" in result.output["sql"]


@pytest.mark.asyncio
async def test_sql_exec_without_asset_id_fails_loudly():
    """旧配置只给 connection_id 时必须明确失败，而不是假成功。"""
    executor = get_executor("sql_exec")
    config = {"connection_id": "conn_1", "sql": "SELECT 1"}
    result = await executor.execute(config, {}, dry_run=False)
    assert result.success is False
    assert "asset_id" in result.message


@pytest.mark.asyncio
async def test_modify_attribute_dry_run():
    executor = get_executor("modify_attribute")
    config = {"target_entity_id": "e1", "target_attribute": "price", "value_expression": "new_price"}
    result = await executor.execute(config, {"new_price": "99.9"}, dry_run=True)
    assert result.success is True
    assert "price" in result.message
    assert result.output["value"] == "99.9"


@pytest.mark.asyncio
async def test_notification_dry_run():
    executor = get_executor("notification")
    config = {"channel": "email", "recipient": "$email", "message_template": "Hello $name"}
    result = await executor.execute(config, {"email": "a@b.com", "name": "Alice"}, dry_run=True)
    assert result.success is True
    assert result.output["recipient"] == "a@b.com"
    assert result.output["message"] == "Hello Alice"


@pytest.mark.asyncio
async def test_call_function_dry_run():
    executor = get_executor("call_function")
    config = {"function_id": "fn_1", "param_mapping": {"x": "input_x"}}
    result = await executor.execute(config, {"input_x": "hello"}, dry_run=True)
    assert result.success is True
    assert result.output["function_id"] == "fn_1"
    assert result.output["params"]["x"] == "hello"


@pytest.mark.asyncio
async def test_custom_script_disabled_by_default():
    """默认关闭：脚本不能执行，且要明确报告原因，不能假成功。"""
    from app.config import settings

    original = settings.ACTION_SCRIPT_ENABLED
    settings.ACTION_SCRIPT_ENABLED = False
    try:
        executor = get_executor("custom_script")
        result = await executor.execute(
            {"script": "result['total'] = 1"}, {}, dry_run=False,
        )
        assert result.success is False
        assert result.output["reason"] == "script_disabled"
    finally:
        settings.ACTION_SCRIPT_ENABLED = original


@pytest.mark.asyncio
async def test_custom_script_rejects_dangerous_code():
    """打开开关后，AST 校验必须拦住 import os。"""
    from app.config import settings

    original = settings.ACTION_SCRIPT_ENABLED
    settings.ACTION_SCRIPT_ENABLED = True
    try:
        executor = get_executor("custom_script")
        result = await executor.execute(
            {"script": "import os\nresult['x'] = os.getcwd()"}, {}, dry_run=False,
        )
        assert result.success is False
        assert result.output["reason"] == "script_rejected"
    finally:
        settings.ACTION_SCRIPT_ENABLED = original


@pytest.mark.asyncio
async def test_custom_script_runs_in_subprocess():
    """打开开关后，脚本在子进程里执行并回传 result。"""
    from app.config import settings

    original = settings.ACTION_SCRIPT_ENABLED
    settings.ACTION_SCRIPT_ENABLED = True
    try:
        executor = get_executor("custom_script")
        config = {"script": "result['total'] = params['a'] + params['b']"}
        result = await executor.execute(config, {"a": 1, "b": 2}, dry_run=False)
        assert result.success is True
        assert result.output["result"]["total"] == 3
    finally:
        settings.ACTION_SCRIPT_ENABLED = original


def test_get_all_type_info():
    types = get_all_type_info()
    assert len(types) == 6
    type_keys = [t["type_key"] for t in types]
    assert "api_call" in type_keys
    assert "custom_script" in type_keys


def test_get_executor_unknown_type():
    with pytest.raises(ValueError, match="Unknown action type"):
        get_executor("nonexistent")
