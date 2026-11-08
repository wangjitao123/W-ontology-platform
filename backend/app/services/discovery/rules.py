"""推导规则：从表结构生成"对象类型 / 属性 / 关系"候选。

## 设计原则

**不猜，只说得出理由。** 每条候选都带 `reason` 和 `confidence`，
人看的时候能判断"它为什么说这是一个对象类型"。

**宁缺勿滥。** 165 张表能推出几百个"候选"，全列出来没人看。
所以规则做三层过滤：

    1. 家族必须带 key 列（*_code / *_id / *_no）——没有主键的东西不是对象
    2. 家族必须还有描述列（*_name）或者跨多张表出现——否则只是个外键
    3. 技术噪声家族直接拉黑（create / update / stat / record / etl ...）

## 为什么用"列名前缀家族"而不是别的方法

这个数仓**没有维度表**（全是 dwd/dws/ads 的明细和汇总）。
真正意义上的实体是通过事实表里的 key 列体现的：

    dwd_sap_inv_wms_aging_detail_1da 里的 material_code + material_name
    → 物料是一个独立存在的对象

把 `material_code` / `material_name` 按前缀 `material` 归成一家，
就能把散落在多张表里的同一个实体拼接起来。这也是原手工 14 个类型的来源。

## 中文名

内置词典覆盖常见数仓词汇；命中不了就**如实标注需要人工命名**，
而不是用英文冒充中文。名称是给人看的，假的中文名比没有更糟。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.services.discovery.profiler import ColumnMeta, TableMeta

logger = logging.getLogger(__name__)

# 不是实体、只是技术字段前缀的家族
_NOISE_FAMILIES = {
    "create", "update", "delete", "stat", "record", "data", "etl", "dw",
    "is", "has", "batch", "load", "sync", "source", "target", "date", "time",
    "month", "day", "year", "start", "end", "begin", "finish", "first", "last",
    "current", "prev", "next", "old", "new", "parent", "child", "ref",
    # 下面这些是踩出来的：它们长着 *_id 的样子，但描述的是技术对象而不是业务实体
    "approve", "process", "instance", "flow", "file", "agreement", "ship",
    "pay", "method", "version", "number", "remark", "note", "comment",
    "ext", "raw", "tmp", "temp", "bak", "log", "seq", "uuid", "guid",
    "task", "job", "rule", "config", "param", "option", "flag", "status",
    # 首词挂载后会冒出这些：deleted_by_name / sign_person_name / sales_name
    "deleted", "sign", "sales", "battery", "person", "user", "creator",
    "updater", "owner", "operator", "modifier", "employee", "staff",
}

# 明确是业务实体的家族：命中这些直接给 high（人工复核过的白名单，
# 来自原手工 14 个对象类型 + 常见数仓实体）
_KNOWN_ENTITIES = {
    "customer", "material", "supplier", "plant", "warehouse", "factory",
    "contract", "vehicle", "order", "product", "portfolio", "chance",
    "bank", "currency", "fund", "invoice", "payment", "delivery",
    "deposit", "dealer", "channel", "project", "model", "series", "brand",
    "engine", "loan", "account", "industry", "province", "city", "region",
    "brand", "battery", "defect", "fault", "claim", "bill", "transport",
}

# 常见数仓词汇 → 中文（命中不了就标 needs_review）
_NAME_CN = {
    "material": "物料", "plant": "工厂", "warehouse": "仓库", "customer": "客户",
    "supplier": "供应商", "contract": "合同", "vehicle": "车辆", "order": "订单",
    "product": "产品", "portfolio": "产品组合", "chance": "销售机会",
    "principal": "负责人", "unit": "单位", "org": "组织", "organization": "组织",
    "factory": "基地", "bank": "银行", "currency": "币种", "fund": "资金",
    "finance": "财务", "invoice": "发票", "payment": "付款", "delivery": "交付",
    "abroad": "海外", "deposit": "定金", "aging": "库龄", "warehouse": "仓库",
    "model": "车型", "series": "车系", "brand": "品牌", "engine": "发动机",
    "type": "类型", "channel": "渠道", "dealer": "经销商", "region": "区域",
    "province": "省份", "city": "城市", "industry": "行业", "project": "项目",
    "task": "任务", "repair": "维修", "quality": "质量", "problem": "问题",
    "loan": "贷款", "balance": "余额", "rate": "汇率", "account": "账户",
    "mileage": "里程", "otd": "OTD", "production": "生产", "schedule": "排产",
}

# 一个家族至少要有这些列角色之一，才算"有描述信息"
_DESCRIPTIVE_ROLES = {"label", "category", "status"}


@dataclass
class Proposal:
    """一条候选。id 是确定性的，方便前端回传后再定位。"""

    id: str
    kind: str                    # entity / snapshot / relation
    name: str
    name_cn: str | None
    reason: str
    confidence: str              # high / medium / low
    attributes: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    key_field: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def identity_risk(self) -> bool:
        """没有编码列、只能按名称识别（extra 里放，免得字段越加越多）。"""
        return bool(self.extra.get("identityRisk"))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "nameCn": self.name_cn,
            "nameCnNeedsReview": self.name_cn is None,
            "reason": self.reason,
            "confidence": self.confidence,
            "attributes": self.attributes,
            "sources": self.sources,
            "keyField": self.key_field,
        }
        d.update(self.extra)
        return d


def _pascal(snake: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[_\-\s]+", snake) if p)


def _family_columns(
    tables: list[TableMeta], family: str,
) -> dict[str, tuple[ColumnMeta, str]]:
    """把某家族在各表里出现的列合并（按列名**小写**去重）。

    去重要按小写：Doris 里存在 `SUPPLIER_NAME` 和 `supplier_name` 同时出现的情况，
    按原始名去重会让同一个字段在属性列表里出现两次。

    同时返回"这一列首次出现在哪张表"。属性的 `source_table` 必须是真实存在的
    那张表——随便挑一张既通不过一致性检查（要求 ≥50% 属性有 source_table +
    source_field），映射本身也是假的。
    """
    merged: dict[str, tuple[ColumnMeta, str]] = {}
    for t in tables:
        for c in t.columns:
            if c.family == family and c.role not in ("ignore", "measure"):
                merged.setdefault(c.name.lower(), (c, t.fq_name))
    return merged


def _family_sources(tables: list[TableMeta], family: str) -> list[TableMeta]:
    return [t for t in tables if any(c.family == family for c in t.columns)]


# ── 实体候选 ──────────────────────────────────────────────

def discover_entities(tables: list[TableMeta], limit: int = 40) -> list[Proposal]:
    """从列名前缀家族推导实体对象类型。

    过滤分四层，每一层都是为了把 107 个候选压到能看的人数：

      1. 拉黑技术噪声家族（approve / process / file / pay ...）
      2. 必须有主键列（*_code / *_id / *_no）
      3. 必须有描述列（*_name / *_type / *_status）——只有外键的不算对象
      4. 要么是已知业务实体，要么跨 ≥3 张表 / 有 ≥3 个属性，否则太薄不算
    """
    families: dict[str, list[TableMeta]] = defaultdict(list)
    for t in tables:
        for fam in t.families():
            families[fam].append(t)

    scored: list[tuple[int, Proposal]] = []
    for fam, srcs in families.items():
        # 噪声按首词匹配：approve_process_instance / file_upload_id 这类
        # 变体必须也挡掉，否则会冒出 ApproveProcessInstance 这种候选。
        head = fam.split("_")[0]
        if len(fam) < 3 or fam in _NOISE_FAMILIES or head in _NOISE_FAMILIES:
            continue

        cols = _family_columns(tables, fam)
        if not cols:
            continue
        roles = {c.role for c, _ in cols.values()}
        key_cols = [c for c, _ in cols.values() if c.role == "key"]
        # 必须有描述信息：光有 customer_code 只是外键
        if not (roles & _DESCRIPTIVE_ROLES):
            continue
        known = fam in _KNOWN_ENTITIES
        if not known and len(srcs) < 3 and len(cols) < 3:
            continue

        # 没有主键列的情况照样要提出来——但必须**明确标注身份风险**。
        # 典型例子：customer 家族有 customer_name / customer_desc / customer_type，
        # 却没有 customer_code。这种实体只能按名称识别，正好撞上语义层里
        # 那条 identity_by_name 约束（不同实体重名会被算成一个）。
        # 静默丢掉它才是错的：人需要知道"你的客户主数据缺编码"。
        # 没有编码列的候选要更严格：至少 3 个属性或者属于已知业务实体。
        # 否则 sign_person_name / deleted_by_name 这种"人"字段会被当成实体。
        identity_risk = not key_cols
        if identity_risk and (len(srcs) < 3 or (len(cols) < 3 and not known)):
            continue

        key = (
            sorted(key_cols, key=lambda c: ("_code" not in c.name, c.name))[0]
            if key_cols else None
        )
        cn = _NAME_CN.get(fam)

        attrs = []
        for _lname, (c, src_table) in sorted(
            cols.items(), key=lambda kv: (kv[1][0].role != "key", kv[0])
        ):
            attrs.append({
                "name": c.name,
                "type": _attr_type(c.dtype),
                "role": c.role,
                "isKey": c.role == "key",
                # 真实来源表 + 真实列名，属性可追溯到具体表的列
                "sourceTable": src_table,
                "sourceField": c.name,
            })

        reasons = [f"列名族 `{fam}_*` 出现在 {len(srcs)} 张表里"]
        if key is not None:
            reasons.append(f"有主键列 {key.name}")
        else:
            reasons.append(
                "**没有编码/ID 列，只能按名称识别**——与「不能用名称当身份」"
                "的约束冲突，采纳前需要补一个稳定编码"
            )
        if roles & _DESCRIPTIVE_ROLES:
            reasons.append(f"有描述列（{', '.join(sorted(roles & _DESCRIPTIVE_ROLES))}）")
        if cn is None:
            reasons.append("中文名未识别，需人工命名")

        if identity_risk:
            confidence = "low"
        elif known and cn:
            confidence = "high"
        elif len(srcs) >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # 排序分：已知实体优先，其次跨表多的、属性多的
        score = (100 if known else 0) + len(srcs) * 2 + len(attrs) - (20 if identity_risk else 0)

        scored.append((score, Proposal(
            id=f"ent::{fam}",
            kind="entity",
            name=_pascal(fam),
            name_cn=cn,
            reason="；".join(reasons),
            confidence=confidence,
            attributes=attrs,
            sources=[t.fq_name for t in srcs[:8]],
            key_field=key.name if key else None,
            extra={
                "family": fam,
                "tableCount": len(srcs),
                "score": score,
                "identityRisk": identity_risk,
            },
        )))

    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]


def _attr_type(dtype: str) -> str:
    d = (dtype or "").lower()
    if any(x in d for x in ("int", "bigint", "smallint", "tinyint")):
        return "integer"
    if any(x in d for x in ("decimal", "double", "float")):
        return "decimal"
    if "date" in d or "time" in d:
        return "datetime"
    if "bool" in d:
        return "boolean"
    return "string"


# ── 快照 / 汇总候选 ───────────────────────────────────────

def discover_snapshots(tables: list[TableMeta], limit: int = 25) -> list[Proposal]:
    """把"按日期存的汇总表"推成 snapshot / report 类型。

    只挑形态够像的：有日期列 + 有度量 + 维度家族少（≤3）。
    不满足的直接跳过——不是每张表都值得成为一个对象类型。
    """
    cands: list[tuple[int, Proposal]] = []
    for t in tables:
        date_col = t.date_column()
        measures = t.columns_of("measure")
        dim_fams = {c.family for c in t.columns
                    if c.family and c.role in ("key", "label", "category", "status")}
        if not date_col or not measures:
            continue
        if len(dim_fams) > 3:
            continue  # 维度太多 → 明细表，不是汇总

        kind = "report" if t.layer == "ads" else "snapshot"
        name = _pascal(t.name)
        for pref in ("Dwd", "Dws", "Ads"):
            if name.startswith(pref):
                name = name[len(pref):]
                break
        name = re.sub(r"(_?\d+[DdAa]?[Aa]?)$", "", name) or name

        attrs = []
        for c in t.columns:
            if c.role == "ignore":
                continue
            attrs.append({
                "name": c.name,
                "type": _attr_type(c.dtype),
                "role": c.role,
                "isKey": False,
                "sourceTable": t.fq_name,
                "sourceField": c.name,
            })

        score = len(measures) + len(dim_fams) * 2
        cands.append((score, Proposal(
            id=f"snap::{t.schema}::{t.name}",
            kind=kind,
            name=name,
            name_cn=None,
            reason=(
                f"{t.fq_name} 有日期列 {date_col} + {len(measures)} 个度量 + "
                f"{len(dim_fams)} 个维度族（{', '.join(sorted(dim_fams)) or '无'}）"
            ),
            confidence="medium",
            attributes=attrs,
            sources=[t.fq_name],
            key_field=None,
            extra={"layer": t.layer, "dateColumn": date_col, "measureCount": len(measures)},
        )))

    cands.sort(key=lambda x: -x[0])
    return [p for _, p in cands[:limit]]


# ── 关系候选 ──────────────────────────────────────────────

def discover_relations(
    entities: list[Proposal], tables: list[TableMeta],
    *, min_cooccurrence: int = 3, limit: int = 40,
) -> list[Proposal]:
    """两个实体族在同一张表里反复共现 → 它们之间存在关联。

    保守规则：只在**三张以上**表里共现才提。两表共现很可能是巧合
    （比如同一张宽表里既有 contract_id 又有 vehicle_id），
    三张以上才有统计意义。上限 40 条，避免又变成 458 条的噪声墙。
    """
    fam_of = {e.id: e.extra.get("family") for e in entities}
    name_of = {e.id: e.name for e in entities}
    known_fams = set(fam_of.values())

    cooccur: dict[tuple[str, str], list[str]] = defaultdict(list)
    for t in tables:
        fams = sorted({f for f in t.families() if f in known_fams})
        for i in range(len(fams)):
            for j in range(i + 1, len(fams)):
                cooccur[(fams[i], fams[j])].append(t.fq_name)

    rev = {v: k for k, v in fam_of.items()}
    out: list[Proposal] = []
    for (a, b), srcs in sorted(cooccur.items(), key=lambda kv: -len(kv[1])):
        if len(srcs) < min_cooccurrence:
            continue
        ea, eb = rev.get(a), rev.get(b)
        if not ea or not eb:
            continue
        out.append(Proposal(
            id=f"rel::{a}::{b}",
            kind="relation",
            name=f"{name_of[ea]}-{name_of[eb]}",
            name_cn=None,
            reason=f"`{a}_*` 与 `{b}_*` 在 {len(srcs)} 张表里同时出现："
                   f"{', '.join(srcs[:3])}",
            confidence="high" if len(srcs) >= 5 else "medium",
            attributes=[],
            sources=srcs[:6],
            key_field=None,
            extra={"from": ea, "to": eb, "relType": "association",
                   "cooccurrence": len(srcs)},
        ))
        if len(out) >= limit:
            break
    return out


# ── 汇总 ──────────────────────────────────────────────────

def build_proposals(
    tables: list[TableMeta], *, include_snapshots: bool = True,
) -> dict[str, Any]:
    entities = discover_entities(tables)
    snaps = discover_snapshots(tables) if include_snapshots else []
    rels = discover_relations(entities, tables)
    return {
        "entities": [p.to_dict() for p in entities],
        "snapshots": [p.to_dict() for p in snaps],
        "relations": [p.to_dict() for p in rels],
        "stats": {
            "tables": len(tables),
            "columns": sum(len(t.columns) for t in tables),
            "entityCandidates": len(entities),
            "snapshotCandidates": len(snaps),
            "relationCandidates": len(rels),
        },
    }
