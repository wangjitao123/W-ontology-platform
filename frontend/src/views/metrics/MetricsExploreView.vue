<template>
  <div class="mx-page">
    <header class="mx-head">
      <div>
        <h2 class="mx-title">指标探查</h2>
        <p class="mx-sub">
          口径来自 Cube，本平台只做转发。
          <template v-if="catalog">
            共 {{ catalog.cubeCount }} 个数据模型 / {{ catalog.measureCount }} 个指标 / {{ catalog.dimensionCount }} 个维度。
          </template>
        </p>
      </div>
      <div class="mx-head__actions">
        <span v-if="health" class="mx-chip" :class="health.ok ? 'is-ok' : 'is-bad'">
          Cube {{ health.ok ? '在线' : '异常' }}
        </span>
        <button class="mx-btn" :disabled="loading" @click="loadCatalog">刷新目录</button>
      </div>
    </header>

    <div class="mx-body">
      <!-- 左：cube 列表 -->
      <aside class="mx-aside">
        <input v-model="cubeSearch" class="mx-input" placeholder="搜索数据模型…" />
        <div v-if="loading" class="mx-hint">加载中…</div>
        <ul v-else class="mx-cubes">
          <li
            v-for="c in filteredCubes"
            :key="c.name"
            class="mx-cube"
            :class="{ 'is-active': activeCube?.name === c.name }"
            @click="selectCube(c)"
          >
            <div class="mx-cube__title">{{ c.title }}</div>
            <div class="mx-cube__meta">{{ c.measures.length }} 指标 · {{ c.dimensions.length }} 维度</div>
          </li>
        </ul>
      </aside>

      <!-- 右：查询构建 -->
      <section class="mx-main">
        <div v-if="!activeCube" class="mx-empty">← 先在左侧选一个数据模型</div>

        <template v-else>
          <div class="mx-cubehead">
            <h3>{{ activeCube.title }}</h3>
            <code class="mx-code">{{ activeCube.name }}</code>
          </div>

          <div class="mx-grid">
            <div class="mx-field">
              <label>指标 <span class="mx-muted">({{ pickedMeasures.length }} 已选)</span></label>
              <div class="mx-choices">
                <label v-for="m in activeCube.measures" :key="m.name" class="mx-choice">
                  <input v-model="pickedMeasures" type="checkbox" :value="m.name" />
                  <span :title="m.description">{{ shortName(m.name) }}<em v-if="m.title"> · {{ m.title }}</em></span>
                </label>
              </div>
            </div>

            <div class="mx-field">
              <label>维度 <span class="mx-muted">({{ pickedDimensions.length }} 已选)</span></label>
              <div class="mx-choices">
                <label v-for="d in activeCube.dimensions" :key="d.name" class="mx-choice">
                  <input v-model="pickedDimensions" type="checkbox" :value="d.name" />
                  <span :title="d.description">{{ shortName(d.name) }}<em v-if="d.title"> · {{ d.title }}</em></span>
                </label>
              </div>
            </div>
          </div>

          <div class="mx-row">
            <label class="mx-inline">
              时间粒度
              <select v-model="timeDim" class="mx-select">
                <option value="">（不使用）</option>
                <option v-for="t in timeDimensions" :key="t.name" :value="t.name">{{ shortName(t.name) }}</option>
              </select>
            </label>
            <label v-if="timeDim" class="mx-inline">
              按
              <select v-model="granularity" class="mx-select">
                <option value="day">日</option>
                <option value="week">周</option>
                <option value="month">月</option>
                <option value="quarter">季度</option>
                <option value="year">年</option>
              </select>
            </label>
            <label class="mx-inline">
              条数
              <input v-model.number="limit" type="number" min="1" max="500" class="mx-select mx-select--num" />
            </label>
            <button class="mx-btn mx-btn--primary" :disabled="!pickedMeasures.length || querying" @click="runQuery">
              {{ querying ? '查询中…' : '查询' }}
            </button>
          </div>

          <div v-if="queryError" class="mx-alert">{{ queryError }}</div>

          <template v-if="result">
            <div class="mx-resulthead">
              <span>{{ result.rowCount }} 行</span>
              <span v-if="resultMs !== null" class="mx-muted">{{ resultMs }} ms</span>
            </div>

            <div v-if="chartable" ref="chartEl" class="mx-chart"></div>

            <div class="mx-tablewrap">
              <table class="mx-table">
                <thead>
                  <tr>
                    <th v-for="col in columns" :key="col">{{ shortName(col) }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in result.data" :key="i">
                    <td v-for="col in columns" :key="col">{{ formatCell(row[col]) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef } from 'vue'
import * as echarts from 'echarts'
import { metricsApi, describeMetricError } from '../../api/metrics'
import type { CubeInfo, MetricsCatalog, MetricsHealth, MetricQueryResult } from '../../api/metrics'

const catalog = ref<MetricsCatalog | null>(null)
const health = ref<MetricsHealth | null>(null)
const loading = ref(false)
const cubeSearch = ref('')

const activeCube = ref<CubeInfo | null>(null)
const pickedMeasures = ref<string[]>([])
const pickedDimensions = ref<string[]>([])
const timeDim = ref('')
const granularity = ref('month')
const limit = ref(20)

const querying = ref(false)
const queryError = ref('')
const result = ref<MetricQueryResult | null>(null)
const resultMs = ref<number | null>(null)

const chartEl = ref<HTMLDivElement | null>(null)
const chart = shallowRef<echarts.ECharts | null>(null)

const filteredCubes = computed(() => {
  const q = cubeSearch.value.trim().toLowerCase()
  const all = catalog.value?.cubes ?? []
  if (!q) return all
  return all.filter(c =>
    c.name.toLowerCase().includes(q) || (c.title ?? '').toLowerCase().includes(q),
  )
})

const timeDimensions = computed(() =>
  (activeCube.value?.dimensions ?? []).filter(d => d.type === 'time'),
)

const columns = computed(() => (result.value?.data?.length ? Object.keys(result.value.data[0]) : []))

const chartable = computed(() => {
  const r = result.value
  if (!r || !r.data.length) return false
  if (!timeDim.value && pickedDimensions.value.length !== 1) return false
  return pickedMeasures.value.length > 0
})

/** cube 内的 measure/dimension 名都带前缀，展示时去掉 */
function shortName(full: string): string {
  const i = full.indexOf('.')
  return i >= 0 ? full.slice(i + 1) : full
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 })
  const s = String(v)
  const n = Number(s)
  if (s !== '' && !Number.isNaN(n) && /^-?\d+(\.\d+)?$/.test(s)) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 4 })
  }
  return s
}

async function loadCatalog() {
  loading.value = true
  try {
    const [cat, h] = await Promise.all([metricsApi.catalog(), metricsApi.health()])
    catalog.value = cat
    health.value = h
  } catch (e) {
    health.value = { ok: false, reason: 'error', message: describeMetricError(e) }
  } finally {
    loading.value = false
  }
}

function selectCube(c: CubeInfo) {
  activeCube.value = c
  pickedMeasures.value = c.measures.slice(0, 1).map(m => m.name)
  pickedDimensions.value = []
  timeDim.value = ''
  result.value = null
  queryError.value = ''
  disposeChart()
}

async function runQuery() {
  if (!activeCube.value) return
  querying.value = true
  queryError.value = ''
  const t0 = performance.now()
  try {
    const q = {
      measures: pickedMeasures.value,
      dimensions: pickedDimensions.value,
      limit: limit.value || 20,
      ...(timeDim.value
        ? { timeDimensions: [{ dimension: timeDim.value, granularity: granularity.value }] }
        : {}),
    }
    result.value = await metricsApi.query(q)
    resultMs.value = Math.round(performance.now() - t0)
    await nextTick()
    renderChart()
  } catch (e) {
    result.value = null
    resultMs.value = null
    queryError.value = describeMetricError(e)
    disposeChart()
  } finally {
    querying.value = false
  }
}

function disposeChart() {
  chart.value?.dispose()
  chart.value = null
}

function renderChart() {
  disposeChart()
  if (!chartable.value || !chartEl.value || !result.value) return

  const rows = result.value.data
  const xField = timeDim.value
    ? columns.value.find(c => c.startsWith(timeDim.value))
    : pickedDimensions.value[0]
  if (!xField) return

  const x = rows.map(r => String(r[xField] ?? ''))
  const isTime = Boolean(timeDim.value)
  const series = pickedMeasures.value
    .filter(m => columns.value.includes(m))
    .map(m => ({
      name: shortName(m),
      type: isTime ? 'line' : 'bar',
      smooth: isTime,
      data: rows.map(r => {
        const n = Number(r[m])
        return Number.isNaN(n) ? null : n
      }),
    }))

  chart.value = echarts.init(chartEl.value)
  chart.value.setOption({
    grid: { left: 60, right: 24, top: 36, bottom: 60 },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, type: 'scroll' },
    xAxis: { type: 'category', data: x, axisLabel: { rotate: x.length > 8 ? 30 : 0 } },
    yAxis: { type: 'value' },
    series,
  })
}

onBeforeUnmount(disposeChart)
loadCatalog()
</script>

<style scoped>
.mx-page { display: flex; flex-direction: column; height: 100%; padding: 20px; gap: 16px; overflow: hidden; }
.mx-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.mx-title { margin: 0; font-size: 20px; font-weight: 600; }
.mx-sub { margin: 4px 0 0; font-size: 13px; color: var(--neutral-500, #868e96); }
.mx-head__actions { display: flex; align-items: center; gap: 10px; }
.mx-chip { font-size: 12px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--neutral-200); }
.mx-chip.is-ok { color: #2f9e44; border-color: #b2f2bb; background: #ebfbee; }
.mx-chip.is-bad { color: #c92a2a; border-color: #ffc9c9; background: #fff5f5; }

.mx-body { display: flex; gap: 16px; flex: 1; min-height: 0; }
.mx-aside { width: 260px; flex: none; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.mx-input { width: 100%; padding: 8px 10px; border: 1px solid var(--neutral-200); border-radius: 8px; background: var(--neutral-0); font-size: 13px; }
.mx-cubes { list-style: none; margin: 0; padding: 0; overflow: auto; flex: 1; }
.mx-cube { padding: 9px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.mx-cube:hover { background: var(--neutral-100, #f1f3f5); }
.mx-cube.is-active { background: var(--primary-50, #e7f5ff); border-color: var(--primary-200, #a5d8ff); }
.mx-cube__title { font-size: 13px; font-weight: 500; }
.mx-cube__meta { font-size: 11px; color: var(--neutral-500, #868e96); margin-top: 2px; }

.mx-main { flex: 1; min-width: 0; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
.mx-cubehead { display: flex; align-items: baseline; gap: 10px; }
.mx-cubehead h3 { margin: 0; font-size: 16px; }
.mx-code { font-size: 12px; color: var(--neutral-500, #868e96); background: var(--neutral-100, #f1f3f5); padding: 2px 6px; border-radius: 4px; }

.mx-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.mx-field label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.mx-choices { max-height: 220px; overflow: auto; border: 1px solid var(--neutral-200); border-radius: 8px; padding: 8px; background: var(--neutral-0); }
.mx-choice { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 0; cursor: pointer; }
.mx-choice em { color: var(--neutral-500, #868e96); font-style: normal; }

.mx-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.mx-inline { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.mx-select { padding: 6px 8px; border: 1px solid var(--neutral-200); border-radius: 6px; background: var(--neutral-0); font-size: 13px; }
.mx-select--num { width: 80px; }
.mx-btn { padding: 7px 14px; border: 1px solid var(--neutral-200); border-radius: 8px; background: var(--neutral-0); font-size: 13px; cursor: pointer; }
.mx-btn:disabled { opacity: .55; cursor: not-allowed; }
.mx-btn--primary { background: var(--primary-500, #228be6); border-color: var(--primary-500, #228be6); color: #fff; }

.mx-alert { padding: 10px 12px; border: 1px solid #ffc9c9; background: #fff5f5; color: #c92a2a; border-radius: 8px; font-size: 13px; }
.mx-resulthead { display: flex; gap: 12px; font-size: 13px; color: var(--neutral-600, #495057); }
.mx-chart { height: 300px; border: 1px solid var(--neutral-200); border-radius: 8px; }
.mx-tablewrap { overflow: auto; border: 1px solid var(--neutral-200); border-radius: 8px; }
.mx-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mx-table th, .mx-table td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--neutral-100, #f1f3f5); white-space: nowrap; }
.mx-table thead th { position: sticky; top: 0; background: var(--neutral-50, #f8f9fa); font-weight: 600; }
.mx-hint, .mx-empty, .mx-muted { color: var(--neutral-500, #868e96); font-size: 13px; }
.mx-empty { padding: 40px 0; }
</style>
