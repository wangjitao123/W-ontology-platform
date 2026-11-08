"""把 cube 在查、但平台没登记的表补成资产。

## 为什么要补

「资产」是平台对一张物理表的登记，它决定三件事：
    1. ExecuteService 的执行作用域（SQL 只能查登记过的表）
    2. 血缘图的起点节点（没有资产，列就接不到"表"上，链路断头）
    3. 「数据资产」页面上的清单项

原来只给"对象类型绑定的表"建了资产（11 张），而 21 个 cube 实际用到 20 张
不同的 base 表——差的 10 张只被 cube 直接查，从来没登记过。

补完之后 `finance_summary.total_assets` 这类指标能一路追到
"表 → 连接"，不再在列这一层断掉。**注意：补资产不等于建对象**，这些表只是
变得可识别、可被治理通道查询，不会出现新的对象类型。

## 做法

schema 从 Doris 的 information_schema 现查，不硬编码——表搬家了脚本也不会错。
幂等：按 locator.table 去重，已存在的跳过。
"""
from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 21 个 cube 用到、但没有资产的 base 表
MISSING = [
    "ads_fund_financing_summary_1da",
    "ads_otd_dashboard_online_inbound",
    "ads_otd_dashboard_plan",
    "ads_otd_dimension_distribution",
    "ads_otd_dimension_distribution_fact_1da",
    "dwd_crm_delivery_vehicle_di",
    "dws_cdep_financial_indicator_month",
    "dws_crm_vehicle_mileage_range",
    "dws_sap_inv_aging_summary_1da",
    "dws_sap_inv_aging_top5_1da",
]

# 数仓层 → 平台里的连接名
LAYER_CONN = {
    "dwh_dwd": "doris-dwh",
    "dwh_dws": "doris-dws",
    "dwh_ads": "doris-ads",
}


def main() -> int:
    from app.database import SessionLocal
    from app.models.asset import Asset
    from app.models.connection import Connection
    from app.services.data_plane.connection_service import ConnectionService

    db = SessionLocal()
    try:
        conns = {c.name: c for c in db.query(Connection).all()}
        base = conns.get("doris-dwh")
        if base is None:
            print("  缺少连接 doris-dwh")
            return 1

        # 1) 从 Doris 查每张表在哪一层
        layers: dict[str, str] = {}
        svc = ConnectionService(db)
        with svc.with_conn(base.id) as raw:
            cur = raw.cursor()
            for t in MISSING:
                cur.execute(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = %s", (t,),
                )
                rows = cur.fetchall()
                if rows:
                    layers[t] = rows[0][0]
                else:
                    print(f"  ⚠️ Doris 里找不到 {t}，跳过")
            cur.close()

        # 2) 建资产
        existing = {
            ((a.locator or {}).get("table") or "").lower()
            for a in db.query(Asset).all()
        }
        created = skipped = 0
        for t in MISSING:
            if t.lower() in existing:
                skipped += 1
                continue
            layer = layers.get(t)
            if layer is None:
                continue
            conn_name = LAYER_CONN.get(layer)
            conn = conns.get(conn_name) if conn_name else None
            if conn is None:
                print(f"  ⚠️ {t} 在 {layer}，但没有对应连接 {conn_name}，跳过")
                continue

            db.add(Asset(
                name=t,
                alias=f"{t}_asset",
                kind="table",
                description=f"cube 的 base 表（{layer}）；由 seed_cube_assets 补登记",
                connection_id=conn.id,
                locator={"schema": layer, "table": t},
                domain="doris",
                tags=["doris", "cube_base"],
            ))
            created += 1
            print(f"  + {t:44} {layer}/{conn_name}")
        db.commit()
        print(f"  资产: 新建 {created}，已存在 {skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
