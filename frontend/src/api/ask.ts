/**
 * 问数 API —— 方案 B 的入口
 *
 * 后端会自己决定走 Cube（受治理）还是 Auto-Ontology（临时分析），
 * 前端唯一的责任是：**把 governed 标出来**，让用户知道手里的数来自哪一套。
 */
import { get, post } from './client'

export interface AskCubeQuery {
  measures: string[]
  dimensions?: string[]
  filters?: Array<{ member: string; operator: string; values: Array<string | number> }>
  timeDimensions?: Array<{ dimension: string; granularity?: string; dateRange?: unknown }>
  limit?: number
}

export interface AskGuardrail {
  id: string
  name: string
  severity: 'block' | 'warn' | 'info'
  constraintType: string
  targetKind: string
  targetId: string
  description: string
  remedy: string
  matchedKeywords: string[]
  matchedBy: 'keyword' | 'target'
  /** 自动检测的结果（护栏引擎产出） */
  violated?: boolean
  confidence?: 'high' | 'low'
  enforced?: boolean
  reason?: string
  detector?: string
}

export interface AskResult {
  ok: boolean
  route?: 'cube' | 'ad_hoc'
  governed?: boolean
  source?: string
  note?: string
  decision?: string
  answer?: string
  reason?: string
  message?: string
  hint?: string
  /** cube 路径 */
  query?: AskCubeQuery
  data?: Array<Record<string, unknown>>
  rowCount?: number
  /** ad_hoc 路径 */
  sql?: string
  eventCount?: number
  stepCount?: number
  rows?: unknown
  /**
   * 语义层护栏：与这个问题相关的业务约束。
   * 有值不代表这次算错了，而是提醒"这个口径有已知的坑"。
   */
  guardrails?: AskGuardrail[]
  /** 是否被护栏拦截（拦截时不返回数字，只给原因） */
  blocked?: boolean
  /** 拦截是否可绕过（force=true） */
  bypassable?: boolean
}

export interface AskHealth {
  cube: { ok: boolean; cubeCount?: number; measureCount?: number; reason?: string; message?: string }
  adHoc: { ok: boolean; baseUrl?: string; reason?: string; message?: string }
  note?: string
}

export interface AskDatabase {
  database: string
  type: string
}

export interface AskDatabasesResult {
  databases: AskDatabase[]
  default: string | null
  note?: string | null
}

export const askApi = {
  ask: (question: string, targetDb?: string | null, force?: boolean) =>
    post<AskResult>('/ask', { question, target_db: targetDb || null, force: !!force }),
  health: () => get<AskHealth>('/ask/health'),
  databases: () => get<AskDatabasesResult>('/ask/databases'),
}
