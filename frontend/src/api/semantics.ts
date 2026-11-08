import { get } from './client'

export interface SemanticEvidence {
  id: string
  title: string
  source: string
  query: string
  result: string
  validationMethod: string
  observedAt?: string | null
}

export interface SemanticConstraint {
  id: string
  name: string
  targetKind: string
  targetId: string
  constraintType: string
  severity: 'block' | 'warn' | 'info'
  description: string
  remedy: string
  triggerKeywords: string[]
  scope: string
  autoSurface: boolean
  evidence?: SemanticEvidence[]
}

export interface SemanticsOverview {
  constraints: { total: number; bySeverity: Record<string, number>; byType: Record<string, number> }
  evidence: { total: number }
  terms: {
    entities: number
    withAliases: number
    attributes: number
    withGrain: number
    withAggregation: number
    withSourceField: number
  }
  relations: { total: number; bySemanticType: Record<string, number> }
}

export const semanticsApi = {
  overview: () => get<SemanticsOverview>('/semantics/overview'),
  constraints: (params?: { target_kind?: string; target_id?: string; severity?: string }) => {
    const q = new URLSearchParams()
    if (params?.target_kind) q.set('target_kind', params.target_kind)
    if (params?.target_id) q.set('target_id', params.target_id)
    if (params?.severity) q.set('severity', params.severity)
    const s = q.toString()
    return get<SemanticConstraint[]>('/semantics/constraints' + (s ? `?${s}` : ''))
  },
  evidence: () => get<SemanticEvidence[]>('/semantics/evidence'),
}
