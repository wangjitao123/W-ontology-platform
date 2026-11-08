"""动作脚本沙箱（父进程侧）—— AST 校验 + 子进程隔离 + 硬超时。

四层防御：
    1. **开关**  ACTION_SCRIPT_ENABLED，默认 false。不开就完全不能执行脚本。
    2. **静态校验**  复用 skill_sandbox.validate_code（模块白名单 / 内建黑名单 /
       dunder 封堵）。
    3. **子进程隔离**  脚本跑在独立 `python -m app.services._script_runner` 里，
       拿不到服务进程的会话、连接池、密钥。
    4. **硬超时**  超时后 `subprocess` 直接 kill —— 这是 `exec` 在线程里做不到的，
       死循环不会拖垮服务。

诚实说明：这仍不是不可逃逸的沙箱。Python 子进程和宿主同权限，恶意脚本若突破
静态校验仍可读写本机文件。要更强的边界需要容器/专用解释器。这里的目标是：
**默认不执行、默认拒绝危险写法、出错不拖垮服务。**
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.skill_sandbox import ALLOWED_MODULES, safe_builtins, validate_code

logger = logging.getLogger(__name__)

# 后端包根目录（app 的父目录），子进程以此为 cwd 才能 `-m app.services...`
_BACKEND_DIR = Path(__file__).resolve().parents[2]

DEFAULT_TIMEOUT_SECONDS = 3
MAX_OUTPUT_CHARS = 4000


class ScriptRejected(Exception):
    """脚本被拒绝或执行失败。reason 是机器可读原因码。"""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


def _child_env() -> dict[str, str]:
    """给子进程的最小环境变量。

    刻意不继承 os.environ：避免把 LLM_API_KEY、数据库口令等
    通过环境变量泄露给用户脚本。
    """
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(_BACKEND_DIR),
    }
    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run_script_isolated(
    script: str,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """在子进程里执行脚本，返回 {"ok", "result", "stdout"}；被拒时抛 ScriptRejected。"""
    if not settings.ACTION_SCRIPT_ENABLED:
        raise ScriptRejected(
            "script_disabled",
            "动作脚本执行已禁用（设置 ACTION_SCRIPT_ENABLED=true 才允许）",
        )

    if not script or not script.strip():
        raise ScriptRejected("empty_script", "脚本为空")

    violations = validate_code(script)
    if violations:
        raise ScriptRejected("script_rejected", "; ".join(violations))

    payload = {
        "script": script,
        "params": params or {},
        "builtins": sorted(safe_builtins().keys()),
        "modules": sorted(ALLOWED_MODULES),
    }

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.services._script_runner"],
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(_BACKEND_DIR),
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        raise ScriptRejected(
            "script_timeout", f"脚本执行超过 {timeout}s，已终止子进程",
        ) from None
    except FileNotFoundError:
        raise ScriptRejected("runner_missing", "找不到 Python 解释器，无法启动脚本子进程") from None

    out = (proc.stdout or "").strip()
    if not out:
        detail = (proc.stderr or "").strip()[:MAX_OUTPUT_CHARS]
        raise ScriptRejected("script_failed", f"子进程无输出（exit={proc.returncode}）：{detail}")

    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        raise ScriptRejected(
            "bad_output", f"子进程输出不是 JSON：{out[:MAX_OUTPUT_CHARS]}",
        ) from None

    if not result.get("ok"):
        raise ScriptRejected(
            "script_error", str(result.get("error", "脚本执行失败"))[:MAX_OUTPUT_CHARS],
        )

    return {
        "ok": True,
        "result": result.get("result") or {},
        "stdout": (result.get("stdout") or "")[:MAX_OUTPUT_CHARS],
    }


def validate_script(script: str) -> list[str]:
    """只做静态校验，不执行（供管理端预览用）。"""
    return validate_code(script)
