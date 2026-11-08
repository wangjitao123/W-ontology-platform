"""对象索引（Object Index）—— 把对象实例物化到元数据库，避免每次回源查表。

为什么需要：
    元枢原本的对象浏览走 `SELECT * FROM 源表`，每次点击都要回源查询，
    并且受连接 rate_limit_qps 限制。对象一旦多、源库一旦慢，浏览就退化为
    "转圈"。

设计取舍：
    - **不改变对象的事实来源**：源表仍然是唯一真相，这里只是可再生的索引副本。
    - **全量快照而非增量**：优先保证正确性。单对象类型行数上限由同步任务控制，
      超出时截断并在 sync_state 里标记 truncated。
    - **props_json 存字符串化的属性**：与 /studio/abox 现有输出保持一致（都是 str），
      避免前端因为类型变化（Decimal/date）出错。

对齐 Foundry 的 OSv2 思路：对象由数据集派生、被物化成可查询的索引；
区别是本实现没有独立的对象存储引擎，就是一张普通索引表。
"""
from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, JSON, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.identifiers import gen_uuid


class ObjectIndexEntry(Base):
    """一个对象实例的索引行。"""

    __tablename__ = "object_index"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    object_type_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # 实例主键值（源表主键列的值，字符串化）
    object_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # 展示标题（通常是 name/title 列）
    title: Mapped[str] = mapped_column(String(500), default="")
    # 属性快照：{"customer_id": "C0001", "arpu": "188.50", ...}（全部字符串化）
    props_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    source_table: Mapped[str | None] = mapped_column(String(200))
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("object_type_id", "object_key", name="uq_object_index_key"),
        # 对象集合查询的常用入口：按类型 + 标题搜索
        Index("ix_object_index_type_title", "object_type_id", "title"),
    )


class ObjectIndexSyncState(Base):
    """每个对象类型最近一次同步的状态，供 /studio/index/stats 与前端展示。"""

    __tablename__ = "object_index_sync_state"

    object_type_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
        # pending | ok | empty | error | truncated
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    source_table: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
