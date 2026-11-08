"""端到端验证版本演进：改动能表达 → 能算影响 → 能生效 → 能回滚。

## 它验证什么

之前的版本系统有四块是断的，这个脚本逐条证明它们现在通了：

    1. 草稿能编辑     旧版没有任何编辑接口，"改一个属性"无法表达
    2. 能算出改了什么  旧版 diff 只认"实体删除/改名"，属性变更完全检测不到
    3. 能算出影响谁    旧版只查场景/智能体两张表，且智能体表已清空 → 恒为空
    4. 审批真的生效    旧版 approve 只改状态，实际对象表一个字节没动
    5. 能回滚          旧版 rollback 只多一条记录，实际数据不回退

## 做法

    v1(当前生效)
      → 建 v2 草稿，把 ContractVehicle.actual_amount 的类型改成 string
      → diff / impact / apply-preview 三项检查
      → 提交 + 审批 → 断言实际属性类型真的变成 string
      → 回滚到 v1（生成 v3，待审批）
      → 审批 v3 → 断言实际属性类型退回 number

## 为什么挑 actual_amount

因为"大额合同需人工复核"这条规则的条件就是
`{"field": "ContractVehicle.actual_amount", ...}`，
改它的类型是**破坏性变更**，影响面里必须能看见这条规则——
这正好验证"改了物料的属性会影响谁"这个核心问题。

脚本结束时会话会停在"v_rollback 生效"的状态，内容与 v1 一致。
"""
from __future__ import annotations

import io
import sys

import httpx

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8001/api/v1/ontology-publish"
ENTITY_BASE = "http://127.0.0.1:8001/api/v1/entities"

TARGET_ENTITY = "ContractVehicle"
TARGET_ATTR = "actual_amount"
BROKEN_TYPE = "string"          # 故意改成错误类型，制造破坏性变更
# 原始类型不写死：实测是 decimal，不是文档里常见的 number。
# 写死会让脚本在第一步就误报失败，掩盖真正要验证的东西。
ORIGINAL_TYPE = ""

ok = 0
fail = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}" + (f"  ← {detail}" if detail else ""))


def main() -> int:
    c = httpx.Client(timeout=120.0)

    # ── 0) 前置状态 ──
    print("=== 0) 前置状态 ===")
    active = c.get(f"{BASE}/versions/active").json()
    check(bool(active), "存在生效版本", str(active)[:80])
    if not active:
        return 1
    v1_id = active["id"]
    print(f"    生效版本 v{active.get('version_number')} {active.get('name')}")

    # 找到目标实体在 v1 里的版本实体 id 与属性 id
    ents = c.get(ENTITY_BASE, params={"page_size": 200}).json()
    ents = ents.get("items") if isinstance(ents, dict) else ents
    ent = next((e for e in (ents or []) if e.get("id") == TARGET_ENTITY), None)
    check(ent is not None, f"找到实际实体 {TARGET_ENTITY}")
    if ent is None:
        return 1

    # 属性在详情接口里（列表接口不带 attributes）
    detail_live = c.get(f"{ENTITY_BASE}/{TARGET_ENTITY}").json()
    live_attr = next((a for a in (detail_live.get("attributes") or [])
                      if a.get("name") == TARGET_ATTR), None)
    check(live_attr is not None, f"找到属性 {TARGET_ATTR}")
    if live_attr is None:
        return 1
    global ORIGINAL_TYPE
    ORIGINAL_TYPE = str(live_attr.get("type"))
    print(f"    当前类型: {ORIGINAL_TYPE}")

    # ── 1) 建草稿 ──
    print("=== 1) 建 v2 草稿并冻结全部实体 ===")
    r = c.post(f"{BASE}/versions", json={
        "name": "验证：把合同金额类型改错",
        "description": "端到端验证版本演进（改动能表达/能算影响/能生效/能回滚）",
    })
    check(r.status_code in (200, 201), "草稿创建成功", r.text[:150])
    v2 = r.json()
    v2_id = v2["id"]
    print(f"    v{v2.get('version_number')}  id={v2_id}")

    all_ids = [e["id"] for e in (ents or []) if e.get("id")]
    r = c.post(f"{BASE}/versions/{v2_id}/entities", json={"entity_ids": all_ids})
    check(r.status_code in (200, 201), f"冻结 {len(all_ids)} 个实体")

    # ── 2) 编辑草稿：把属性类型改错 ──
    print("=== 2) 编辑草稿（这一步旧版根本没有）===")
    detail = c.get(f"{BASE}/versions/{v2_id}").json()
    ve = next((x for x in (detail.get("entities") or [])
               if x.get("source_entity_id") == TARGET_ENTITY
               or x.get("sourceEntityId") == TARGET_ENTITY), None)
    check(ve is not None, f"v2 引用了 {TARGET_ENTITY}", str(list(detail))[:100])
    if ve is None:
        return 1
    ve_id = ve.get("id")
    va = next((a for a in (ve.get("attributes") or [])
               if a.get("name") == TARGET_ATTR), None)
    check(va is not None, f"v2 里有 {TARGET_ATTR}")
    if va is None:
        return 1
    va_id = va.get("id")

    r = c.put(f"{BASE}/versions/{v2_id}/entities/{ve_id}/attributes/{va_id}",
              json={"type": BROKEN_TYPE, "description": "故意改错，用于验证回滚"})
    check(r.status_code in (200, 201), f"属性类型改为 {BROKEN_TYPE}", r.text[:150])
    if r.status_code not in (200, 201):
        return 1

    # ── 3) diff：能不能算出"改了什么" ──
    print("=== 3) diff：算出改了什么 ===")
    d = c.get(f"{BASE}/versions/{v2_id}/diff").json()
    changes = d.get("changes") or []
    check(bool(changes), f"检出变更 {len(changes)} 条", str(d.get("summary")))
    type_change = next((x for x in changes
                        if x.get("scope") == "attribute"
                        and x.get("target") == TARGET_ATTR
                        and x.get("field") == "type"), None)
    check(type_change is not None,
          f"检出了 {TARGET_ATTR} 的类型变更",
          str([f"{x.get('target')}.{x.get('field')}" for x in changes])[:150])
    if type_change:
        print(f"    {type_change.get('label')}")
        check(type_change.get("breaking") is True, "标记为破坏性变更")

    # ── 4) impact：能不能算出影响谁 ──
    print("=== 4) impact：算出影响谁 ===")
    imp = c.get(f"{BASE}/versions/{v2_id}/impact").json()
    deps = imp.get("dependents") or {}
    for key, label in (
        ("rules", "规则"),
        ("metricBindings", "指标绑定"),
        ("actions", "动作"),
        ("functions", "函数"),
        ("lineage", "血缘"),
    ):
        n = len(deps.get(key) or [])
        check(n > 0, f"影响面包含{label}（{n} 个）", f"{key} 为空")
    rule_hit = next((x for x in (deps.get("rules") or [])
                     if TARGET_ATTR in str(x.get("reason"))), None)
    check(rule_hit is not None,
          f"规则影响精确到 {TARGET_ATTR}",
          str([x.get("reason") for x in (deps.get("rules") or [])])[:150])
    if rule_hit:
        print(f"    {rule_hit['name']}：{rule_hit['reason']}")

    # ── 5) apply-preview：审批前看会改什么 ──
    print("=== 5) apply-preview：审批前预览 ===")
    pv = c.get(f"{BASE}/versions/{v2_id}/apply-preview").json()
    will = pv.get("willUpdate") or {}
    check((will.get("attributes") or 0) > 0, f"预览到会改 {will.get('attributes')} 个属性")

    # ── 6) 提交 + 审批：验证真的生效 ──
    print("=== 6) 提交 + 审批（验证审批会真的改实际对象）===")
    r = c.post(f"{BASE}/versions/{v2_id}/submit")
    check(r.status_code in (200, 201), "提交审批", r.text[:150])
    r = c.post(f"{BASE}/versions/{v2_id}/approve", json={})
    check(r.status_code in (200, 201), "审批通过", r.text[:200])
    applied = (r.json() or {}).get("applied") or {}
    check((applied.get("attributesUpdated") or 0) > 0,
          f"实际对象被更新（attributesUpdated={applied.get('attributesUpdated')}）",
          str(applied)[:150])

    # 直接查实际对象，确认类型真的变了（详情接口才带 attributes）
    d2 = c.get(f"{ENTITY_BASE}/{TARGET_ENTITY}").json()
    attr2 = next((a for a in (d2.get("attributes") or [])
                  if a.get("name") == TARGET_ATTR), None)
    check((attr2 or {}).get("type") == BROKEN_TYPE,
          f"实际属性类型确实变成 {BROKEN_TYPE}",
          f"实际是 {(attr2 or {}).get('type')}")

    # ── 7) 回滚到 v1 ──
    print("=== 7) 回滚到 v1 ===")
    r = c.post(f"{BASE}/versions/{v1_id}/rollback")
    check(r.status_code in (200, 201), "创建回滚版本", r.text[:200])
    rb = r.json()
    v3_id = rb.get("id") or rb.get("version_id")
    check(bool(v3_id), "拿到回滚版本 id", str(rb)[:150])
    if not v3_id:
        return 1
    print(f"    回滚版本 v{rb.get('version_number')}  id={v3_id}")

    # 回滚版本应当是待审批，且内容等于 v1
    rb_list = c.get(f"{BASE}/versions").json()
    rb_list = rb_list.get("items") if isinstance(rb_list, dict) else rb_list
    rb_row = next((x for x in (rb_list or []) if x.get("id") == v3_id), None)
    check((rb_row or {}).get("status") == "pending_approval",
          "回滚版本处于待审批", str(rb_row)[:150])

    # ── 8) 审批回滚版本 → 实际数据退回 ──
    print("=== 8) 审批回滚版本（验证真的退回上一版）===")
    r = c.post(f"{BASE}/versions/{v3_id}/approve", json={})
    check(r.status_code in (200, 201), "回滚版本审批通过", r.text[:200])
    rb_applied = (r.json() or {}).get("applied") or {}
    print(f"    applied: 实体更新 {rb_applied.get('entitiesUpdated')}, "
          f"属性更新 {rb_applied.get('attributesUpdated')}")

    d3 = c.get(f"{ENTITY_BASE}/{TARGET_ENTITY}").json()
    attr3 = next((a for a in (d3.get("attributes") or [])
                  if a.get("name") == TARGET_ATTR), None)
    check((attr3 or {}).get("type") == ORIGINAL_TYPE,
          f"实际属性类型已退回 {ORIGINAL_TYPE}",
          f"实际是 {(attr3 or {}).get('type')}")

    active2 = c.get(f"{BASE}/versions/active").json()
    check((active2 or {}).get("id") == v3_id,
          "回滚版本成为生效版本",
          f"生效的是 {(active2 or {}).get('id')}")

    # ── 9) 回滚版本与 v1 的内容应当一致 ──
    print("=== 9) 回滚后应与 v1 内容一致 ===")
    diff_rb = c.get(f"{BASE}/versions/{v3_id}/diff", params={"against": v1_id}).json()
    check((diff_rb or {}).get("breakingCount") == 0,
          "回滚版本相对 v1 无破坏性差异",
          str((diff_rb or {}).get("changes"))[:200])

    c.close()
    print()
    print(f"=== 结果：{ok} 项通过，{fail} 项失败 ===")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
