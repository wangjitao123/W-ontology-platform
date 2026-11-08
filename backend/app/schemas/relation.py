from pydantic import BaseModel, Field


class JoinKey(BaseModel):
    """一条关系在实例层的物理连接依据（列对）。

    `*_transform` 用来表达非等值直连的关系，例如"银行资金余额[日] 归属
    财务月[月]"需要把日期截成月份再比：

        {"from_column": "data_date", "to_column": "period",
         "from_transform": "month"}

    可选值：month / upper / lower / trim（白名单，见 object_store._TRANSFORMS）。
    """
    from_column: str
    to_column: str
    from_transform: str | None = None
    to_transform: str | None = None


class RelationCreate(BaseModel):
    from_entity_id: str
    to_entity_id: str
    name: str
    rel_type: str = "has_many"
    cardinality: str = "1:N"
    description: str | None = None
    join_keys: list[JoinKey] = Field(default_factory=list)


class RelationOut(BaseModel):
    id: str
    from_entity_id: str
    from_entity_name: str
    to_entity_id: str
    to_entity_name: str
    name: str
    rel_type: str
    cardinality: str
    description: str | None
    join_keys: list[JoinKey] = Field(default_factory=list)
