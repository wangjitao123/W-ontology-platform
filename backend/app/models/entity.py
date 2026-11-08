from datetime import datetime

from sqlalchemy import String, Integer, Boolean, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.identifiers import gen_uuid


class OntologyEntity(Base):
    __tablename__ = "ontology_entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name_cn: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    description: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by: Mapped[str | None] = mapped_column(String(36))
    publish_config: Mapped[dict | None] = mapped_column(JSON)
    # ── 语义层字段 ──
    # 别名/同义词。同一业务对象在不同系统里叫法不同（"合同车辆" / "合同行"），
    # 没有别名就做不了语义对齐，agent 也无法把用户口语对上概念。
    aliases: Mapped[list | None] = mapped_column(JSON, default=list)
    # 权威程度：high = 经数据验证；medium = 推断；low = 待确认
    confidence: Mapped[str] = mapped_column(String(10), default="medium")

    attributes: Mapped[list["EntityAttribute"]] = relationship(back_populates="entity", cascade="all, delete-orphan")
    rules: Mapped[list["BusinessRule"]] = relationship(back_populates="entity", cascade="all, delete-orphan")
    actions: Mapped[list["EntityAction"]] = relationship(back_populates="entity", cascade="all, delete-orphan")
    functions: Mapped[list["OntologyFunction"]] = relationship(back_populates="entity", cascade="all, delete-orphan")


class EntityAttribute(Base):
    __tablename__ = "entity_attributes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    entity_id: Mapped[str] = mapped_column(ForeignKey("ontology_entities.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    example: Mapped[str | None] = mapped_column(String(200))
    constraints_json: Mapped[dict | None] = mapped_column(JSON)
    source_table: Mapped[str | None] = mapped_column(String(200))
    source_field: Mapped[str | None] = mapped_column(String(200))
    data_status: Mapped[str] = mapped_column(String(20), default="未确认来源")

    # ── 语义层字段（映射的"怎么算"语义，光有表.列不够）──
    # 粒度：这一列是哪个粒度上的值（如"合同×产品组合"、"物料×工厂×快照日"）。
    # 粒度说错会导致跨维度重复累加。
    grain: Mapped[str | None] = mapped_column(String(300))
    # 聚合语义：能不能 sum / 必须去重后再加 / 只能取最新。
    # 这是本轮踩过的坑：库龄表跨天直接 SUM 会重复计算同一天的数据。
    aggregation_semantics: Mapped[str | None] = mapped_column(String(300))
    # 映射的隐含过滤：取这一列时必须附带的口径条件（如 sign_state=4）。
    semantic_filter: Mapped[str | None] = mapped_column(String(500))
    # 置信度：high / medium / low
    confidence: Mapped[str] = mapped_column(String(10), default="medium")

    entity: Mapped["OntologyEntity"] = relationship(back_populates="attributes")
