// 本体侧血缘：Asset → ObjectType → Action

export type LineageNodeKind =
  | 'asset'
  | 'object_type'
  | 'action'
  | 'rule'
  // 字段级（detail=full 才有）
  | 'metric'
  | 'column'
  | 'attribute'
export type LineageRelation =
  | 'binds_to'
  | 'reads'
  | 'writes'
  | 'derives_from'
  | 'uses'
  | 'belongs_to'
  | 'maps_to'
  | 'bound_to'

export interface LineageNode {
  kind: LineageNodeKind
  id: string
}

export interface LineageEdgeOut {
  source: LineageNode
  target: LineageNode
  relation: LineageRelation
  via_module?: string | null
  via_purpose?: string | null
  weight: number
}

export interface LineageGraph {
  nodes: LineageNode[]
  edges: LineageEdgeOut[]
}
