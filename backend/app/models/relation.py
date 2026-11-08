from datetime import datetime

from sqlalchemy import String, Integer, Boolean, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.identifiers import gen_uuid


class EntityRelation(Base):
    __tablename__ = "entity_relations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    from_entity_id: Mapped[str] = mapped_column(ForeignKey("ontology_entities.id", ondelete="CASCADE"))
    to_entity_id: Mapped[str] = mapped_column(ForeignKey("ontology_entities.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(10), nullable=False)
    acyclic: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 实例层连线依据：物理列对，形如
    #   [{"from_column": "order_no", "to_column": "order_no"}]
    # 列表形式天然支持复合键；为空表示该关系不可在实例层遍历。
    join_keys: Mapped[list | None] = mapped_column(JSON, default=list)

    # ── 语义层字段 ──
    # 关系的语义类型，区分"关联"和"包含"这类本质差异：
    #   association 关联（合同 ↔ 交货单）
    #   hierarchy   层级（大区 → 工厂）
    #   composition 组成（物料 → 它的库龄记录，子项依附于父项）
    #   equivalence 等价（同一实体的两种口径）
    #   derivation  派生（由父项计算而来）
    # 这个分类决定了能不能沿边聚合：composition 的子项求和是有意义的，
    # equivalence 的两端求和就是重复计算。
    semantic_type: Mapped[str] = mapped_column(String(20), default="association")
    # 置信度：high / medium / low
    confidence: Mapped[str] = mapped_column(String(10), default="high")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
