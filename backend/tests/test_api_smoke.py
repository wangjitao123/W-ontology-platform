"""API 冒烟测试：所有 GET 接口都不许 5xx。

## 为什么需要它

本轮遇到一类隐蔽 bug：**代码路径从来没被执行过，因为对应的表是空的。**

`entity_actions` 长期 0 行，于是
    lineage_service.py:  sub_label = act.type
    studio.py:           "actionType": a.type
从未执行。等我把 3 个动作填进去，两个接口立刻 AttributeError → 500：
`/api/v1/lineage/overview`（血缘整页打不开）和 `/api/v1/studio/capability`。

单元测试跑不到这些路径（它们依赖"库里有数据"这个前提），
所以需要一层"把每个接口都真的打一遍"的冒烟测试兜底。

## 判定

· 2xx / 3xx / 401 / 403 / 404 / 422   → 正常（鉴权、缺参、资源不存在都是合法响应）
· 5xx                                 → 失败，必须修

## 白名单

`mnp` / `scenes/broadband` 这些**演示场景**依赖独立业务库
（`bb_audit_db`、`mnp.user_count` 资产），本机没接这些数据源，
接口如实返回 503"业务连接未就绪"。这与代码正确性无关，列进白名单，
但白名单只允许 503，出现 500 仍然失败。

`ai-builder` 依赖 MinIO 对象存储（地址见 MINIO_ENDPOINT），本机也没部署。
修过一处：boto3 默认 connect/read 各 60s 且重试 4 次，MinIO 不可达时
`/documents` 会挂几分钟并拖死线程池；现在压到 2s、不重试，5 秒内返回 503。
"""
from __future__ import annotations

import pytest

BASE = "http://127.0.0.1:8001"

# 依赖外部服务、当前未部署：只允许"依赖未就绪"的 503
DEMO_PREFIXES = (
    "/api/v1/mnp/",
    "/api/v1/scenes/broadband",
    "/api/v1/ai-builder",
)

# 需要请求体的 GET（OpenAPI 里声明了 required query/body），跳过
SKIP_PATHS = {"/api/v1/ai-builder/documents/content", "/api/v1/ai-builder/tables"}

# 流式接口：返回 200 后**不关闭连接**（SSE 长连接 / 文件下载）。
# 冒烟测试是"打一下就断开"，打这类接口会一直等下去——实测卡了 25 分钟，
# 最后靠查 access 日志才定位到是 /api/v1/events/sse。
STREAM_MARKERS = ("/sse", "/stream", "/download", "/export", "/tail")

ALLOWED = {200, 201, 204, 301, 302, 400, 401, 403, 404, 405, 422}


@pytest.fixture(scope="module")
def spec() -> dict:
    import httpx

    try:
        resp = httpx.get(f"{BASE}/openapi.json", timeout=15.0)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"后端未运行（{e}）")
    if resp.status_code != 200:
        pytest.skip(f"OpenAPI 不可用：HTTP {resp.status_code}")
    return resp.json()


def _get_paths(spec: dict) -> list[str]:
    out = []
    for path, methods in (spec.get("paths") or {}).items():
        if "get" not in methods:
            continue
        if any(m in path for m in STREAM_MARKERS):
            continue
        # 路径参数统一填 1（不存在的资源会返回 404，这是合法响应）
        concrete = path
        while "{" in concrete:
            start = concrete.index("{")
            end = concrete.index("}", start)
            concrete = concrete[:start] + "1" + concrete[end + 1:]
        if concrete in SKIP_PATHS:
            continue
        out.append(concrete)
    return sorted(set(out))


def test_openapi_has_endpoints(spec):
    paths = _get_paths(spec)
    assert len(paths) > 50, f"GET 接口只有 {len(paths)} 个，路由注册可能出问题了"


def test_no_get_endpoint_returns_5xx(spec):
    """所有 GET 接口逐个打一遍，任何 5xx 都算失败。

    失败信息里列出全部出问题的路径——改完一批再跑一遍，
    一眼看到还剩几个没修好。
    """
    import httpx

    client = httpx.Client(timeout=60.0)
    failures: list[str] = []
    checked = 0

    try:
        for path in _get_paths(spec):
            try:
                resp = client.get(f"{BASE}{path}")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{path} → 请求异常 {type(e).__name__}: {e}")
                continue
            checked += 1
            code = resp.status_code

            if code in ALLOWED:
                continue
            if code == 503 and any(path.startswith(p) for p in DEMO_PREFIXES):
                continue
            detail = resp.text[:160].replace("\n", " ")
            failures.append(f"{path} → HTTP {code} {detail}")
    finally:
        client.close()

    assert checked > 50, "实际只测到很少的接口，OpenAPI 解析可能有问题"
    assert not failures, (
        f"{len(failures)} 个 GET 接口返回 5xx：\n  " + "\n  ".join(failures)
    )


def test_lineage_endpoint_works():
    """血缘是踩过坑的接口：动作表有数据后它 500 过。

    单独一条，让失败信息直接指向"血缘坏了"，而不是埋在几十个路径里。
    """
    import httpx

    try:
        resp = httpx.get(f"{BASE}/api/v1/lineage/overview", timeout=120.0)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"后端未运行（{e}）")
    assert resp.status_code == 200, f"血缘接口 HTTP {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "nodes" in body and "edges" in body
    assert isinstance(body["nodes"], list)


def test_studio_capability_works():
    """同一类 bug：capability 接口用了 EntityAction.type。"""
    import httpx

    try:
        resp = httpx.get(f"{BASE}/api/v1/studio/capability", timeout=60.0)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"后端未运行（{e}）")
    assert resp.status_code == 200, f"capability HTTP {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    # 动作填进去之后，能力清单里必须能看见它们
    text = str(body)
    assert "actionType" in text or "ACTION" in text, "能力清单里没有动作信息"
