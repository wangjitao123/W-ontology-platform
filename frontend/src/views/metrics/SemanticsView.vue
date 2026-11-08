<template>
  <div class="sem">
    <header class="sem-head">
      <div>
        <h2 class="sem-title">语义层</h2>
        <p class="sem-sub">
          本体告诉你「有什么」，语义层告诉你「怎么算才算对」。下面是业务约束与它们的证据。
        </p>
      </div>
      <button class="sem-btn" :disabled="loading" @click="load">
        {{ loading ? '加载中…' : '刷新' }}
      </button>
    </header>

    <!-- 总览 -->
    <div v-if="overview" class="sem-cards">
      <div class="sem-card">
        <div class="sem-card__num">{{ overview.constraints.total }}</div>
        <div class="sem-card__label">业务约束</div>
        <div class="sem-card__hint">
          block {{ overview.constraints.bySeverity.block || 0 }} ·
          warn {{ overview.constraints.bySeverity.warn || 0 }}
        </div>
      </div>
      <div class="sem-card">
        <div class="sem-card__num">{{ overview.evidence.total }}</div>
        <div class="sem-card__label">证据</div>
        <div class="sem-card__hint">每条约束都有实际查询支撑</div>
      </div>
      <div class="sem-card">
        <div class="sem-card__num">
          {{ overview.terms.withAliases }}/{{ overview.terms.entities }}
        </div>
        <div class="sem-card__label">实体有别名</div>
        <div class="sem-card__hint">口语 → 概念的桥梁</div>
      </div>
      <div class="sem-card">
        <div class="sem-card__num">
          {{ overview.terms.withAggregation }}/{{ overview.terms.attributes }}
        </div>
        <div class="sem-card__label">属性有聚合语义</div>
        <div class="sem-card__hint">能不能 sum、要不要先按快照日去重</div>
      </div>
    </div>

    <!-- 页签 -->
    <div class="sem-tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        class="sem-tab"
        :class="{ 'is-active': tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- 约束 -->
    <section v-if="tab === 'constraints'" class="sem-list">
      <div v-if="!constraints.length && !loading" class="sem-empty">暂无约束</div>
      <article v-for="c in constraints" :key="c.id" class="sem-item" :class="`sem-item--${c.severity}`">
        <div class="sem-item__head">
          <span class="sem-sev" :class="`sem-sev--${c.severity}`">{{ c.severity }}</span>
          <span class="sem-item__name">{{ c.name }}</span>
          <span class="sem-chip">{{ c.constraintType }}</span>
          <span class="sem-chip sem-chip--ghost">{{ c.targetKind }}:{{ c.targetId }}</span>
        </div>
        <p class="sem-item__desc">{{ c.description }}</p>
        <p v-if="c.remedy" class="sem-item__remedy">建议：{{ c.remedy }}</p>
        <div v-if="c.triggerKeywords?.length" class="sem-item__kw">
          <span class="sem-kw-label">触发词</span>
          <span v-for="k in c.triggerKeywords" :key="k" class="sem-kw">{{ k }}</span>
        </div>
        <details v-if="c.evidence?.length" class="sem-item__ev">
          <summary>证据（{{ c.evidence.length }}）</summary>
          <div v-for="e in c.evidence" :key="e.id" class="sem-ev">
            <div class="sem-ev__title">{{ e.title }}</div>
            <div class="sem-ev__row"><b>查询</b><code>{{ e.query }}</code></div>
            <div class="sem-ev__row"><b>结果</b><span>{{ e.result }}</span></div>
            <div class="sem-ev__row"><b>方法</b><span>{{ e.validationMethod }}</span></div>
          </div>
        </details>
      </article>
    </section>

    <!-- 证据 -->
    <section v-else class="sem-list">
      <div v-if="!evidence.length && !loading" class="sem-empty">暂无证据</div>
      <article v-for="e in evidence" :key="e.id" class="sem-item">
        <div class="sem-item__head">
          <span class="sem-item__name">{{ e.title }}</span>
          <span class="sem-chip">{{ e.source }}</span>
          <span class="sem-chip sem-chip--ghost">{{ e.validationMethod }}</span>
        </div>
        <div class="sem-ev__row"><b>查询</b><code>{{ e.query }}</code></div>
        <div class="sem-ev__row"><b>结果</b><span>{{ e.result }}</span></div>
      </article>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  semanticsApi,
  type SemanticsOverview,
  type SemanticConstraint,
  type SemanticEvidence,
} from '../../api/semantics'

const TABS = [
  { key: 'constraints', label: '业务约束' },
  { key: 'evidence', label: '证据' },
] as const

const tab = ref<'constraints' | 'evidence'>('constraints')
const loading = ref(false)
const overview = ref<SemanticsOverview | null>(null)
const constraints = ref<SemanticConstraint[]>([])
const evidence = ref<SemanticEvidence[]>([])
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [o, c, e] = await Promise.all([
      semanticsApi.overview(),
      semanticsApi.constraints(),
      semanticsApi.evidence(),
    ])
    overview.value = o
    constraints.value = c
    evidence.value = e
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sem { display: flex; flex-direction: column; gap: 14px; padding: 4px; }
.sem-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.sem-title { margin: 0; font-size: 17px; font-weight: 600; }
.sem-sub { margin: 4px 0 0; font-size: 12px; color: var(--neutral-500, #868e96); }
.sem-btn { font-size: 12px; padding: 5px 12px; border: 1px solid var(--neutral-200); border-radius: 7px; background: var(--neutral-0); cursor: pointer; }
.sem-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.sem-card { border: 1px solid var(--neutral-200); border-radius: 10px; padding: 10px 12px; background: var(--neutral-0); }
.sem-card__num { font-size: 20px; font-weight: 600; }
.sem-card__label { font-size: 12px; color: var(--neutral-700, #343a40); margin-top: 2px; }
.sem-card__hint { font-size: 11px; color: var(--neutral-500, #868e96); margin-top: 3px; }
.sem-tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--neutral-200); }
.sem-tab { font-size: 13px; padding: 6px 12px; border: none; background: transparent; cursor: pointer; color: var(--neutral-600, #495057); border-bottom: 2px solid transparent; }
.sem-tab.is-active { color: var(--primary-600, #1971c2); border-bottom-color: var(--primary-600, #1971c2); font-weight: 600; }
.sem-list { display: flex; flex-direction: column; gap: 10px; }
.sem-empty { font-size: 13px; color: var(--neutral-500, #868e96); padding: 20px 0; text-align: center; }
.sem-item { border: 1px solid var(--neutral-200); border-left: 3px solid var(--neutral-300, #dee2e6); border-radius: 10px; padding: 10px 12px; background: var(--neutral-0); }
.sem-item--block { border-left-color: #e03131; }
.sem-item--warn { border-left-color: #f08c00; }
.sem-item__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sem-item__name { font-size: 14px; font-weight: 600; }
.sem-sev { font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 1px 6px; border-radius: 4px; background: var(--neutral-100, #f1f3f5); }
.sem-sev--block { background: #ffe3e3; color: #c92a2a; }
.sem-sev--warn { background: #fff4e6; color: #b35c00; }
.sem-sev--info { background: #e7f5ff; color: #1864ab; }
.sem-chip { font-size: 10px; padding: 1px 6px; border-radius: 4px; background: var(--neutral-100, #f1f3f5); color: var(--neutral-700, #343a40); }
.sem-chip--ghost { background: transparent; border: 1px solid var(--neutral-200); color: var(--neutral-500, #868e96); }
.sem-item__desc { font-size: 13px; line-height: 1.6; margin: 6px 0 0; }
.sem-item__remedy { font-size: 12px; line-height: 1.6; margin: 4px 0 0; color: #0b7285; }
.sem-item__kw { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin-top: 7px; }
.sem-kw-label { font-size: 11px; color: var(--neutral-500, #868e96); }
.sem-kw { font-size: 11px; padding: 0 6px; border-radius: 999px; background: var(--neutral-100, #f1f3f5); }
.sem-item__ev { margin-top: 8px; font-size: 12px; }
.sem-item__ev summary { cursor: pointer; color: var(--primary-600, #1971c2); }
.sem-ev { margin-top: 8px; padding: 8px 10px; background: var(--neutral-50, #f8f9fa); border-radius: 8px; }
.sem-ev__title { font-weight: 600; margin-bottom: 4px; }
.sem-ev__row { display: flex; gap: 6px; margin-top: 3px; line-height: 1.5; }
.sem-ev__row b { flex: none; color: var(--neutral-500, #868e96); font-weight: 500; }
.sem-ev__row code { font-size: 11px; word-break: break-all; background: var(--neutral-100, #f1f3f5); padding: 1px 4px; border-radius: 4px; }
</style>
