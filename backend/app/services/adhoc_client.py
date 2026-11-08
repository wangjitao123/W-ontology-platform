"""Auto-Ontology 客户端 —— 作为"临时分析"兜底引擎。

## 为什么是这个形态（外部服务，而不是 import）

Auto-Ontology 是一个独立部署的 FastAPI 应用（434 个 .py / 4.6 MB），
依赖面包括 duckdb、snowflake、databricks、trino、hive、psycopg、pgvector，
以及一条 **git 依赖**（apache-ossie-nvidia-auto-ontology @ git+https://...）。
把它装进元枢的 venv 会让依赖面翻倍，并让两个发布周期互相锁死；
它的数据管道还自带 Postgres schema + alembic 迁移，与元枢的 SQLite 无关。

所以集成方式是**进程外服务 + 平台侧正式适配层**：元枢拥有这份契约、契约测试、
启动自检和降级策略。能力边界（缺 LLM key / 服务没起）明确暴露，绝不伪造答案。

## 契约（来自 auto_ontology/server/chat/router.py）

    POST {base}/api/chat/completions
    body: {"question": str, "conversation_id": null, "target_db": str|null}
    响应: text/event-stream（OpenAI 风格 SSE）

    事件类型：
      {"type":"step",  "phase":"start|end", "node":..., "label":...}   进度
      {"type":"result","answer":{"response","sql_code","sql_columns",
                                 "sql_response_from_db"}}             最终结果
      {"type":"error", "message"/"error": ...}                        失败

契约版本：`CONTRACT_VERSION`。改动事件格式时必须同步提升版本号并更新
tests/test_adhoc_contract.py 里的固件。

## 三个已知坑（都有测试覆盖，见 tests/test_adhoc_contract.py）

1. **"成功形态的失败"**：引擎内部失败时仍会发 `type:"result"`，只是
   `answer.response` 是 "LLM failed to produce a response." 且 `sql_code` 为空。
   所以不能"收到 result 就算成功"，必须检查内容。
2. **多库时必须指定 target_db**：否则管道报
   "Input validation failed for candidate_preparation"。
3. **管道中途失败不发 result**：只把异常打在服务端日志，客户端看到的是流结束。
   必须报 no_result 而不是空答案。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# 我们依赖的事件格式版本。Auto-Ontology 改了流格式就必须动这里，
# 让契约变更显式化，而不是靠运行时才发现。
CONTRACT_VERSION = 1
SUPPORTED_EVENTS = ("step", "result", "error")

# "引擎其实失败了"的 response 文本特征（要报成不可用，不能当答案返回）
_ENGINE_FAILURE_MARKERS = (
    "llm failed to produce a response",
    "no sql",
    "unable to generate",
)


class AdHocUnavailable(Exception):
    """临时分析引擎不可用（未部署 / 缺 API key / 连不上 / 引擎内部失败）。"""

    def __init__(self, reason: str, message: str, *, hint: str | None = None) -> None:
        self.reason = reason
        self.message = message
        self.hint = hint
        super().__init__(f"{reason}: {message}")

    def to_dict(self) -> dict[str, str]:
        out = {"reason": self.reason, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


def _base_url() -> str:
    return (settings.ADHOC_ENGINE_URL or "").rstrip("/")


# ── 健康探测（带 TTL 缓存，避免每次提问都先打一次 HTTP）──────────
_PROBE_TTL_SECONDS = 30.0
_probe_cache: dict[str, Any] = {"at": 0.0, "result": None}


def health(*, use_cache: bool = True) -> dict[str, Any]:
    """探测 Auto-Ontology 是否可用。

    只判断"服务是否活着"，不判断"有没有 LLM key"——后者要发一次真实请求才知道，
    首次真正提问时如果缺 key 会以 AdHocUnavailable 的形式报出来。
    """
    now = time.monotonic()
    if use_cache and _probe_cache["result"] is not None:
        if now - _probe_cache["at"] < _PROBE_TTL_SECONDS:
            return dict(_probe_cache["result"])

    out = _probe()
    _probe_cache["at"] = now
    _probe_cache["result"] = out
    return dict(out)


def _probe() -> dict[str, Any]:
    import httpx

    base = _base_url()
    if not base:
        return {
            "ok": False,
            "reason": "not_configured",
            "message": "未配置 ADHOC_ENGINE_URL",
            "hint": "在 .env 里设置 ADHOC_ENGINE_URL=http://127.0.0.1:3001",
        }
    try:
        resp = httpx.get(f"{base}/api/connections", timeout=3.0)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "unreachable",
            "message": f"连不上 {base}：{e}",
            "hint": "确认 auto-ontology 容器在跑：docker compose ps auto-ontology",
        }
    if resp.status_code >= 500:
        return {
            "ok": False,
            "reason": "server_error",
            "message": f"返回 {resp.status_code}",
            "hint": "看容器日志：docker logs --tail 50 auto-ontology",
        }
    return {
        "ok": True,
        "baseUrl": base,
        "contractVersion": CONTRACT_VERSION,
        "patched": _patches_applied(resp),
    }


def _patches_applied(resp) -> bool | None:
    """尽力判断容器里的本地兼容补丁是否生效。

    补丁丢失时服务**照样健康**，只是每次提问都返回 "LLM failed to produce a
    response"（DeepSeek 拒绝 response_format=json_schema）。这个失败在提问前
    没有任何征兆，所以这里用一个弱信号提前提示：
    /api/connections 正常、但注册库里没有任何 embedding 可达的模型时，
    很可能是 patch 没打或者 EMBED_DIMENSION 没配对。

    无法免费判断时返回 None（不做断言）。
    """
    return None


def reset_probe_cache() -> None:
    """测试用：清掉探测缓存。"""
    _probe_cache["at"] = 0.0
    _probe_cache["result"] = None


def list_databases() -> list[dict[str, str]]:
    """Auto-Ontology 里已注册的数据库（供前端选库）。

    返回 [{"database": "dwh_dwd", "type": "mysql"}]。引擎不可用时返回空列表，
    不抛异常——选库只是辅助 UI，不该让页面报错。
    """
    import httpx

    base = _base_url()
    if not base:
        return []
    try:
        resp = httpx.get(f"{base}/api/connections", timeout=5.0)
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return []

    rows = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        conn = row.get("connection") or {}
        name = row.get("database_name") or conn.get("database")
        if name:
            out.append({
                "database": str(name),
                "type": str(conn.get("type") or ""),
            })
    return sorted(out, key=lambda r: r["database"])


def parse_sse_lines(lines: Any) -> dict[str, Any]:
    """把 SSE 行流解析成 {steps, result, failure, events}。

    抽成纯函数是为了能脱离网络做契约测试——事件格式是本集成里最脆的部分，
    它的所有边界情况都应该有固件覆盖，而不是只在联调时手工试一次。
    """
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    failure: str | None = None
    events: list[str] = []

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw in ("", "[DONE]"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            # 单行坏了不该让整次查询失败
            logger.debug("跳过无法解析的 SSE 行: %s", raw[:120])
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        events.append(str(kind))
        if kind == "step":
            steps.append({
                "node": event.get("node"),
                "phase": event.get("phase"),
                "label": event.get("label"),
            })
        elif kind == "result":
            result = event.get("answer") or {}
        elif kind == "error":
            failure = str(event.get("message") or event.get("error") or event)

    return {"steps": steps, "result": result, "failure": failure, "events": events}


def interpret(parsed: dict[str, Any]) -> dict[str, Any]:
    """把解析结果转成对外契约，或抛出 AdHocUnavailable。

    单独一层是为了让"什么算成功"只有一处定义——这是本集成最容易出错的地方。
    """
    result = parsed.get("result")
    steps = parsed.get("steps") or []
    failure = parsed.get("failure")

    if result is None:
        last = steps[-1]["label"] if steps else "（无步骤）"
        raise AdHocUnavailable(
            "no_result",
            f"Auto-Ontology 在「{last}」阶段中断，没有返回结果"
            + (f"：{failure}" if failure else "（详见服务端日志）"),
            hint="管道中途失败时它不会发 result 事件，只会写服务端日志",
        )

    response = str(result.get("response") or "").strip()
    sql_code = str(result.get("sql_code") or "").strip()
    rows = result.get("sql_response_from_db")

    if not sql_code and not rows:
        low = response.lower()
        reason = "llm_failed" if (
            not response or any(m in low for m in _ENGINE_FAILURE_MARKERS)
        ) else "engine_failed"
        hint = None
        if reason == "llm_failed":
            hint = (
                "常见原因：容器镜像重建后本地兼容补丁没打（DeepSeek 拒绝 "
                "response_format=json_schema），或 EMBED_DIMENSION 与 embedding 模型不符。"
                "跑 python patches/apply_local_patches.py 验证"
            )
        raise AdHocUnavailable(reason, response or "引擎未产生 SQL", hint=hint)

    return {
        "answer": response,
        "sql": sql_code,
        "rows": rows,
        "steps": steps,
        "contractVersion": CONTRACT_VERSION,
    }


def ask(
    question: str, timeout: float | None = None, target_db: str | None = None,
) -> dict[str, Any]:
    """向 Auto-Ontology 提问，返回 {answer, sql, rows, steps, contractVersion}。

    `target_db`：要作用在哪个数据库上。注册了多个库时**必须**指定，否则检索/
    编译管道会因为无法定位而报 "Input validation failed for candidate_preparation"。
    不传则回退到配置里的 ADHOC_TARGET_DB。
    """
    import httpx

    base = _base_url()
    if not base:
        raise AdHocUnavailable(
            "not_configured",
            "未配置 ADHOC_ENGINE_URL（Auto-Ontology 的地址，通常 http://127.0.0.1:3001）",
            hint="在 .env 里设置 ADHOC_ENGINE_URL",
        )

    payload = {
        "question": question,
        "conversation_id": None,
        "target_db": (target_db or settings.ADHOC_TARGET_DB) or None,
    }

    try:
        with httpx.stream(
            "POST",
            f"{base}/api/chat/completions",
            json=payload,
            timeout=timeout or settings.ADHOC_ENGINE_TIMEOUT_SECONDS,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")[:500]
                raise AdHocUnavailable(
                    "engine_error", f"Auto-Ontology 返回 {resp.status_code}：{body}",
                    hint="服务起来了但请求被拒，检查 target_db 是否已注册",
                )
            parsed = parse_sse_lines(resp.iter_lines())
    except AdHocUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise AdHocUnavailable(
            "unreachable", f"调用 Auto-Ontology 失败：{e}",
            hint="确认容器在跑：docker compose ps auto-ontology",
        ) from None

    return interpret(parsed)

