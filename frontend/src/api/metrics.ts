/**
 * 指标层 API —— 对接 Cube（元枢只做转发，口径始终在 Cube 的模型里）
 *
 * 设计约定：
 *   - 目录接口返回的是"能查什么"，不含数据
 *   - 查询接口遇错会抛 AxiosError，后端把 detail.reason 放在 error.response.data.detail
 *     页面用 describeMetricError() 统一翻译成人话
 */
import { get, post } from './client'

export interface MeasureInfo {
  name: string
  title: string
  type: string
  description: string
}

export interface DimensionInfo {
  name: string
  title: string
  type: string
  description: string
}

export interface CubeInfo {
  name: string
  title: string
  description: string
  measures: MeasureInfo[]
  dimensions: DimensionInfo[]
}

export interface MetricsCatalog {
  cubeCount: number
  measureCount: number
  dimensionCount: number
  cubes: CubeInfo[]
}

export interface MetricTimeDimension {
  dimension: string
  granularity?: string
  dateRange?: string | string[]
}

export interface MetricFilter {
  member: string
  operator: string
  values: Array<string | number>
}

export interface MetricQuery {
  measures: string[]
  dimensions?: string[]
  timeDimensions?: MetricTimeDimension[]
  filters?: MetricFilter[]
  limit?: number
  order?: Record<string, 'asc' | 'desc'>
}

export interface MetricQueryResult {
  data: Array<Record<string, unknown>>
  annotation: Record<string, unknown>
  rowCount: number
}

export interface MetricsHealth {
  ok: boolean
  cubeCount?: number
  measureCount?: number
  dimensionCount?: number
  reason?: string
  message?: string
}

export interface ObjectMetricError {
  reason: string
  message: string
}

export interface ObjectMetricItem {
  label: string
  cube: string
  keyColumn: string
  filterMember: string
  filterValue: string | null
  data: Array<Record<string, unknown>>
  error: ObjectMetricError | null
}

export interface ObjectMetricsResult {
  objectType: string
  objectTypeDisplay?: string
  objectKey: string
  metrics: ObjectMetricItem[]
  note?: string
}

/** 后端错误 → 人话。指标层最常见的两类失败是 VPN 断和口径过滤为空。 */
export function describeMetricError(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    const d = detail as { reason?: string; message?: string }
    if (d.reason === 'query_failed') {
      return `${d.message ?? '查询失败'}（通常是 Cube 连不上 Doris：检查 VPN）`
    }
    if (d.reason === 'unreachable') {
      return `${d.message ?? '连不上 Cube'}（Cube 服务是否在 4000 端口运行？）`
    }
    if (d.message) return d.message
  }
  if (typeof detail === 'string') return detail
  return (err as Error)?.message ?? '未知错误'
}

export interface ObjectMetricBindingInfo {
  objectTypeId: string
  objectTypeDisplay: string
  label: string
  cube: string
  keyColumn: string
  filterMember: string
  metrics: string[]
}

export const metricsApi = {
  health: () => get<MetricsHealth>('/metrics/health'),

  catalog: () => get<MetricsCatalog>('/metrics/catalog'),

  cube: (name: string) => get<CubeInfo>(`/metrics/catalog/${encodeURIComponent(name)}`),

  query: (query: MetricQuery) => post<MetricQueryResult>('/metrics/query', { query }),

  /** 哪些对象类型配了指标绑定（前端用来打标 + 默认选中） */
  objectBindings: () => get<ObjectMetricBindingInfo[]>('/metrics/object-bindings'),

  /** 对象实例自己的指标（按 object_metric_bindings 配置过滤） */
  objectMetrics: (objectType: string, objectKey: string) =>
    get<ObjectMetricsResult>(
      `/object-sets/${encodeURIComponent(objectType)}/${encodeURIComponent(objectKey)}/metrics`,
    ),

  /** 对象集合查询（对象指标页用来找实例） */
  queryObjects: (
    objectType: string,
    body: { search?: string; page?: number; page_size?: number; prefer_key?: string },
  ) =>
    post<{
      objectType: string
      total: number
      page: number
      pageSize: number
      items: Array<{ id: string; objectKey: string; title: string; properties?: Record<string, unknown> }>
    }>(`/object-sets/${encodeURIComponent(objectType)}/query`, body),

  refresh: () => post<{ ok: boolean }>('/metrics/refresh'),
}
