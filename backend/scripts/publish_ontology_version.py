"""通过真实的发布流程创建并生效一个本体版本。

## 为什么不用 ORM 直接插

`ontology_versions` 不是一张孤立表：发布时会冻结一份实体/属性/关系的快照到
`ontology_version_entities` / `_attributes` / `_relations`，还要走
草稿 → 提交 → 审批 → 生效 的状态机。直接 INSERT 一行版本记录只能得到一个
"看起来像版本"的空壳，正是本轮要消灭的东西。

所以这里调用 `/api/v1/ontology-publish/*`，和前端按钮走同一条路径。

## 流程

    POST /versions               建草稿
    POST /versions/{id}/entities 把当前 14 个对象类型的快照冻结进去
    POST /versions/{id}/submit   提交审批
    POST /versions/{id}/approve  审批通过（会置 is_active）

## 幂等

已经有已发布版本时直接跳过，不重复建。
"""
from __future__ import annotations

import io
import sys

import httpx

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8001/api/v1/ontology-publish"


def main() -> int:
    c = httpx.Client(timeout=60.0)

    # 1) 已经有生效版本就不动了
    try:
        active = c.get(f"{BASE}/versions/active")
        if active.status_code == 200 and active.json():
            v = active.json()
            print(f"  已有生效版本 v{v.get('version_number') or v.get('versionNumber')}（{v.get('name')}），跳过")
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 查询生效版本失败: {e}")

    # 2) 取出当前对象类型
    r = c.get("http://127.0.0.1:8001/api/v1/entities", params={"page_size": 200})
    if r.status_code != 200:
        print(f"  ✗ 取实体列表失败 HTTP {r.status_code}: {r.text[:150]}")
        return 1
    payload = r.json()
    entities = payload.get("items") if isinstance(payload, dict) else payload
    entity_ids = [e["id"] for e in (entities or []) if e.get("id")]
    if not entity_ids:
        print("  ✗ 没有可发布的对象类型")
        return 1
    print(f"  待发布对象类型 {len(entity_ids)} 个")

    # 3) 建草稿
    r = c.post(f"{BASE}/versions", json={
        "name": "v1.0 基线：对象层 + 语义层",
        "description": (
            "首个可发布基线。包含 14 个对象类型（8 实体 / 6 快照）、"
            "7 条关系、语义层的 10 条约束与 11 条证据，"
            "以及本轮补充的 3 个函数 / 3 个动作 / 3 条规则。"
        ),
    })
    if r.status_code not in (200, 201):
        print(f"  ✗ 建版本失败 HTTP {r.status_code}: {r.text[:200]}")
        return 1
    ver = r.json()
    vid = ver.get("id")
    print(f"  草稿已建: v{ver.get('version_number') or ver.get('versionNumber')} id={vid}")

    # 4) 冻结实体快照
    r = c.post(f"{BASE}/versions/{vid}/entities", json={"entity_ids": entity_ids})
    if r.status_code not in (200, 201):
        print(f"  ✗ 加实体失败 HTTP {r.status_code}: {r.text[:200]}")
        return 1
    print(f"  已冻结 {len(entity_ids)} 个实体快照")

    # 5) 提交审批
    r = c.post(f"{BASE}/versions/{vid}/submit")
    if r.status_code not in (200, 201):
        print(f"  ⚠️ 提交失败 HTTP {r.status_code}: {r.text[:150]}")
    else:
        print(f"  已提交审批: status={r.json().get('status')}")

    # 6) 审批通过
    r = c.post(f"{BASE}/versions/{vid}/approve", json={})
    if r.status_code not in (200, 201):
        print(f"  ⚠️ 审批失败 HTTP {r.status_code}: {r.text[:200]}")
        return 1
    body = r.json()
    print(f"  审批通过: status={body.get('status')} isActive={body.get('is_active')}")

    # 7) 验证
    r = c.get(f"{BASE}/versions/active")
    if r.status_code == 200 and r.json():
        v = r.json()
        print(f"  ✓ 生效版本: v{v.get('version_number') or v.get('versionNumber')} {v.get('name')} "
              f"实体 {v.get('entity_count') or v.get('entityCount')} 个 状态 {v.get('status')}")
    r = c.get(f"{BASE}/status")
    if r.status_code == 200:
        s = r.json()
        if isinstance(s, dict):
            print(f"  发布状态: {s}")
        else:
            print(f"  发布状态: {len(s)} 条记录")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
