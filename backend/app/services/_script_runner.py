"""动作脚本子进程入口。

由 `script_sandbox.run_script_isolated` 以 `python -m app.services._script_runner`
启动，走 stdin/stdout 传 JSON：

    输入  {"script": "...", "params": {...},
           "builtins": ["len", "str", ...], "modules": ["json", "math", ...]}
    输出  {"ok": true,  "result": {...}, "stdout": "..."}
          {"ok": false, "error": "...",  "stdout": "..."}

为什么单独开进程：
    纯 Python 沙箱挡不住死循环，而 `exec` 跑在线程里无法被可靠中断。
    子进程可以被 `subprocess` 直接 kill，也给了一个真正的隔离边界
    （脚本碰不到服务进程里的 ORM 会话、连接池、配置密钥）。

注意：白名单由父进程传入，避免在两处各维护一份。
"""
from __future__ import annotations

import builtins
import contextlib
import importlib
import io
import json
import sys
import traceback

MAX_STDOUT_CHARS = 4000


def _emit(payload: dict) -> None:
    try:
        sys.stdout.write(json.dumps(payload, default=str))
    except Exception:
        sys.stdout.write(json.dumps({"ok": False, "error": "结果无法序列化"}))
    sys.stdout.flush()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _emit({"ok": False, "error": "入参不是合法 JSON"})
        return 0

    script = payload.get("script") or ""
    params = payload.get("params") or {}
    builtin_names = payload.get("builtins") or []
    module_names = payload.get("modules") or []

    safe: dict = {}
    for name in builtin_names:
        obj = getattr(builtins, name, None)
        if obj is not None:
            safe[name] = obj

    # 受限 __import__：import 语句在字节码层依赖它，没有它连 `import json` 都会失败。
    # 这里只放行白名单模块，是 AST 校验之外的第二道防线
    # （脚本直接写 __import__('os') 会被 AST 拦，这里再拦一次）。
    allowed_modules = set(module_names)

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if level != 0:
            raise ImportError("禁止相对导入")
        root = (name or "").split(".")[0]
        if root not in allowed_modules:
            raise ImportError(f"模块不在白名单: {name}")
        return importlib.import_module(name)

    safe["__import__"] = _safe_import

    namespace: dict = {
        "__builtins__": safe,
        "params": params,
        "result": {},
    }
    # 预置白名单模块，脚本可直接用 `json.dumps(...)` 而不必 import
    for mod in module_names:
        try:
            namespace[mod] = importlib.import_module(mod)
        except Exception:
            # 允许模块缺失：脚本若不使用该模块就不该因此失败
            pass

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(script, "<action-script>", "exec"), namespace)  # noqa: S102
    except Exception:
        _emit({
            "ok": False,
            "error": traceback.format_exc(limit=6),
            "stdout": buf.getvalue()[:MAX_STDOUT_CHARS],
        })
        return 0

    _emit({
        "ok": True,
        "result": namespace.get("result", {}),
        "stdout": buf.getvalue()[:MAX_STDOUT_CHARS],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
