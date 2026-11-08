"""语义层：约束（Constraint）与证据（Evidence）。

## 为什么单独两张表

元枢原来有"对象类型 / 属性 / 关系"这些**结构**，但没有两样东西：

1. **约束** —— "这个指标不能这么算"的护栏。结构描述的是"有什么"，
   约束描述的是"不能怎么用"。缺了约束，agent 能算出结构上合法、业务上错误的数。
   （本轮就踩过：库龄表只有单一快照日，跨日期 SUM 会把同一天重复累加。）

2. **证据** —— "我凭什么认为这条映射是对的"。没有证据，语义就是拍脑袋；
   agent 无法判断该不该信，人也无法复核。

这张设计借鉴了 EvoOntology 的 Constraint / Evidence 记录模型，
但存进元枢自己的库（与对象、绑定、血缘同一个事务、同一份备份），
不用它的文件式 workspace —— 否则会出现"同一概念两个权威"。

## 字段说明

约束：
    target_kind   约束作用在什么上：entity / attribute / relation / metric / cube
    target_id     对应 target_kind 的 id（metric 用 "cube.member"）
    constraint_type  分类：grain(粒度) / aggregation(聚合) / scope(口径) /
                      identity(身份) / unit(单位) / quality(质量)
    severity      block = 违反会得到错误结论，必须拦；
                  warn  = 容易错，提醒；
                  info  = 背景说明
    trigger_keywords  问题里出现这些词时这条约束相关（供问数时挂提示）

证据：
    source        证据从哪来：doris_query / cube / execution_log / data_profile
    query         实际跑的查询（或执行目的）
    result        结论（如"共 12,958 行，仅 1 个日期"）
    validation_method  怎么验的：count / distinct / cross_check / sample
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.identifiers import gen_uuid


class SemanticConstraint(Base):
    """业务约束：定义"这个口径不能怎么用"。"""

    __tablename__ = "sem_constraints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # 作用对象：entity / attribute / relation / metric / cube
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="entity")
    target_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    constraint_type: Mapped[str] = mapped_column(String(30), nullable=False, default="scope")
    # block / warn / info —— block 表示违反会得出错误结论
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="warn")

    description: Mapped[str] = mapped_column(Text, default="")
    # 违反时人应该怎么做（可执行建议，而不是一句"不许"）
    remedy: Mapped[str] = mapped_column(Text, default="")

    trigger_keywords: Mapped[list | None] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list | None] = mapped_column(JSON, default=list)

    scope: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    # 是否可用于自动挂到问数结果上
    auto_surface: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── 自动检测规则 ──
    # 把"怎么算才算违反"写成可执行的规则，查询生成后逐条检查。
    # 没有这一步，约束只是给人看的说明文档，拦不住任何错数。
    # 形如 {"type": "snapshot_aggregate_without_date",
    #       "tables": [...], "date_column": "record_date"}
    detector_json: Mapped[dict | None] = mapped_column(JSON)
    # 检测到违反时是否真的拦截。block + enforce=True 才会拒绝执行。
    enforce: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SemanticEvidence(Base):
    """证据：一条语义结论的依据（跑了什么查询、得到什么结果）。"""

    __tablename__ = "sem_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    # doris_query / cube / execution_log / data_profile
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="doris_query")
    # 实际执行的 SQL 或执行目的
    query: Mapped[str] = mapped_column(Text, default="")
    # 结论摘要（给人看的）
    result: Mapped[str] = mapped_column(Text, default="")
    # count / distinct / cross_check / sample / schema
    validation_method: Mapped[str] = mapped_column(String(40), default="count")

    # 关联到执行日志（可选）：同一结论可以由多次执行支撑
    execution_log_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
