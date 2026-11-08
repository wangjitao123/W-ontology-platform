/** 本体自动发现 —— 从 Doris 表结构推对象类型候选 */
import { get, post } from './client'

export interface DiscoveryAttribute {
  name: string
  type: string
  role?: 'key' | 'label' | 'category' | 'status' | 'datetime' | 'measure' | 'other'
  isKey?: boolean
  sourceTable?: string | null
  sourceField?: string | null
}

export interface DiscoveryProposal {
  id: string
  kind: 'entity' | 'snapshot' | 'report' | 'relation'
  name: string
  nameCn: string | null
  nameCnNeedsReview?: boolean
  reason: string
  confidence: 'high' | 'medium' | 'low'
  attributes: DiscoveryAttribute[]
  sources: string[]
  keyField: string | null
  // 实体额外字段
  family?: string
  tableCount?: number
  score?: number
  identityRisk?: boolean
  existing?: boolean
  existingId?: string
  missingAttributes?: string[]
  similarTo?: string[]
  // 快照额外字段
  layer?: string
  dateColumn?: string
  measureCount?: number
  // 关系额外字段
  from?: string
  to?: string
  relType?: string
  cooccurrence?: number
}

export interface DiscoveryScanResult {
  entities: DiscoveryProposal[]
  snapshots: DiscoveryProposal[]
  relations: DiscoveryProposal[]
  stats: {
    tables: number
    columns: number
    entityCandidates: number
    snapshotCandidates: number
    relationCandidates: number
  }
  scannedAt: string
}

export interface AdoptResult {
  ok: boolean
  versionId: string
  versionNumber: number
  copiedFromActive?: number
  created: { name: string; nameCn: string | null; attributeCount: number; identityRisk: boolean }[]
  relations: { name: string; cooccurrence: number }[]
  skipped: { name: string; reason: string }[]
  unknown: string[]
  next?: string
}

export const discoveryApi = {
  scan: (refresh = false) =>
    get<DiscoveryScanResult>(`/discovery/scan?refresh=${refresh ? 'true' : 'false'}`),
  adopt: (proposalIds: string[], versionName?: string) =>
    post<AdoptResult>('/discovery/adopt', {
      proposal_ids: proposalIds,
      version_name: versionName || null,
    }),
}
