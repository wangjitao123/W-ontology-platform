"""方案 B：按问题类型路由 —— "受治理口径" 还是 "临时分析"。

核心思路：
    Cube 答不了任意 SQL（它只能拼 measure/dimension/filter），但它的数是受治理的。
    Auto-Ontology 能编任意 SQL，但它的数不属于任何口径。

    所以不做"串起来"，而是**分流 + 标注**：
        能用已定义指标回答的问题 → Cube      → governed = true
        其余问题                 → Auto-Ontology → governed = false（明确标注）
        兜底引擎不可用           → 如实报告 unavailable，绝不伪造答案

分类器用项目已有的 LLM 客户端（DeepSeek），而不是硬编码关键词——
因为"资产总额"和"总资产"这种同义表述靠字符串匹配很容易漏。
LLM 只做"选指标 + 填参数"，不做 SQL 生成：SQL 仍由 Cube 按模型生成，
所以哪怕是 LLM 也无法绕过口径。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services import adhoc_client
from app.services.adhoc_client import AdHocUnavailable
from app.services.metrics_cube import CubeClient, CubeError

logger = logging.getLogger(__name__)

# 交给 LLM 的目录最多列多少个指标/维度（避免 prompt 过长）
_MAX_MEASURES_PER_CUBE = 60
_MAX_DIMENSIONS_PER_CUBE = 40

_CLASSIFY_SYSTEM = """你是数据平台的问数路由器。你的唯一任务是判断问题该由哪个引擎回答，并给出查询参数。

有两个引擎：
1. cube —— 指标层。只能查"已定义的指标 + 维度 + 过滤"。数是受治理的、口径统一的。
2. ad_hoc —— 临时分析。能写任意 SQL。数不受治理，只能作为探索参考。

判断规则：
- 如果问题能用下面目录里**已存在的指标**回答（哪怕需要按维度分组、按时间聚合），选 cube。
- 如果问题需要目录里没有的计算（窗口函数、跨行比较、自定义公式、关联多表的新口径），选 ad_hoc。
- 拿不准时选 ad_hoc。

输出严格 JSON，不要多余文字：
{"route":"cube"|"ad_hoc","reason":"一句话理由","query":{"measures":["cube.measure"],"dimensions":["cube.dimension"],"filters":[{"member":"cube.dimension","operator":"equals","values":["值"]}],"timeDimensions":[{"dimension":"cube.dim","granularity":"month"}],"limit":50}}

约束：
- measures 必须来自目录，且至少一个。
- dimensions/filters/timeDimensions 可选，成员也必须来自目录。
- 数字类过滤用 equals；时间不要写死成 filters，优先用 timeDimensions。
"""


def _format_catalog(cubes: list[dict[str, Any]]) -> str:
    """把 Cube 目录压成紧凑文本喂给 LLM。"""
    lines: list[str] = []
    for c in cubes:
        measures = c.get("measures") or []
        dims = c.get("dimensions") or []
        if not measures:
            continue
        lines.append(f"\n## {c['name']} — {c.get('title') or ''}")
        for m in measures[:_MAX_MEASURES_PER_CUBE]:
            term = _short_title(m.get("title"), c.get("title") or "")
            lines.append(f"- M {m['name']} = {term}")
        for d in dims[:_MAX_DIMENSIONS_PER_CUBE]:
            term = _short_title(d.get("title"), c.get("title") or "")
            lines.append(f"- D {d['name']} = {term} ({d.get('type')})")
    return "\n".join(lines)


def _short_title(title: str | None, cube_title: str) -> str:
    """Cube 的 title 是 "{cube标题} {成员标题}"，去掉前缀留下可读的中文名。"""
    t = (title or "").strip()
    if cube_title and t.startswith(cube_title):
        t = t[len(cube_title):].strip()
    return t or "(未命名)"


def _parse_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON（容忍 ```json 包裹）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s
        s = s.lstrip("json").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _validate_query(
    query: dict[str, Any], catalog_by_cube: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """校验 LLM 给的查询：指标/维度必须真实存在，否则拒绝。

    这一步是"LLM 不能越过口径"的保证——它只能选已定义的成员，
    编不出一个新指标。Cube 本身也会拒绝未知成员，这里提前给出可读原因。
    """
    measures = [m for m in (query.get("measures") or []) if isinstance(m, str)]
    if not measures:
        return None, "没有选出任何指标"

    valid_measures: set[str] = set()
    valid_dims: set[str] = set()
    for c in catalog_by_cube.values():
        valid_measures.update(m["name"] for m in (c.get("measures") or []))
        valid_dims.update(d["name"] for d in (c.get("dimensions") or []))

    bad = [m for m in measures if m not in valid_measures]
    if bad:
        return None, f"指标不存在: {', '.join(bad[:3])}"

    dims = [d for d in (query.get("dimensions") or []) if isinstance(d, str)]
    bad_dims = [d for d in dims if d not in valid_dims]
    if bad_dims:
        return None, f"维度不存在: {', '.join(bad_dims[:3])}"

    filters = []
    for f in (query.get("filters") or []):
        if isinstance(f, dict) and f.get("member") in valid_dims and f.get("values"):
            filters.append({
                "member": f["member"],
                "operator": f.get("operator") or "equals",
                "values": f["values"],
            })

    time_dims = []
    for t in (query.get("timeDimensions") or []):
        if isinstance(t, dict) and t.get("dimension") in valid_dims:
            entry = {"dimension": t["dimension"]}
            if t.get("granularity"):
                entry["granularity"] = t["granularity"]
            if t.get("dateRange"):
                entry["dateRange"] = t["dateRange"]
            time_dims.append(entry)

    clean: dict[str, Any] = {"measures": measures, "limit": min(int(query.get("limit") or 50), 500)}
    if dims:
        clean["dimensions"] = dims
    if filters:
        clean["filters"] = filters
    if time_dims:
        clean["timeDimensions"] = time_dims
    return clean, None


def _classify(db: Session, question: str, cubes: list[dict[str, Any]]) -> dict[str, Any]:
    """让 LLM 判断走哪条路，并给出 cube 查询。"""
    from app.services.llm_resolver import get_llm_client, get_model_name

    catalog_text = _format_catalog(cubes)
    client = get_llm_client(db, scene="data")
    model = get_model_name(db, scene="data")

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": f"指标目录：{catalog_text}\n\n问题：{question}"},
        ],
        temperature=0,
        max_tokens=800,
    )
    text = (resp.choices[0].message.content or "") if resp.choices else ""
    parsed = _parse_json(text)
    if not parsed:
        return {"route": "ad_hoc", "reason": "模型输出无法解析为 JSON", "raw": text[:300]}
    return parsed


def answer(
    db: Session,
    question: str,
    target_db: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """回答一个问题：先试受治理的指标层，再退到临时分析，全程标注来源。

    `target_db` 只影响临时分析路径（Cube 路径与具体库无关，它查的是已定义指标）。

    `force=True` 时跳过语义护栏的硬拦截（用于人工复核时查看原始结果）。
    """
    question = (question or "").strip()
    if not question:
        return {"ok": False, "reason": "empty_question", "message": "问题不能为空"}

    # 1) 拉目录
    try:
        cubes = CubeClient().list_cubes()
    except CubeError as e:
        return {
            "ok": False, "route": "cube", "governed": True,
            "reason": e.reason, "message": e.message,
        }

    catalog_by_cube = {c["name"]: c for c in cubes}

    # 2) 分类
    try:
        decision = _classify(db, question, cubes)
    except Exception as e:  # noqa: BLE001 - LLM 挂了就退到临时分析，不阻塞
        logger.warning("分类器调用失败: %s", e)
        decision = {"route": "ad_hoc", "reason": f"分类器不可用：{e}"}

    route = decision.get("route") if decision.get("route") in ("cube", "ad_hoc") else "ad_hoc"
    reason = decision.get("reason") or ""

    # 3) Cube 路径
    if route == "cube":
        query, bad = _validate_query(decision.get("query") or {}, catalog_by_cube)
        if query is None:
            # LLM 选的指标不存在 → 不硬查，直接退到临时分析并说明原因
            return _answer_adhoc(db, question, f"指标层无法承接（{bad}）", target_db, force)

        # 语义层护栏：**执行前**检查。这里能拦住已经证实的错数类型
        # （如对跨 143 天的快照表直接 SUM，实测膨胀 89 倍）。
        cube_name_for_guard = ""
        measures_for_guard = query.get("measures") or []
        if measures_for_guard and isinstance(measures_for_guard[0], str):
            cube_name_for_guard = measures_for_guard[0].split(".", 1)[0]

        verdict = _check_cube_guardrails(db, query, cube_name_for_guard)
        if verdict.blocking and not force:
            return _blocked_response(
                question, "cube", query, reason, verdict,
            )

        try:
            result = CubeClient().load(query)
        except CubeError as e:
            return {
                "ok": False, "route": "cube", "governed": True,
                "reason": e.reason, "message": e.message,
                "query": query, "decision": reason,
            }

        rows = result.get("data") or []

        # 语义层：把与这个问题相关的约束挂上去。
        # 指标口径能保证"数怎么算"，约束保证"别这么用"——两者缺一，
        # agent 仍然可能得到结构合法、业务错误的结论。
        cube_name = ""
        measures = query.get("measures") or []
        if measures and isinstance(measures[0], str):
            cube_name = measures[0].split(".", 1)[0]
        guardrails = _guardrails(
            db, question, [("cube", cube_name)] if cube_name else [],
        )

        return {
            "ok": True,
            "route": "cube",
            "governed": True,
            "source": "Cube（受治理口径）",
            "note": "结果来自已定义的指标口径，不同人问同一个问题会得到同一个数。",
            "decision": reason,
            "query": query,
            "rowCount": len(rows),
            "data": rows,
            "answer": _render_cube_answer(query, rows),
            "guardrails": guardrails,
        }

    # 4) 临时分析路径
    return _answer_adhoc(db, question, reason, target_db, force)


def _guardrails(
    db: Session, question: str, targets: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """取与当前问题相关的语义约束。失败不影响回答（护栏缺失不该让问数挂掉）。"""
    try:
        from app.services.semantics_service import relevant_constraints

        return relevant_constraints(db, question, targets=targets)
    except Exception as e:  # noqa: BLE001
        logger.warning("语义约束匹配失败: %s", e)
        return []


def _check_cube_guardrails(
    db: Session, query: dict[str, Any], cube_name: str,
):
    """执行前检查 Cube 查询是否违反硬约束。检查本身出错不阻断（返回空结果）。"""
    try:
        from app.services.guardrail_engine import check_cube_query

        return check_cube_query(db, query, cube_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("护栏检查失败: %s", e)

        class _Empty:
            verdicts: list = []

            @property
            def blocking(self) -> list:
                return []

        return _Empty()


def _blocked_response(
    question: str,
    route: str,
    query: dict[str, Any] | None,
    reason: str,
    verdict: Any,
) -> dict[str, Any]:
    """违反硬约束时的返回：不给错误数字，只给原因和修正方式。

    这是有意的取舍：宁可回答"这个问题我不能直接算"，
    也不返回一个看起来正常但实际膨胀了几十倍的数字。
    """
    first = verdict.blocking[0]
    return {
        "ok": False,
        "route": route,
        "governed": route == "cube",
        "source": "语义层护栏",
        "reason": "guardrail_blocked",
        "message": f"已拦截：{first.name}",
        "decision": reason,
        "query": query,
        "guardrails": [v.to_dict() for v in verdict.verdicts],
        "blocked": True,
        "bypassable": True,
        "hint": (
            "该查询违反了一条已知约束，直接执行会得到错误数字。"
            "请按提示修正口径后重问；确实要查看原始结果可加 force=true。"
        ),
    }


def _render_cube_answer(query: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """把 Cube 返回的行拼成一句话。

    只讲指标值，维度（期间、省份等）作为上下文单独提一句——
    否则答案里会混进 `month = 2026-07-01T00:00:00.000` 这种噪声。
    """
    if not rows:
        return "没有查到数据。"

    measures = [m for m in (query.get("measures") or []) if isinstance(m, str)]
    dims = [d for d in (query.get("dimensions") or []) if isinstance(d, str)]

    if len(rows) == 1:
        row = rows[0]
        parts = [
            f"{m.split('.')[-1]} = {row.get(m)}"
            for m in measures if m in row
        ]
        text = "；".join(parts) if parts else "（没有指标值）"
        if dims:
            ctx = "，".join(f"{d.split('.')[-1]}={row.get(d)}" for d in dims if d in row)
            if ctx:
                text += f"（{ctx}）"
        return text

    shown = "、".join(m.split('.')[-1] for m in measures) or "指标"
    return f"共 {len(rows)} 行，包含 {shown}（明细见下方表格）。"


def _answer_adhoc(
    db: Session, question: str, reason: str, target_db: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """走临时分析引擎。不可用时如实报告，不伪造答案。"""
    health = adhoc_client.health()
    if not health.get("ok"):
        return {
            "ok": False,
            "route": "ad_hoc",
            "governed": False,
            "source": "Auto-Ontology（临时分析）",
            "reason": "engine_unavailable",
            "message": health.get("message") or "临时分析引擎不可用",
            "decision": reason,
            "hint": (
                "临时分析引擎需要：① Auto-Ontology 服务在运行；"
                "② 配置 chat 模型与**embedding 模型**（DeepSeek 不提供 embeddings 接口）。"
                "受治理的问题可以走指标层。"
            ),
        }

    try:
        result = adhoc_client.ask(question, target_db=target_db)
    except AdHocUnavailable as e:
        return {
            "ok": False,
            "route": "ad_hoc",
            "governed": False,
            "source": "Auto-Ontology（临时分析）",
            "reason": e.reason,
            "message": e.message,
            "decision": reason,
            # 具体到"该怎么办"：补丁没打 / 容器没起 / target_db 没注册，
            # 三种失败的处置完全不同，只说"失败"没法排查。
            "hint": e.hint or (
                "临时分析引擎需要：① Auto-Ontology 服务在运行；"
                "② 配置 chat 模型与 embedding 模型；③ 指定已注册的 target_db。"
            ),
        }

    # 临时分析没有受治理口径，护栏更重要——它算的是裸 SQL。
    # 注意：SQL 是引擎生成的，只能在生成后复核；拦不住"生成"这一步，
    # 但可以拦住"把错误数字当成答案交出去"。
    sql = result.get("sql") or ""
    verdicts = _check_sql_guardrails(db, sql) if sql else None
    if verdicts is not None and verdicts.blocking and not force:
        blocked = _blocked_response(question, "ad_hoc", None, reason, verdicts)
        blocked["sql"] = sql
        blocked["note"] = (
            "临时分析引擎生成了这条 SQL，但它违反已知约束，"
            "结果会算错，因此不展示数字。"
        )
        return blocked

    guardrails = _guardrails(db, question, [])

    return {
        "ok": True,
        "route": "ad_hoc",
        "governed": False,
        "source": "Auto-Ontology（临时分析）",
        "note": "⚠️ 此结果由临时分析引擎生成，不属于任何已治理口径，仅供参考。",
        "decision": reason,
        "sql": sql,
        "answer": result.get("answer") or "（引擎未返回文字结论）",
        "rows": result.get("rows"),
        "stepCount": len(result.get("steps") or []),
        "guardrails": guardrails,
        "guardrailVerdicts": (
            [v.to_dict() for v in verdicts.verdicts] if verdicts is not None else []
        ),
    }


def _check_sql_guardrails(db: Session, sql: str):
    """检查临时分析生成的 SQL。检查本身出错不阻断。"""
    try:
        from app.services.guardrail_engine import check_sql

        return check_sql(db, sql)
    except Exception as e:  # noqa: BLE001
        logger.warning("SQL 护栏检查失败: %s", e)
        return None
