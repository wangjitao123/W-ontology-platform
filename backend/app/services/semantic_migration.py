"""语义层字段的向前兼容迁移。

`Base.metadata.create_all()` 会建新表（sem_constraints / sem_evidence），
但**不会给已存在的表加列**。所以语义层新增的这几列要显式 ALTER。

SQLite 的 ALTER TABLE ADD COLUMN 不支持加带 NOT NULL 和无默认值的列，
这里一律加 nullable 或带常量默认值，并在应用层给默认值（模型里的 default=
只在 Python 侧生效，对老行不管用）。

幂等：先读 PRAGMA 看列是否存在，存在就跳过。
"""
from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# (表, 列, 列定义)
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # 概念：别名 / 置信度
    ("ontology_entities", "aliases", "JSON"),
    ("ontology_entities", "confidence", "VARCHAR(10) DEFAULT 'medium'"),
    ("ontology_version_entities", "aliases", "JSON"),
    # 映射：粒度 / 聚合语义 / 隐含过滤 / 置信度
    ("entity_attributes", "grain", "VARCHAR(300)"),
    ("entity_attributes", "aggregation_semantics", "VARCHAR(300)"),
    ("entity_attributes", "semantic_filter", "VARCHAR(500)"),
    ("entity_attributes", "confidence", "VARCHAR(10) DEFAULT 'medium'"),
    ("ontology_version_attributes", "grain", "VARCHAR(300)"),
    ("ontology_version_attributes", "aggregation_semantics", "VARCHAR(300)"),
    # 关系：语义类型 / 置信度
    ("entity_relations", "semantic_type", "VARCHAR(20) DEFAULT 'association'"),
    ("entity_relations", "confidence", "VARCHAR(10) DEFAULT 'high'"),
    ("ontology_version_relations", "semantic_type", "VARCHAR(20) DEFAULT 'association'"),
    # 约束的自动检测规则 + 是否硬拦截
    ("sem_constraints", "detector_json", "JSON"),
    ("sem_constraints", "enforce", "BOOLEAN DEFAULT 0"),
)


def ensure_semantic_columns(engine) -> dict[str, int]:
    """给已存在的表补语义层列。返回 {表名: 新增列数}。"""
    added: dict[str, int] = {}
    with engine.connect() as conn:
        for table, column, ddl in _COLUMNS:
            try:
                existing = {
                    row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
                }
            except Exception:  # noqa: BLE001
                continue          # 表不存在（旧库），跳过
            if not existing:
                continue
            if column in existing:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added[table] = added.get(table, 0) + 1
                logger.info("语义层迁移: %s 增加列 %s", table, column)
            except Exception as e:  # noqa: BLE001
                logger.warning("语义层迁移: %s.%s 失败: %s", table, column, e)
        conn.commit()
    return added
