<template>
  <div class="om-page">
    <header class="om-head">
      <div>
        <h2 class="om-title">对象指标</h2>
        <p class="om-sub">
          选一个对象实例，看它自己的指标。过滤规则来自「对象↔指标绑定」，口径仍由 Cube 定义。
        </p>
      </div>
    </header>

    <div class="om-body">
      <!-- 左：对象类型 -->
      <aside class="om-aside">
        <div class="om-aside__label">对象类型</div>
        <div v-if="loadingTypes" class="om-hint">加载中…</div>
        <ul v-else class="om-types">
          <template v-for="sec in groupedTypes" :key="sec.kind">
            <li class="om-kind" :title="sec.hint">{{ sec.label }}（{{ sec.types.length }}）</li>
            <li
              v-for="t in sec.types"
              :key="t.id"
              class="om-type"
              :class="{ 'is-active': activeType?.id === t.id }"
              @click="selectType(t)"
            >
              <div class="om-type__name">
                {{ t.name_cn }}
                <span v-if="boundTypeIds.has(t.id)" class="om-badge">有指标</span>
              </div>
              <div class="om-type__id">{{ t.id }}</div>
            </li>
          </template>
        </ul>
      </aside>

      <!-- 中：实例列表 -->
      <section class="om-mid">
        <div v-if="!activeType" class="om-empty">← 先选一个对象类型</div>
        <template v-else>
          <input
            v-model="instanceSearch"
            class="om-input"
            placeholder="搜索实例（标题或主键）…"
            @keyup.enter="loadInstances(1)"
          />
          <div class="om-toolbar">
            <span class="om-muted">共 {{ instanceTotal }} 个</span>
            <button class="om-btn" :disabled="loadingInstances" @click="loadInstances(instancePage)">
              {{ loadingInstances ? '…' : '刷新' }}
            </button>
          </div>
          <div v-if="instanceError" class="om-alert">{{ instanceError }}</div>
          <ul class="om-instances">
            <li
              v-for="it in instances"
              :key="it.id"
              class="om-instance"
              :class="{
                'is-active': activeInstance?.objectKey === it.objectKey,
                'is-nokey': !hasMetricKey(it),
              }"
              @click="selectInstance(it)"
            >
              <div class="om-instance__title">
                {{ it.title || it.objectKey }}
                <span v-if="!hasMetricKey(it)" class="om-tag">无{{ activeBinding?.keyColumn }}</span>
              </div>
              <div class="om-instance__key">{{ it.objectKey }}</div>
            </li>
          </ul>
          <div class="om-pager">
            <button class="om-btn" :disabled="instancePage <= 1" @click="loadInstances(instancePage - 1)">上一页</button>
            <span class="om-muted">第 {{ instancePage }} 页</span>
            <button class="om-btn" :disabled="instances.length < pageSize" @click="loadInstances(instancePage + 1)">下一页</button>
          </div>
        </template>
      </section>

      <!-- 右：指标 -->
      <section class="om-main">
        <div v-if="!activeInstance" class="om-empty">← 再选一个实例</div>
        <template v-else>
          <div class="om-mhead">
            <div>
              <h3>{{ activeInstance.title || activeInstance.objectKey }}</h3>
              <code class="om-code">{{ activeInstance.objectKey }}</code>
            </div>
            <button class="om-btn" :disabled="loadingMetrics" @click="loadMetrics">
              {{ loadingMetrics ? '查询中…' : '刷新' }}
            </button>
          </div>

          <div v-if="metricsNote" class="om-hint">{{ metricsNote }}</div>

          <div v-for="(m, i) in metricBlocks" :key="i" class="om-block">
            <div class="om-block__head">
              <span class="om-block__label">{{ m.label }}</span>
              <span class="om-block__cube">{{ m.cube }} · {{ shortName(m.filterMember) }} = {{ m.filterValue ?? '—' }}</span>
            </div>

            <div v-if="m.error" class="om-alert om-alert--sm">
              <strong>{{ m.error.reason }}</strong> — {{ humanError(m.error) }}
            </div>
            <div v-else class="om-cards">
              <div v-for="(v, k) in firstRow(m)" :key="k" class="om-card">
                <div class="om-card__value">{{ formatNumber(v) }}</div>
                <div class="om-card__label">{{ shortName(String(k)) }}</div>
              </div>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { get } from '../../api/client'
import { metricsApi, describeMetricError } from '../../api/metrics'
import type { ObjectMetricBindingInfo, ObjectMetricItem } from '../../api/metrics'

interface EntityBrief {
  id: string
  name_cn: string
  name?: string
}

interface InstanceBrief {
  id: string
  objectKey: string
  title: string
  properties?: Record<string, unknown>
}

const objectTypes = ref<EntityBrief[]>([])
const bindings = ref<ObjectMetricBindingInfo[]>([])
const loadingTypes = ref(false)
const activeType = ref<EntityBrief | null>(null)

/**
 * 对象类型分三类，来自 /studio/tbox 的 objectKind：
 *   entity   身份稳定、可被操作、能挂指标 —— 这才是"对象"
 *   snapshot 事实快照，主键里带日期，作为实体的时间序列存在
 *   report   聚合报表，不该当成对象
 * 混在一起会让人以为"报表行"和"客户"是一回事，所以在 UI 上分组。
 */
const typeKinds = ref<Record<string, string>>({})
const KIND_SECTIONS = [
  { kind: 'entity', label: '实体', hint: '身份跨时间稳定，可被操作' },
  { kind: 'snapshot', label: '快照', hint: '事实行，按时间序列挂在实体上' },
  { kind: 'report', label: '报表', hint: '聚合结果，不是对象' },
]

const instances = ref<InstanceBrief[]>([])
const instanceTotal = ref(0)
const instancePage = ref(1)
const pageSize = 20
const instanceSearch = ref('')
const loadingInstances = ref(false)
const instanceError = ref('')
const activeInstance = ref<InstanceBrief | null>(null)

const metricBlocks = ref<ObjectMetricItem[]>([])
const metricsNote = ref('')
const loadingMetrics = ref(false)

const hasType = computed(() => objectTypes.value.length > 0)

/** 当前对象类型对应的指标绑定（决定用哪个属性去过滤） */
const activeBinding = computed(
  () => bindings.value.find(b => b.objectTypeId === activeType.value?.id) ?? null,
)

/** 实例是否具备过滤指标所需的属性值；缺这个值就不可能查出指标 */
function hasMetricKey(it: InstanceBrief): boolean {
  const b = activeBinding.value
  if (!b) return true
  const v = it.properties?.[b.keyColumn]
  return v !== null && v !== undefined && String(v) !== ''
}

/** 配了指标绑定的对象类型（用 Set 做 O(1) 判断） */
const boundTypeIds = computed(() => new Set(bindings.value.map(b => b.objectTypeId)))

/** 有指标的排前面，避免一进页面就选中一个查不出指标的类型 */
const sortedTypes = computed(() => {
  const ids = boundTypeIds.value
  return [...objectTypes.value].sort((a, b) => {
    const wa = ids.has(a.id) ? 0 : 1
    const wb = ids.has(b.id) ? 0 : 1
    if (wa !== wb) return wa - wb
    return a.id.localeCompare(b.id)
  })
})

/** 按 objectKind 分组，空组不显示 */
const groupedTypes = computed(() => {
  const byKind: Record<string, EntityBrief[]> = {}
  for (const t of sortedTypes.value) {
    const k = typeKinds.value[t.id] || 'entity'
    ;(byKind[k] ||= []).push(t)
  }
  return KIND_SECTIONS
    .filter(s => (byKind[s.kind] || []).length > 0)
    .map(s => ({ ...s, types: byKind[s.kind] }))
})

function shortName(full: string): string {
  const i = full.indexOf('.')
  return i >= 0 ? full.slice(i + 1) : full
}

function humanError(e: { reason: string; message: string }): string {
  if (e.reason === 'missing_key') return '该实例缺少绑定用的属性值，无法过滤指标'
  if (e.reason === 'query_failed') return `${e.message}（通常是 Cube 连不上 Doris：检查 VPN）`
  return e.message
}

function firstRow(m: ObjectMetricItem): Record<string, unknown> {
  return m.data?.[0] ?? {}
}

function formatNumber(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

async function loadTypes() {
  loadingTypes.value = true
  try {
    const [types, binds] = await Promise.all([
      get<EntityBrief[]>('/entities'),
      metricsApi.objectBindings().catch(() => [] as ObjectMetricBindingInfo[]),
    ])
    objectTypes.value = types
    bindings.value = binds
    // 分类来自 tbox（objectKind）；拿不到就全部当实体，不影响使用
    try {
      const tbox = await get<{ objectTypes?: Array<{ apiName: string; objectKind?: string }> }>(
        '/studio/tbox',
      )
      const map: Record<string, string> = {}
      for (const t of tbox.objectTypes ?? []) {
        map[t.apiName] = t.objectKind || 'entity'
      }
      typeKinds.value = map
    } catch {
      typeKinds.value = {}
    }
  } catch (e) {
    instanceError.value = describeMetricError(e)
  } finally {
    loadingTypes.value = false
  }
}

function selectType(t: EntityBrief) {
  activeType.value = t
  activeInstance.value = null
  metricsNote.value = ''
  instanceError.value = ''
  instanceSearch.value = ''
  instancePage.value = 1
  loadInstances(1)
}

async function loadInstances(page: number) {
  if (!activeType.value) return
  loadingInstances.value = true
  instanceError.value = ''
  try {
    const res = await metricsApi.queryObjects(activeType.value.id, {
      search: instanceSearch.value || undefined,
      page,
      page_size: pageSize,
      prefer_key: activeBinding.value?.keyColumn,
    })
    instances.value = res.items
    instanceTotal.value = res.total
    instancePage.value = page

    // 自动选一个"能查出指标"的实例：没有过滤属性值的实例点进去只会报 missing_key
    const stillThere = instances.value.some(
      i => i.objectKey === activeInstance.value?.objectKey,
    )
    if (!stillThere) {
      const pick = instances.value.find(hasMetricKey) ?? instances.value[0] ?? null
      activeInstance.value = pick
      if (pick) await loadMetrics()
    }
  } catch (e) {
    instances.value = []
    instanceError.value = describeMetricError(e)
  } finally {
    loadingInstances.value = false
  }
}

async function selectInstance(it: InstanceBrief) {
  activeInstance.value = it
  await loadMetrics()
}

async function loadMetrics() {
  if (!activeType.value || !activeInstance.value) return
  loadingMetrics.value = true
  metricsNote.value = ''
  metricBlocks.value = []
  try {
    const res = await metricsApi.objectMetrics(activeType.value.id, activeInstance.value.objectKey)
    metricBlocks.value = res.metrics ?? []
    metricsNote.value = res.note ?? ''
    if (!metricBlocks.value.length && !res.note) {
      metricsNote.value = '该对象类型没有配置指标绑定'
    }
  } catch (e) {
    instanceError.value = describeMetricError(e)
  } finally {
    loadingMetrics.value = false
  }
}

loadTypes().then(() => {
  if (hasType.value) selectType(sortedTypes.value[0])
})
</script>

<style scoped>
.om-page { display: flex; flex-direction: column; height: 100%; padding: 20px; gap: 16px; overflow: hidden; }
.om-title { margin: 0; font-size: 20px; font-weight: 600; }
.om-sub { margin: 4px 0 0; font-size: 13px; color: var(--neutral-500, #868e96); }

.om-body { display: flex; gap: 16px; flex: 1; min-height: 0; }
.om-aside { width: 200px; flex: none; display: flex; flex-direction: column; gap: 8px; min-height: 0; }
.om-aside__label { font-size: 12px; color: var(--neutral-500, #868e96); }
.om-types { list-style: none; margin: 0; padding: 0; overflow: auto; flex: 1; }
.om-kind { font-size: 11px; font-weight: 600; color: var(--neutral-500, #868e96); letter-spacing: 0.04em; padding: 12px 10px 4px; text-transform: none; border-top: 1px solid var(--neutral-100, #f1f3f5); margin-top: 4px; }
.om-kind:first-child { border-top: none; margin-top: 0; padding-top: 2px; }
.om-type { padding: 8px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.om-type:hover { background: var(--neutral-100, #f1f3f5); }
.om-type.is-active { background: var(--primary-50, #e7f5ff); border-color: var(--primary-200, #a5d8ff); }
.om-type__name { font-size: 13px; font-weight: 500; display: flex; align-items: center; gap: 6px; }
.om-type__id { font-size: 11px; color: var(--neutral-500, #868e96); }
.om-badge {
  font-size: 10px; font-weight: 400; padding: 1px 5px; border-radius: 999px;
  color: #2f9e44; background: #ebfbee; border: 1px solid #b2f2bb;
}

.om-mid { width: 300px; flex: none; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.om-input { width: 100%; padding: 8px 10px; border: 1px solid var(--neutral-200); border-radius: 8px; background: var(--neutral-0); font-size: 13px; }
.om-toolbar { display: flex; align-items: center; justify-content: space-between; }
.om-instances { list-style: none; margin: 0; padding: 0; overflow: auto; flex: 1; border: 1px solid var(--neutral-200); border-radius: 8px; }
.om-instance { padding: 8px 10px; cursor: pointer; border-bottom: 1px solid var(--neutral-100, #f1f3f5); }
.om-instance:hover { background: var(--neutral-100, #f1f3f5); }
.om-instance.is-active { background: var(--primary-50, #e7f5ff); }
.om-instance.is-nokey .om-instance__title { color: var(--neutral-500, #868e96); }
.om-tag {
  font-size: 10px; padding: 1px 5px; border-radius: 999px; margin-left: 4px;
  color: var(--neutral-500, #868e96); background: var(--neutral-100, #f1f3f5);
}
.om-instance__title { font-size: 13px; }
.om-instance__key { font-size: 11px; color: var(--neutral-500, #868e96); word-break: break-all; }
.om-pager { display: flex; align-items: center; gap: 8px; }

.om-main { flex: 1; min-width: 0; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
.om-mhead { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.om-mhead h3 { margin: 0; font-size: 16px; }
.om-code { font-size: 12px; color: var(--neutral-500, #868e96); background: var(--neutral-100, #f1f3f5); padding: 2px 6px; border-radius: 4px; }

.om-block { border: 1px solid var(--neutral-200); border-radius: 10px; padding: 12px 14px; }
.om-block__head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
.om-block__label { font-size: 13px; font-weight: 600; }
.om-block__cube { font-size: 11px; color: var(--neutral-500, #868e96); }
.om-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.om-card { background: var(--neutral-0); border: 1px solid var(--neutral-200); border-radius: 10px; padding: 12px; }
.om-card__value { font-size: 18px; font-weight: 600; }
.om-card__label { font-size: 11px; color: var(--neutral-500, #868e96); margin-top: 4px; }

.om-btn { padding: 6px 12px; border: 1px solid var(--neutral-200); border-radius: 8px; background: var(--neutral-0); font-size: 12px; cursor: pointer; }
.om-btn:disabled { opacity: .55; cursor: not-allowed; }
.om-alert { padding: 9px 12px; border: 1px solid #ffc9c9; background: #fff5f5; color: #c92a2a; border-radius: 8px; font-size: 12px; }
.om-alert--sm { padding: 7px 10px; }
.om-hint, .om-muted, .om-empty { color: var(--neutral-500, #868e96); font-size: 13px; }
.om-empty { padding: 40px 0; }
</style>
