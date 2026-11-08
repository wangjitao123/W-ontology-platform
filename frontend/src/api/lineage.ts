import { get, post } from './client'

export type LineageNodeKind =
  | 'asset'
  | 'object_type'
  | 'action'
  | 'rule'
  // 字段级（detail=full 才有）
  | 'metric'
  | 'column'
  | 'attribute'

export interface LineageNode {
  kind: LineageNodeKind
  id: string
  label: string | null
  sub_label: string | null
  extra: Record<string, unknown> | null
}

export interface LineageEdge {
  source: { kind: LineageNodeKind; id: string }
  target: { kind: LineageNodeKind; id: string }
  relation: string
  via_module: string | null
  via_purpose: string | null
  weight: number
}

export interface LineageGraph {
  nodes: LineageNode[]
  edges: LineageEdge[]
}

export const lineageApi = {
  /** detail=coarse 只有 资产→对象→动作；full 追加字段级链路（指标→列→属性） */
  overview(detail: 'coarse' | 'full' = 'coarse') {
    return get<LineageGraph>('/lineage/overview?detail=' + detail)
  },
  rebuild(includeFields = true) {
    return post<Record<string, unknown>>(
      '/lineage/rebuild?include_fields=' + includeFields,
      {},
    )
  },
  forAsset(assetId: string, depth = 2) {
    return get<LineageGraph>(`/lineage?asset_id=${encodeURIComponent(assetId)}&depth=${depth}`)
  },
  forObjectType(objectTypeId: string, depth = 2) {
    return get<LineageGraph>(`/lineage?object_type_id=${encodeURIComponent(objectTypeId)}&depth=${depth}`)
  },
}
