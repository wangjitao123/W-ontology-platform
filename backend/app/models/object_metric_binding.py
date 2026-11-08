"""对象 ↔ 指标 绑定。

解决的问题：
    Cube 里的指标是"全公司口径"（如"山东省合同数 574"），但用户常常想问
    "**这个对象的**指标"（这个合同的台数、这个月的资产总额）。

    元枢有对象身份，Cube 有指标口径，这张表把两者接起来：
    用对象的某个属性值，去过滤某个 cube 的对应维度。

示例：
    object_type_id = FinanceMonth, key_column = "period",
    cube_name = "finance_summary", filter_member = "finance_summary.period"
    → 查 period=2026-07 的月报对象时，自动带 filters=[{period == "2026-07"}]

为什么不做成"元枢自己定义指标"：
    那会造出第二套口径，和 Cube 必然漂移。这里只存"怎么过滤"，口径仍在 Cube。
"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.identifiers import gen_uuid


class ObjectMetricBinding(Base):
    __tablename__ = "object_metric_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    object_type_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 展示名，如"本月财务指标" / "本省合同"
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    cube_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 用对象的哪个属性值去过滤
    key_column: Mapped[str] = mapped_column(String(120), nullable=False)
    # cube 里的维度全名；为空则默认 f"{cube_name}.{key_column}"
    filter_member: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 展示哪些指标（cube 内的 measure 名，不含 cube 前缀）
    metric_names: Mapped[list | None] = mapped_column(JSON, default=list)
    # 附加固定过滤（Cube filters 语法）
    extra_filters: Mapped[list | None] = mapped_column(JSON, default=list)

    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def resolved_filter_member(self) -> str:
        return self.filter_member or f"{self.cube_name}.{self.key_column}"
