"""Auto-Ontology 集成的契约测试。

不连网络：用真实抓到的 SSE 行做固件，验证"什么算成功、什么算失败"。

## 为什么这层测试最重要

`adhoc_client` 是元枢和 Auto-Ontology 之间唯一的接触面。它有一堆反直觉的坑，
每一个都曾经真实地把错误答案端到用户面前：

· 引擎内部失败时**照样发 type:"result"**，只是 response 是
  "LLM failed to produce a response."、sql_code 为空 —— 早期实现把它当成功了
· 管道中途失败**不发 result**，客户端只看到流结束 —— 早期实现返回空答案
· 多库注册时必须带 target_db，否则报
  "Input validation failed for candidate_preparation"
· 单行 SSE 坏掉不能让整次查询失败

这些都在下面有固件。改契约时同步提升 CONTRACT_VERSION 并更新本文件。
"""
from __future__ import annotations

import json

import pytest

from app.services import adhoc_client
from app.services.adhoc_client import (
    CONTRACT_VERSION,
    AdHocUnavailable,
    interpret,
    parse_sse_lines,
)


def sse(*events: dict) -> list[str]:
    """把事件列表变成 Auto-Ontology 实际发出的行格式。"""
    return [f"data: {json.dumps(e, ensure_ascii=False)}" for e in events]


# ── 解析层 ──────────────────────────────────────────────────

def test_parses_steps_and_result():
    lines = sse(
        {"type": "step", "phase": "start", "node": "candidate_preparation", "label": "准备候选"},
        {"type": "step", "phase": "end", "node": "candidate_preparation", "label": "准备候选"},
        {"type": "result", "answer": {
            "response": "共 5647 条",
            "sql_code": "SELECT COUNT(*) FROM t",
            "sql_response_from_db": [{"c": 5647}],
        }},
    )
    parsed = parse_sse_lines(lines)
    assert len(parsed["steps"]) == 2
    assert parsed["steps"][0]["node"] == "candidate_preparation"
    assert parsed["result"]["sql_code"] == "SELECT COUNT(*) FROM t"
    assert parsed["failure"] is None


def test_ignores_blank_and_done_lines():
    parsed = parse_sse_lines(["", "data: [DONE]", "data:", ": keep-alive"])
    assert parsed["result"] is None
    assert parsed["steps"] == []


def test_tolerates_broken_json_line():
    """一行坏掉不能毁掉整次查询——后面还有结果。"""
    lines = [
        "data: {this is not json",
        'data: {"type":"result","answer":{"response":"ok","sql_code":"SELECT 1","sql_response_from_db":[{"a":1}]}}',
    ]
    parsed = parse_sse_lines(lines)
    assert parsed["result"]["sql_code"] == "SELECT 1"


def test_captures_error_event():
    parsed = parse_sse_lines(sse({"type": "error", "message": "boom"}))
    assert parsed["failure"] == "boom"


# ── 判定层：什么算成功 ──────────────────────────────────────

def test_success_returns_answer_sql_rows():
    parsed = parse_sse_lines(sse({"type": "result", "answer": {
        "response": "共 5647 条",
        "sql_code": "SELECT COUNT(*) FROM t",
        "sql_response_from_db": [{"c": 5647}],
    }}))
    out = interpret(parsed)
    assert out["sql"] == "SELECT COUNT(*) FROM t"
    assert out["rows"] == [{"c": 5647}]
    assert out["contractVersion"] == CONTRACT_VERSION


@pytest.mark.parametrize(
    "response_text",
    [
        "LLM failed to produce a response.",
        "llm failed to produce a response",
        "",
        "Unable to generate SQL",
    ],
)
def test_success_shaped_failure_is_rejected(response_text):
    """最危险的情况：发的是 result 事件，但其实失败了。

    早期实现直接把它当答案返回，用户看到一个"AI 说 LLM 失败了"的答案。
    """
    parsed = parse_sse_lines(sse({"type": "result", "answer": {
        "response": response_text,
        "sql_code": "",
        "sql_response_from_db": None,
    }}))
    with pytest.raises(AdHocUnavailable) as ei:
        interpret(parsed)
    assert ei.value.reason == "llm_failed"
    # 必须给出可操作的提示，而不是只说"失败"
    assert ei.value.hint and "补丁" in ei.value.hint


def test_result_without_sql_but_with_unknown_text_is_engine_failed():
    parsed = parse_sse_lines(sse({"type": "result", "answer": {
        "response": "我理解你的问题是……", "sql_code": "", "sql_response_from_db": None,
    }}))
    with pytest.raises(AdHocUnavailable) as ei:
        interpret(parsed)
    assert ei.value.reason == "engine_failed"


def test_no_result_event_raises_no_result():
    """管道中途失败：只有 step，没有 result。"""
    parsed = parse_sse_lines(sse(
        {"type": "step", "phase": "start", "node": "sql_generation", "label": "生成 SQL"},
    ))
    with pytest.raises(AdHocUnavailable) as ei:
        interpret(parsed)
    assert ei.value.reason == "no_result"
    assert "生成 SQL" in ei.value.message


def test_result_with_rows_but_no_sql_is_accepted():
    """有数据行就算成功——有些引擎版本只回行不回 SQL 文本。"""
    parsed = parse_sse_lines(sse({"type": "result", "answer": {
        "response": "共 3 条", "sql_code": "", "sql_response_from_db": [{"a": 1}],
    }}))
    out = interpret(parsed)
    assert out["rows"] == [{"a": 1}]


# ── 错误结构 ────────────────────────────────────────────────

def test_unavailable_serializes_with_hint():
    e = AdHocUnavailable("unreachable", "连不上", hint="检查容器")
    d = e.to_dict()
    assert d == {"reason": "unreachable", "message": "连不上", "hint": "检查容器"}


# ── 健康探测 ────────────────────────────────────────────────

def test_health_when_not_configured(monkeypatch):
    monkeypatch.setattr("app.config.settings.ADHOC_ENGINE_URL", "")
    adhoc_client.reset_probe_cache()
    out = adhoc_client.health()
    assert out["ok"] is False
    assert out["reason"] == "not_configured"
    assert out.get("hint")


def test_health_unreachable_gives_hint(monkeypatch):
    monkeypatch.setattr("app.config.settings.ADHOC_ENGINE_URL", "http://127.0.0.1:1")
    adhoc_client.reset_probe_cache()
    out = adhoc_client.health()
    assert out["ok"] is False
    assert out["reason"] == "unreachable"
    assert "docker compose" in (out.get("hint") or "")


def test_health_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_probe():
        calls["n"] += 1
        return {"ok": True, "baseUrl": "x"}

    monkeypatch.setattr(adhoc_client, "_probe", fake_probe)
    adhoc_client.reset_probe_cache()
    adhoc_client.health()
    adhoc_client.health()
    adhoc_client.health()
    assert calls["n"] == 1, "健康探测应带缓存，否则每次提问都多一次 HTTP 往返"

    adhoc_client.health(use_cache=False)
    assert calls["n"] == 2


def test_ask_without_base_url_raises(monkeypatch):
    monkeypatch.setattr("app.config.settings.ADHOC_ENGINE_URL", "")
    with pytest.raises(AdHocUnavailable) as ei:
        adhoc_client.ask("随便问")
    assert ei.value.reason == "not_configured"


# ── 契约版本 ────────────────────────────────────────────────

def test_contract_version_is_declared():
    """契约版本必须显式存在，且事件类型集合与解析器一致。"""
    assert isinstance(CONTRACT_VERSION, int) and CONTRACT_VERSION >= 1
    assert set(adhoc_client.SUPPORTED_EVENTS) == {"step", "result", "error"}
