"""实例级连线 —— 把 EntityRelation 定义的 join 键算成真实的边。

## 概念分层（这部分以前是缺的）

```
类型层   EntityRelation:  AbroadContract ──has──▶ AbroadDelivery
                          join_keys = [{from_column: contract_id,
                                        to_column:   contract_id}]
                                    │
                                    │  物化（本模块）
                                    ▼
实例层   object_link:     DWHV20260033AE ──▶ 交货单 A
                          DWHV20260033AE ──▶ 交货单 B
```

旧实现只有类型层，实例层靠 twin 视图里 **O(n×m) 的内存双层循环**临时算，
`WmsAging`(1.3 万) × `B2bAging`(2.9 万) = 3.7 亿次比较，跑不动，所以实际上
一条边都画不出来。

现在把 join 下推到 Postgres 做哈希连接，结果落到 `object_link` 表，
查询"这个对象连了谁"就是两个索引查。

## join_keys 的扩展

```json
[{"from_column": "data_date", "to_column": "period", "from_transform": "month"}]
```

`*_transform` 走白名单（month / upper / lower / trim），用来表达
"按月份归属"这类非等值直连的关系。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import OntologyEntity
from app.models.relation import EntityRelation
from app.services.object_store import get_object_store

logger = logging.getLogger(__name__)


def _join_pairs(rel: EntityRelation) -> list[dict[str, str]]:
    return [
        jk for jk in (rel.join_keys or [])
        if isinstance(jk, dict) and jk.get("from_column") and jk.get("to_column")
    ]


def rebuild_relation(db: Session, rel: EntityRelation) -> int:
    """重建一条关系的实例连线，返回该关系当前的边数。"""
    store = get_object_store()
    if store is None:
        return 0
    pairs = _join_pairs(rel)
    if not pairs:
        logger.info("连线: %s 没有 join_keys，跳过", rel.name)
        return 0

    count = store.rebuild_links(rel.id, rel.from_entity_id, rel.to_entity_id, pairs)
    logger.info(
        "连线: %s (%s → %s) 物化 %d 条", rel.name, rel.from_entity_id,
        rel.to_entity_id, count,
    )
    return count


def rebuild_all(db: Session) -> dict[str, Any]:
    """全量重建所有关系。对象索引同步之后调用。"""
    store = get_object_store()
    if store is None:
        return {
            "ok": False,
            "reason": "object_store_disabled",
            "message": "未配置 OBJECT_STORE_URL，连线无法物化",
            "relations": [],
        }

    rels = db.query(EntityRelation).all()
    rows: list[dict[str, Any]] = []
    for rel in rels:
        try:
            n = rebuild_relation(db, rel)
            rows.append({
                "rel": rel.name, "from": rel.from_entity_id,
                "to": rel.to_entity_id, "links": n, "status": "ok",
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("连线: %s 重建失败: %s", rel.name, e)
            rows.append({
                "rel": rel.name, "from": rel.from_entity_id,
                "to": rel.to_entity_id, "links": 0, "status": "error",
                "error": str(e)[:300],
            })

    return {
        "ok": all(r["status"] == "ok" for r in rows),
        "relations": rows,
        "totalLinks": sum(r["links"] for r in rows),
        "totalRelations": len(rows),
    }


def definitions(db: Session) -> list[dict[str, Any]]:
    """关系定义 + 当前边数，供前端/运维看。"""
    store = get_object_store()
    counts = store.link_counts_by_rel() if store is not None else {}
    entities = {e.id: e for e in db.query(OntologyEntity).all()}

    out = []
    for rel in db.query(EntityRelation).all():
        src = entities.get(rel.from_entity_id)
        tgt = entities.get(rel.to_entity_id)
        out.append({
            "id": rel.id,
            "name": rel.name,
            # 短名给前端当标签用；描述单独给，别拿一句话当图例
            "displayName": rel.name,
            "description": rel.description or "",
            "fromType": rel.from_entity_id,
            "fromDisplay": src.name_cn if src else None,
            "toType": rel.to_entity_id,
            "toDisplay": tgt.name_cn if tgt else None,
            "cardinality": rel.cardinality,
            "relType": rel.rel_type,
            "joinKeys": _join_pairs(rel),
            "linkCount": counts.get(rel.id, 0),
        })
    return out


def neighbors(
    db: Session, object_type_id: str, object_key: str, *, limit: int = 200,
) -> dict[str, Any]:
    """某个实例的双向邻居，按关系分组。

    返回里每条关系带上对方类型的显示名和关系名，前端不用再查一遍字典。
    """
    store = get_object_store()
    if store is None:
        return {
            "objectType": object_type_id, "objectKey": object_key,
            "groups": [], "total": 0,
            "note": "对象存储未启用，连线不可用",
        }

    raw = store.links_of_object(object_type_id, object_key, limit=limit)
    meta = {r["id"]: r for r in definitions(db)}
    entities = {e.id: e for e in db.query(OntologyEntity).all()}
    # 邻居标题一次取回，避免前端拿键再查一遍
    titles = store.objects_by_keys([(i["peerType"], i["peerKey"]) for i in raw])

    groups: dict[str, dict[str, Any]] = {}
    for item in raw:
        rel = meta.get(item["relId"]) or {}
        g = groups.setdefault(item["relId"], {
            "relId": item["relId"],
            "relName": rel.get("name") or item["relId"],
            "displayName": rel.get("displayName") or item["relId"],
            "direction": item["direction"],
            "peerType": item["peerType"],
            "peerTypeDisplay": (
                entities[item["peerType"]].name_cn
                if item["peerType"] in entities else item["peerType"]
            ),
            "neighbors": [],
        })
        info = titles.get((item["peerType"], item["peerKey"])) or {}
        g["neighbors"].append({
            "objectType": item["peerType"],
            "objectKey": item["peerKey"],
            "title": info.get("title") or item["peerKey"],
        })

    return {
        "objectType": object_type_id,
        "objectKey": object_key,
        "groups": list(groups.values()),
        "total": len(raw),
    }
