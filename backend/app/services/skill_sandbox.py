"""技能代码安全校验与沙箱执行。

安全模型（纵深防御，但都不是绝对边界）：
    1. **模块白名单** —— 只有 ALLOWED_MODULES 里的模块可以 import。
    2. **调用黑名单** —— exec/eval/compile/__import__/open/getattr/globals 等一律禁用。
    3. **dunder 封堵** —— 任何以下划线开头的属性访问或名字都被拒绝，
       这是挡住 `().__class__.__bases__[0].__subclasses__()` 这类逃逸链的关键。
    4. **受限 builtins** —— 命名空间里只放安全的内建函数。

⚠️ 这些是**降低风险**的措施，不是强隔离。纯 Python 层面的沙箱无法抵御
   CPython 解释器漏洞或有心构造的逃逸链。真正需要强隔离时，应把代码放到
   独立进程/容器里执行，见 `script_sandbox.py`（动作脚本走那条路径）。

历史问题（已修）：
    - ALLOWED_MODULES 定义了却从未使用 → `import pickle` 等可借此拿到 RCE
    - 没有 dunder 检查 → 用 `__class__` 链可绕开所有黑名单
    - `getattr` 在白名单里 → `getattr(x, "__globals__")` 用字符串绕开 AST 检查
"""
from __future__ import annotations

import ast
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 明确禁止的模块（即使误加进白名单也会被挡）
FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket",
    "requests", "urllib", "http", "ftplib", "smtplib",
    "ctypes", "importlib", "pathlib", "glob", "tempfile",
    "pickle", "marshal", "shelve", "builtins", "gc", "code",
    "pty", "posix", "nt", "signal", "threading", "multiprocessing",
}

# 唯一允许导入的模块（白名单，未列出的一律拒绝）
ALLOWED_MODULES = {
    "json", "re", "datetime", "math", "collections",
    "itertools", "functools", "decimal", "statistics",
    "typing", "dataclasses", "string", "random",
}

# 禁止直接调用的内建
FORBIDDEN_BUILTINS = {
    "exec", "eval", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "memoryview", "help", "exit", "quit", "dir", "object",
}

# 禁止调用的方法名（覆盖 os.system / 文件句柄等常见路径）
FORBIDDEN_METHODS = {
    "system", "popen", "spawn", "fork", "execv", "execve",
    "read_text", "write_text", "read_bytes", "write_bytes",
    "loads", "load", "dumps_unsafe",
}


class CodeValidationError(Exception):
    pass


def validate_code(code: str) -> list[str]:
    """AST 级静态检查，返回违规说明列表（空列表表示通过）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    violations: list[str] = []
    for node in ast.walk(tree):
        # 1) import 白名单
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in FORBIDDEN_MODULES:
                    violations.append(f"禁止导入模块: {mod}")
                elif mod not in ALLOWED_MODULES:
                    violations.append(f"模块不在白名单: {mod}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                violations.append(f"禁止从模块导入: {mod}")
            elif mod and mod not in ALLOWED_MODULES:
                violations.append(f"模块不在白名单: {mod}")

        # 2) 危险内建调用
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                violations.append(f"禁止调用内建: {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_METHODS:
                violations.append(f"禁止调用方法: {func.attr}")

        # 3) dunder 属性访问（逃逸链的入口）
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                violations.append(f"禁止访问下划线属性: {node.attr}")

        # 4) dunder 名字
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                violations.append(f"禁止使用下划线名字: {node.id}")

        # 5) 全局/闭包改写
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            violations.append("禁止使用 global/nonlocal")

    # 去重但保持顺序
    seen: set[str] = set()
    unique = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def check_code(code: str) -> None:
    """校验不通过直接抛异常（调用方通常只需要这个）。"""
    violations = validate_code(code)
    if violations:
        raise CodeValidationError("; ".join(violations))


def execute_in_sandbox(code: str, func_name: str, params: dict, context: dict | None = None) -> Any:
    """在当前进程的受限命名空间中执行函数。

    ⚠️ 没有超时控制：死循环会一直占着调用线程。需要硬超时的场景请用
       `script_sandbox.run_script_isolated`（子进程 + kill）。
    """
    check_code(code)

    namespace: dict[str, Any] = {"__builtins__": _safe_builtins()}
    if context:
        namespace.update(context)

    exec(code, namespace)  # noqa: S102 - 已通过 AST 白名单校验

    if func_name not in namespace:
        raise CodeValidationError(f"代码中找不到函数 '{func_name}'")

    func = namespace[func_name]
    return func(**params)


def _safe_builtins() -> dict:
    """受限 builtins —— 无文件/网络/动态执行能力。"""
    import builtins

    safe: dict[str, Any] = {}
    allowed = [
        "abs", "all", "any", "bool", "dict", "enumerate", "filter",
        "float", "frozenset", "hasattr", "int", "isinstance",
        "issubclass", "len", "list", "map", "max", "min", "next",
        "print", "range", "repr", "reversed", "round", "set", "slice",
        "sorted", "str", "sum", "tuple", "zip",
        "None", "True", "False",
    ]
    for name in allowed:
        if hasattr(builtins, name):
            safe[name] = getattr(builtins, name)
    # 注意：刻意不提供 getattr / setattr / type / open / __import__
    # getattr 是最经典的沙箱逃逸入口（getattr(x, "__globals__")）。
    return safe


def safe_builtins() -> dict:
    """公开别名：供 `script_sandbox` 等模块复用同一份白名单。"""
    return _safe_builtins()


def safe_globals() -> dict:
    """给执行器用的初始全局命名空间（含白名单模块）。"""
    import json
    import math
    import random
    import re
    import statistics
    from datetime import datetime, timedelta

    return {
        "__builtins__": _safe_builtins(),
        "json": json,
        "math": math,
        "re": re,
        "random": random,
        "statistics": statistics,
        "datetime": datetime,
        "timedelta": timedelta,
    }
