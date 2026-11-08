<template>
  <div class="ask-page">
    <header class="ask-head">
      <div>
        <h2 class="ask-title">问数</h2>
        <p class="ask-sub">
          平台自己判断该用哪套引擎回答，并<strong>告诉你答案来自哪一套</strong>。
        </p>
      </div>
      <div class="ask-engines">
        <span class="ask-chip" :class="health?.cube.ok ? 'is-ok' : 'is-bad'">
          指标层 {{ health?.cube.ok ? `(${health?.cube.measureCount} 指标)` : '不可用' }}
        </span>
        <span class="ask-chip" :class="health?.adHoc.ok ? 'is-ok' : 'is-warn'">
          临时分析 {{ health?.adHoc.ok ? '可用' : '不可用' }}
        </span>
      </div>
    </header>

    <!-- 提问 -->
    <div class="ask-box">
      <textarea
        v-model="question"
        class="ask-input"
        rows="2"
        placeholder="例如：2026年7月资产总额多少 / 各省合同数排名前5 / 每个客户最近3次订单的间隔"
        @keydown.ctrl.enter="submit"
        @keydown.meta.enter="submit"
      ></textarea>
      <div class="ask-box__foot">
        <div class="ask-examples">
          <label v-if="databases.length" class="ask-db">
            查询库
            <select v-model="targetDb" class="ask-db__select">
              <option v-for="d in databases" :key="d.database" :value="d.database">
                {{ d.database }}
              </option>
            </select>
          </label>
          <span class="ask-muted">试试：</span>
          <button v-for="ex in examples" :key="ex" class="ask-ex" @click="useExample(ex)">{{ ex }}</button>
        </div>
        <button class="ask-btn ask-btn--primary" :disabled="!question.trim() || loading" @click="submit()">
          {{ loading ? '思考中…' : '提问 (Ctrl+Enter)' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="ask-alert">{{ error }}</div>

    <!-- 结果 -->
    <div v-if="result" class="ask-result">
      <!-- 来源标注：方案 B 的核心 -->
      <div class="ask-badge" :class="badgeClass">
        <span class="ask-badge__dot"></span>
        <span class="ask-badge__label">{{ badgeLabel }}</span>
        <span class="ask-badge__source">{{ result.source }}</span>
      </div>

      <p v-if="result.note" class="ask-note">{{ result.note }}</p>

      <!-- 被语义护栏拦截：不给数字，只给原因和修正方式 -->
      <div v-if="result.blocked" class="ask-blocked">
        <div class="ask-blocked__head">
          <span class="ask-blocked__icon">⛔</span>
          <span>已拦截：这条查询会算出错误的数字</span>
        </div>
        <div v-for="g in violatedGuardrails" :key="g.id" class="ask-blocked__item">
          <div class="ask-blocked__name">{{ g.name }}</div>
          <div v-if="g.reason" class="ask-blocked__reason">{{ g.reason }}</div>
          <div v-if="g.remedy" class="ask-blocked__remedy">修正方式：{{ g.remedy }}</div>
        </div>
        <div class="ask-blocked__foot">
          <button class="ask-btn" :disabled="loading" @click="forceRun">
            确实要查看原始结果（可能算错）
          </button>
        </div>
      </div>

      <!-- 语义层护栏：与这个问题相关的业务约束 -->
      <div v-if="!result.blocked && result.guardrails?.length" class="ask-guards">
        <div class="ask-guards__title">
          <span class="ask-guards__icon">⚠</span>
          这个口径有 {{ result.guardrails.length }} 条已知约束
        </div>
        <div
          v-for="g in result.guardrails"
          :key="g.id"
          class="ask-guard"
          :class="`ask-guard--${g.severity}`"
        >
          <div class="ask-guard__head">
            <span class="ask-guard__sev">{{ g.severity }}</span>
            <span class="ask-guard__name">{{ g.name }}</span>
            <span class="ask-guard__type">{{ g.constraintType }}</span>
          </div>
          <div class="ask-guard__desc">{{ g.description }}</div>
          <div v-if="g.remedy" class="ask-guard__remedy">建议：{{ g.remedy }}</div>
        </div>
      </div>

      <!-- 答案 -->
      <div v-if="result.ok" class="ask-answer">
        <div class="ask-answer__label">答案</div>
        <div class="ask-answer__body">{{ result.answer }}</div>
      </div>

      <!-- 不可用 -->
      <div v-else class="ask-alert ask-alert--soft">
        <strong>{{ result.reason }}</strong>
        <div>{{ result.message }}</div>
        <div v-if="result.hint" class="ask-hint">{{ result.hint }}</div>
      </div>

      <!-- 判断理由 -->
      <div v-if="result.decision" class="ask-meta">
        <span class="ask-muted">路由理由：</span>{{ result.decision }}
      </div>

      <!-- Cube 路径：生成的查询 + 数据 -->
      <template v-if="result.route === 'cube' && result.query">
        <details class="ask-details">
          <summary>平台生成的 Cube 查询（可审计）</summary>
          <pre class="ask-pre">{{ JSON.stringify(result.query, null, 2) }}</pre>
        </details>
        <div v-if="result.data?.length" class="ask-tablewrap">
          <table class="ask-table">
            <thead>
              <tr><th v-for="c in columns" :key="c">{{ shortName(c) }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in result.data" :key="i">
                <td v-for="c in columns" :key="c">{{ formatCell(row[c]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- ad_hoc 路径：SQL -->
      <details v-if="result.route === 'ad_hoc' && result.sql" class="ask-details">
        <summary>临时分析生成的 SQL（<strong>未经口径审查</strong>）</summary>
        <pre class="ask-pre">{{ result.sql }}</pre>
      </details>
    </div>

    <div v-else-if="!loading" class="ask-empty">
      <p>输入一个问题，看平台如何选择引擎。</p>
      <ul class="ask-empty__list">
        <li><b>能用已定义指标回答</b> → 走 Cube，标注「受治理口径」，同一问题永远同一个数</li>
        <li><b>需要新算法/新公式</b> → 走临时分析，标注「临时分析」，仅供参考</li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { askApi, type AskDatabase, type AskHealth, type AskResult } from '../../api/ask'

const question = ref('')
const loading = ref(false)
const result = ref<AskResult | null>(null)
const error = ref('')
const health = ref<AskHealth | null>(null)
const databases = ref<AskDatabase[]>([])
const targetDb = ref<string>('')

const examples = [
  '2026年7月资产总额多少',
  '各省合同数排名前5',
  '每个客户最近3次订单的平均间隔',
]

const columns = computed(() => {
  const d = result.value?.data
  return d && d.length ? Object.keys(d[0]) : []
})

const badgeLabel = computed(() => {
  const r = result.value
  if (!r) return ''
  if (r.governed === true) return '受治理口径'
  if (r.governed === false) return '临时分析（非受治理）'
  return '未知来源'
})

const badgeClass = computed(() => {
  const r = result.value
  if (!r) return ''
  if (r.governed === true) return 'is-governed'
  if (r.governed === false) return 'is-adhoc'
  return 'is-unknown'
})

function shortName(full: string): string {
  const i = full.indexOf('.')
  return i >= 0 ? full.slice(i + 1) : full
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isNaN(n) && String(v).trim() !== '') {
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  return String(v)
}

function useExample(ex: string) {
  question.value = ex
  submit()
}

async function submit(force = false) {
  const q = question.value.trim()
  if (!q || loading.value) return
  loading.value = true
  error.value = ''
  result.value = null
  try {
    result.value = await askApi.ask(q, targetDb.value || null, force)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : (e as Error).message
  } finally {
    loading.value = false
  }
}

/** 拦截后可强制查看：用户明确知道结果可能错 */
function forceRun() {
  submit(true)
}

/** 被检测器判定"确定违反"的约束（而不是关键词命中的提示） */
const violatedGuardrails = computed(() =>
  (result.value?.guardrails ?? []).filter(g => g.violated),
)

askApi.health().then(h => { health.value = h }).catch(() => { /* 健康检查失败不阻塞 */ })
askApi.databases().then(d => {
  databases.value = d.databases ?? []
  targetDb.value = d.default || databases.value[0]?.database || ''
}).catch(() => { /* 选库失败不阻塞 */ })
</script>

<style scoped>
.ask-page { padding: 20px; max-width: 980px; display: flex; flex-direction: column; gap: 16px; overflow: auto; height: 100%; }
.ask-title { margin: 0; font-size: 20px; font-weight: 600; }
.ask-sub { margin: 4px 0 0; font-size: 13px; color: var(--neutral-500, #868e96); }
.ask-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.ask-engines { display: flex; gap: 8px; }
.ask-chip { font-size: 12px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--neutral-200); }
.ask-chip.is-ok { color: #2f9e44; border-color: #b2f2bb; background: #ebfbee; }
.ask-chip.is-warn { color: #e8590c; border-color: #ffd8a8; background: #fff4e6; }
.ask-chip.is-bad { color: #c92a2a; border-color: #ffc9c9; background: #fff5f5; }

.ask-box { border: 1px solid var(--neutral-200); border-radius: 12px; padding: 12px; background: var(--neutral-0); }
.ask-input { width: 100%; border: none; outline: none; resize: vertical; font-size: 14px; font-family: inherit; background: transparent; }
.ask-box__foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; flex-wrap: wrap; }
.ask-examples { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ask-ex { font-size: 11px; padding: 3px 8px; border: 1px solid var(--neutral-200); border-radius: 999px; background: transparent; cursor: pointer; color: var(--neutral-600, #495057); }
.ask-ex:hover { background: var(--neutral-100, #f1f3f5); }
.ask-db { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--neutral-600, #495057); }
.ask-db__select { padding: 3px 6px; border: 1px solid var(--neutral-200); border-radius: 6px; background: var(--neutral-0); font-size: 12px; }
.ask-btn { padding: 7px 14px; border: 1px solid var(--neutral-200); border-radius: 8px; background: var(--neutral-0); font-size: 13px; cursor: pointer; }
.ask-btn:disabled { opacity: .55; cursor: not-allowed; }
.ask-btn--primary { background: var(--primary-500, #228be6); border-color: var(--primary-500, #228be6); color: #fff; }

.ask-result { display: flex; flex-direction: column; gap: 12px; }
.ask-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 10px; font-size: 13px; border: 1px solid; }
.ask-badge__dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.ask-badge__label { font-weight: 600; }
.ask-badge__source { opacity: .8; font-size: 12px; }
.ask-badge.is-governed { color: #2b8a3e; border-color: #b2f2bb; background: #ebfbee; }
.ask-badge.is-adhoc { color: #e8590c; border-color: #ffd8a8; background: #fff4e6; }
.ask-badge.is-unknown { color: var(--neutral-600, #495057); border-color: var(--neutral-200); background: var(--neutral-50, #f8f9fa); }

.ask-note { margin: 0; font-size: 12px; color: var(--neutral-500, #868e96); }
/* 语义层护栏 */
.ask-guards { border: 1px solid #ffd8a8; background: #fff9f0; border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
.ask-guards__title { font-size: 12px; font-weight: 600; color: #b35c00; display: flex; align-items: center; gap: 6px; }
.ask-guards__icon { font-size: 13px; }
.ask-guard { border-left: 3px solid #f59f00; padding-left: 8px; }
.ask-guard--block { border-left-color: #e03131; }
.ask-guard--info { border-left-color: #4dabf7; }
.ask-guard__head { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.ask-guard__sev { font-size: 10px; text-transform: uppercase; font-weight: 700; color: #e03131; }
.ask-guard--warn .ask-guard__sev { color: #f08c00; }
.ask-guard--info .ask-guard__sev { color: #1c7ed6; }
.ask-guard__name { font-weight: 600; color: var(--neutral-800, #212529); }
.ask-guard__type { font-size: 10px; color: var(--neutral-500, #868e96); border: 1px solid var(--neutral-200); border-radius: 4px; padding: 0 4px; }
.ask-guard__desc { font-size: 12px; color: var(--neutral-700, #343a40); margin-top: 3px; line-height: 1.5; }
.ask-guard__remedy { font-size: 12px; color: #0b7285; margin-top: 3px; line-height: 1.5; }
/* 拦截态 */
.ask-blocked { border: 1px solid #ffc9c9; background: #fff5f5; border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.ask-blocked__head { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; color: #c92a2a; }
.ask-blocked__icon { font-size: 15px; }
.ask-blocked__item { border-left: 3px solid #e03131; padding-left: 9px; }
.ask-blocked__name { font-size: 13px; font-weight: 600; color: #212529; }
.ask-blocked__reason { font-size: 12px; color: #343a40; margin-top: 3px; line-height: 1.55; }
.ask-blocked__remedy { font-size: 12px; color: #0b7285; margin-top: 3px; line-height: 1.55; }
.ask-blocked__foot { display: flex; justify-content: flex-end; }
.ask-answer { border: 1px solid var(--neutral-200); border-radius: 10px; padding: 12px 14px; background: var(--neutral-0); }
.ask-answer__label { font-size: 11px; color: var(--neutral-500, #868e96); margin-bottom: 4px; }
.ask-answer__body { font-size: 16px; font-weight: 500; word-break: break-word; }
.ask-alert { padding: 10px 12px; border: 1px solid #ffc9c9; background: #fff5f5; color: #c92a2a; border-radius: 8px; font-size: 13px; }
.ask-alert--soft { border-color: #ffd8a8; background: #fff4e6; color: #b35c00; }
.ask-hint { margin-top: 6px; font-size: 12px; opacity: .9; }
.ask-meta { font-size: 12px; color: var(--neutral-600, #495057); }
.ask-details { font-size: 12px; }
.ask-details summary { cursor: pointer; color: var(--neutral-600, #495057); }
.ask-pre { margin: 8px 0 0; padding: 10px; background: var(--neutral-50, #f8f9fa); border-radius: 8px; overflow: auto; font-size: 11px; line-height: 1.5; }
.ask-tablewrap { overflow: auto; border: 1px solid var(--neutral-200); border-radius: 8px; }
.ask-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.ask-table th, .ask-table td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--neutral-100, #f1f3f5); white-space: nowrap; }
.ask-table thead th { background: var(--neutral-50, #f8f9fa); font-weight: 600; }
.ask-muted { color: var(--neutral-500, #868e96); }
.ask-empty { padding: 24px 0; color: var(--neutral-500, #868e96); font-size: 13px; }
.ask-empty__list { margin: 10px 0 0; padding-left: 20px; line-height: 1.9; }
</style>
