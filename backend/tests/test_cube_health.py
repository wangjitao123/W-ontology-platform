"""Cube 健康检查：每一个 cube 都必须能真的跑出一条数据。

## 为什么需要这个测试

改 cube 模型（YAML）时最常见的错误不是"写得不好看"，而是**写错了导致 SQL 直接
不成立**，而 Cube 在构建/启动时**不会报错** —— 它只在有人查询时才失败。

本轮就踩过一次：给 `fund_financing_maturity` 加日期过滤时，把 SQL 注释
（`-- ...`）写进了 YAML 的单引号字符串里。YAML 会把换行折叠成空格，
于是 `--` 把后面的 `AND ...` 和右括号全部注释掉，Cube 报
`missing ')' at '<EOF>'`。**这个 cube 完全不可用，而没人会去逐个点开验证。**

所以这里对 21 个 cube 各跑一次最小查询（第一个 measure，limit 1）：
    · 跑通 → 说明 base 表的列、SQL 语法、Jinja 引用全部成立
    · 失败 → 直接把 Cube 的报错原样抛出，定位到具体 cube

## 依赖

需要 Cube 在运行（:4000）。Cube 没起时整组跳过——不把外部服务不可用
当成代码缺陷，和 test_object_store 对 Postgres 的处理方式一致。

## 覆盖不到的部分

只验证"能跑"，不验证"数字对不对"。数字正确性由语义层的约束（sem_*）
和具体业务测试保证，这两件事必须分开测——否则一个测试失败时说不清是
SQL 坏了还是口径变了。
"""
from __future__ import annotations

import pytest

from app.services.metrics_cube import CubeClient, CubeError


def _cube_alive() -> bool:
    """Cube 进程是否在响应（只看存活，不看模型）。

    区分两件事很重要：
        · 进程没起       → skip（外部依赖不可用，不是代码问题）
        · 进程起了但模型坏 → fail（这是真正的缺陷，必须暴露）
    否则一个坏掉的 YAML 会让整套测试静默跳过，等于没有安全网。
    """
    import httpx

    from app.config import settings

    base = (settings.CUBE_API_URL or "").rstrip("/")
    if not base:
        return False
    root = base.split("/cubejs-api")[0]
    try:
        resp = httpx.get(f"{root}/readyz", timeout=3.0)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def client() -> CubeClient:
    if not _cube_alive():
        pytest.skip("Cube 进程未运行（外部依赖不可用）")
    return CubeClient()


@pytest.fixture(scope="module")
def cube_list(client) -> list[dict]:
    """取 cube 目录。

    Cube 活着但这里抛错，说明**模型编译失败**（YAML 写坏、SQL 引用了不存在的列等），
    这是必须失败的缺陷，不能 skip。
    """
    try:
        cubes = client.list_cubes()
    except CubeError as e:
        pytest.fail(f"Cube 在运行但模型目录读取失败（{e.reason}）：{e.message}")
    if not cubes:
        pytest.fail("Cube 返回空目录——模型未加载")
    return cubes


def test_catalog_is_not_empty(cube_list):
    """目录为空通常意味着模型加载失败，比单个 cube 报错更严重。"""
    assert len(cube_list) >= 20, f"cube 数量异常：{len(cube_list)}"


def test_every_cube_has_measures(cube_list):
    empty = [c["name"] for c in cube_list if not (c.get("measures") or [])]
    assert not empty, f"这些 cube 没有度量（无法查询）：{empty}"


@pytest.mark.parametrize(
    "cube_name",
    [
        # 库龄（本轮加过最新快照日过滤）
        "aging_summary", "wms_detail", "b2b_detail", "vehicle_detail", "aging_top5",
        # 资金（本轮加过最新快照日过滤）
        "fund_bank_balance", "fund_financing", "fund_financing_maturity",
        # 财务 / 应收
        "finance_summary", "ar_open_items",
        # OTD（事件/计划口径，本轮未改）
        "otd_plan", "otd_daily", "otd_distribution_plan",
        "otd_distribution_daily", "otd_schedule_detail",
        # 销售事实
        "contract_vehicle", "deposit", "delivery", "mileage_range",
        "abroad_contract", "abroad_delivery",
    ],
)
def test_cube_is_queryable(client, cube_name):
    """每个 cube 至少能跑通一条最小查询。

    失败信息里带上 Cube 的原始报错（通常是 SQL 语法或列不存在），
    因为那才是需要修的东西。
    """
    try:
        cubes = {c["name"]: c for c in client.list_cubes()}
    except CubeError as e:
        pytest.skip(f"Cube 不可用：{e.reason}")
    if cube_name not in cubes:
        pytest.fail(f"目录里没有 cube {cube_name}（模型文件改坏或未加载）")

    measures = [m["name"] for m in cubes[cube_name].get("measures") or [] if m.get("name")]
    assert measures, f"{cube_name} 没有度量"

    try:
        result = client.load({"measures": [measures[0]], "limit": 1})
    except CubeError as e:
        pytest.fail(f"{cube_name} 查询失败：{e.reason}: {e.message}")
    assert "data" in result, f"{cube_name} 返回结构异常：{list(result)}"


def test_all_cubes_queryable_in_one_pass(client, cube_list):
    """汇总校验：任何 cube 跑不通都要列出来，而不是只报第一个。

    单独的参数化用例负责定位，这条负责给一份完整清单——
    改完一批 cube 后一眼看到"还剩几个坏的"。
    """
    try:
        client.list_cubes()
    except CubeError as e:
        pytest.skip(f"Cube 不可用：{e.reason}")

    broken: list[str] = []
    for c in cube_list:
        ms = [m["name"] for m in c.get("measures") or [] if m.get("name")]
        if not ms:
            broken.append(f"{c['name']}(无度量)")
            continue
        try:
            client.load({"measures": [ms[0]], "limit": 1})
        except Exception as e:  # noqa: BLE001
            broken.append(f"{c['name']}({str(e)[:80]})")
    assert not broken, f"以下 cube 查询失败：{broken}"
