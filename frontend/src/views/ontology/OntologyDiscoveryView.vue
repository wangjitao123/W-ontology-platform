<template>
  <div class="disc-page">
    <header class="disc-head">
      <div>
        <h2 class="disc-title">从表结构自动发现本体</h2>
        <p class="disc-sub">
          扫描 Doris 的 <code>dwh_dwd</code> / <code>dwh_dws</code> / <code>dwh_ads</code>，
          按列名前缀家族推导对象类型候选。<strong>不调 LLM</strong>，每条候选都能说清依据。
        </p>
      </div>
      <div class="disc-head__actions">
        <button class="btn-ghost" :disabled="scanning" @click="doScan(true)">
          {{ scanning ? '扫描中...' : '重新扫描' }}
        </button>
        <button class="btn-primary" :disabled="!selected.length || adopting" @click="doAdopt">
          {{ adopting ? '采纳中...' : `采纳 ${selected.length} 项为草稿版本` }}
        </button>
      </div>
    </header>

    <div v-if="errorMsg" class="disc-error">{{ errorMsg }}</div>

    <div v-if="result" class="disc-stats">
      <div class="stat"><span class="stat__label">表</span><strong>{{ result.stats.tables }}</strong></div>
      <div class="stat"><span class="stat__label">列</span><strong>{{ result.stats.columns }}</strong></div>
      <div class="stat"><span class="stat__label">实体候选</span><strong>{{ result.stats.entityCandidates }}</strong></div>
      <div class="stat"><span class="stat__label">快照候选</span><strong>{{ result.stats.snapshotCandidates }}</strong></div>
      <div class="stat"><span class="stat__label">关系候选</span><strong>{{ result.stats.relationCandidates }}</strong></div>
      <div class="stat stat--time"><span class="stat__label">扫描于</span><strong>{{ result.scannedAt }}</strong></div>
    </div>

    <div v-if="scanning && !result" class="disc-loading">正在扫描表结构...</div>

    <div v-if="result" class="disc-body">
      <!-- 实体 -->
      <section class="disc-section">
        <div class="disc-section__head">
          <h3>对象类型候选（{{ result.entities.length }}）</h3>
          <div class="disc-section__ops">
            <button class="btn-link" @click="selectAll('entities', true)">全选高置信</button>
            <button class="btn-link btn-link--muted" @click="clearKind('entities')">清空</button>
          </div>
        </div>
        <p class="disc-section__hint">
          判定依据：列名前缀家族 + 有编码列 + 有描述列。
          「已存在」的会被跳过；「相似」的说明现有本体里可能已经有同一个概念。
        </p>
        <div class="card-list">
          <label
            v-for="p in result.entities"
            :key="p.id"
            class="pcard"
            :class="{ 'pcard--on': selected.includes(p.id), 'pcard--muted': p.existing }"
          >
            <input
              type="checkbox"
              class="pcard__check"
              :value="p.id"
              :disabled="p.existing"
              v-model="selected"
            />
            <div class="pcard__main">
              <div class="pcard__title">
                <strong>{{ p.nameCn || '(待命名)' }}</strong>
                <code>{{ p.name }}</code>
                <span class="conf" :class="`conf--${p.confidence}`">{{ confLabel(p.confidence) }}</span>
                <span v-if="p.existing" class="tag tag--exists">已存在</span>
                <span v-if="p.similarTo" class="tag tag--similar">
                  相似 {{ p.similarTo.join(', ') }}
                </span>
                <span v-if="p.identityRisk" class="tag tag--risk">无编码列</span>
              </div>
              <div class="pcard__reason">{{ p.reason }}</div>
              <div class="pcard__meta">
                <span>{{ p.attributes.length }} 个属性</span>
                <span>跨 {{ p.tableCount }} 张表</span>
                <span v-if="p.keyField">主键 {{ p.keyField }}</span>
                <span v-if="p.missingAttributes?.length" class="pcard__missing">
                  现有类型缺 {{ p.missingAttributes.length }} 个属性：{{ p.missingAttributes.slice(0, 4).join(', ') }}
                </span>
              </div>
              <details class="pcard__attrs">
                <summary>看属性（{{ p.attributes.length }}）</summary>
                <table>
                  <tr v-for="a in p.attributes" :key="a.name">
                    <td><code>{{ a.name }}</code></td>
                    <td>{{ a.type }}</td>
                    <td class="pcard__role">{{ a.role }}</td>
                  </tr>
                </table>
              </details>
            </div>
          </label>
        </div>
      </section>

      <!-- 快照 -->
      <section class="disc-section">
        <div class="disc-section__head">
          <h3>快照 / 汇总候选（{{ result.snapshots.length }}）</h3>
          <div class="disc-section__ops">
            <button class="btn-link" @click="clearKind('snapshots')">清空</button>
          </div>
        </div>
        <p class="disc-section__hint">
          判定依据：有日期列 + 有度量 + 维度家族 ≤ 3。这类是"某天的汇总"，不是实体。
        </p>
        <div class="card-list card-list--compact">
          <label
            v-for="p in result.snapshots"
            :key="p.id"
            class="pcard pcard--compact"
            :class="{ 'pcard--on': selected.includes(p.id) }"
          >
            <input type="checkbox" class="pcard__check" :value="p.id" v-model="selected" />
            <div class="pcard__main">
              <div class="pcard__title">
                <code>{{ p.name }}</code>
                <span class="tag tag--layer">{{ p.layer }}</span>
                <span class="tag">{{ p.measureCount }} 度量</span>
                <span class="tag tag--date">date: {{ p.dateColumn }}</span>
              </div>
              <div class="pcard__reason">{{ p.reason }}</div>
            </div>
          </label>
        </div>
      </section>

      <!-- 关系 -->
      <section class="disc-section">
        <div class="disc-section__head">
          <h3>关系候选（{{ result.relations.length }}）</h3>
          <div class="disc-section__ops">
            <button class="btn-link btn-link--muted" @click="clearKind('relations')">清空</button>
          </div>
        </div>
        <p class="disc-section__hint">
          两个实体的列在 ≥3 张表里共同出现。关系只有在**两端都被采纳**时才会建。
        </p>
        <div class="card-list card-list--compact">
          <label
            v-for="p in result.relations"
            :key="p.id"
            class="pcard pcard--compact"
            :class="{ 'pcard--on': selected.includes(p.id) }"
          >
            <input type="checkbox" class="pcard__check" :value="p.id" v-model="selected" />
            <div class="pcard__main">
              <div class="pcard__title">
                <strong>{{ p.name }}</strong>
                <span class="conf" :class="`conf--${p.confidence}`">{{ confLabel(p.confidence) }}</span>
                <span class="tag">共现 {{ p.cooccurrence }} 表</span>
              </div>
              <div class="pcard__reason">{{ p.reason }}</div>
            </div>
          </label>
        </div>
      </section>
    </div>

    <!-- 采纳结果 -->
    <div v-if="adoptResult" class="disc-result">
      <div class="disc-result__head">
        <h3>已生成草稿版本 v{{ adoptResult.versionNumber }}</h3>
        <button class="btn-link" @click="adoptResult = null">关闭</button>
      </div>
      <p class="disc-result__note">
        复制了现有 {{ adoptResult.copiedFromActive }} 个对象类型，新增
        {{ adoptResult.created.length }} 个。
        <strong>还没有生效</strong>——去版本页看差异并审批，通过后才会真正创建。
      </p>
      <ul class="disc-result__list">
        <li v-for="c in adoptResult.created" :key="c.name">
          <strong>{{ c.nameCn }}</strong>
          <code>{{ c.name }}</code>
          <span>{{ c.attributeCount }} 属性</span>
          <span v-if="c.identityRisk" class="tag tag--risk">无编码列</span>
        </li>
      </ul>
      <ul v-if="adoptResult.skipped.length" class="disc-result__skip">
        <li v-for="s in adoptResult.skipped" :key="s.name">
          跳过 {{ s.name }}：{{ s.reason }}
        </li>
      </ul>
      <div class="disc-result__foot">
        <RouterLink class="btn-primary" to="/ontology/publish">去版本页看差异</RouterLink>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { discoveryApi, type AdoptResult, type DiscoveryProposal, type DiscoveryScanResult } from '../../api/discovery'

const result = ref<DiscoveryScanResult | null>(null)
const scanning = ref(false)
const adopting = ref(false)
const errorMsg = ref('')
const selected = ref<string[]>([])
const adoptResult = ref<AdoptResult | null>(null)

onMounted(() => doScan(false))

async function doScan(refresh: boolean) {
  scanning.value = true
  errorMsg.value = ''
  try {
    result.value = await discoveryApi.scan(refresh)
    // 默认勾上高置信、非已存在的实体候选——这是"直接能用"的那批
    selected.value = result.value.entities
      .filter(p => p.confidence === 'high' && !p.existing)
      .map(p => p.id)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || e?.message || '扫描失败'
  } finally {
    scanning.value = false
  }
}

function selectAll(kind: 'entities', highOnly: boolean) {
  if (!result.value) return
  const pool = result.value[kind].filter(p => !p.existing)
  if (highOnly) {
    selected.value = Array.from(new Set([
      ...selected.value,
      ...pool.filter(p => p.confidence === 'high').map(p => p.id),
    ]))
  } else {
    selected.value = pool.map(p => p.id)
  }
}

function clearKind(kind: 'entities' | 'snapshots' | 'relations') {
  if (!result.value) return
  const ids = new Set(result.value[kind].map(p => p.id))
  selected.value = selected.value.filter(id => !ids.has(id))
}

function confLabel(c: DiscoveryProposal['confidence']) {
  return ({ high: '高置信', medium: '中等', low: '低' } as Record<string, string>)[c] || c
}

async function doAdopt() {
  if (!selected.value.length) return
  adopting.value = true
  errorMsg.value = ''
  try {
    adoptResult.value = await discoveryApi.adopt(selected.value)
    selected.value = []
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || e?.message || '采纳失败'
  } finally {
    adopting.value = false
  }
}
</script>

<style scoped>
.disc-page { padding: 24px; height: 100%; overflow-y: auto; }
.disc-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.disc-title { font-size: 20px; font-weight: 700; margin: 0; color: var(--neutral-900); }
.disc-sub { font-size: 13px; color: var(--neutral-500); margin: 6px 0 0; max-width: 720px; line-height: 1.6; }
.disc-sub code { background: var(--neutral-100); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.disc-head__actions { display: flex; gap: 10px; flex-shrink: 0; }

.btn-primary { padding: 8px 18px; border-radius: 6px; border: none; background: var(--semantic-600); color: #fff; font-size: 13px; font-weight: 500; cursor: pointer; text-decoration: none; display: inline-block; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-ghost { padding: 8px 16px; border-radius: 6px; border: 1px solid var(--neutral-200); background: #fff; font-size: 13px; cursor: pointer; }
.btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-link { background: none; border: none; font-size: 12px; cursor: pointer; color: var(--semantic-600); }
.btn-link--muted { color: var(--neutral-400); }

.disc-error { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 14px; }

.disc-stats { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
.stat { display: flex; align-items: baseline; gap: 6px; background: #fff; border: 1px solid var(--neutral-100); border-radius: 8px; padding: 8px 14px; }
.stat__label { font-size: 11px; color: var(--neutral-500); }
.stat strong { font-size: 15px; color: var(--neutral-800); }
.stat--time strong { font-size: 12px; font-weight: 500; }

.disc-loading { padding: 40px; text-align: center; color: var(--neutral-500); font-size: 13px; }

.disc-body { display: flex; flex-direction: column; gap: 22px; }
.disc-section__head { display: flex; align-items: center; justify-content: space-between; }
.disc-section__head h3 { margin: 0; font-size: 14px; color: var(--neutral-800); }
.disc-section__ops { display: flex; gap: 12px; }
.disc-section__hint { font-size: 12px; color: var(--neutral-500); margin: 6px 0 10px; line-height: 1.6; }

.card-list { display: flex; flex-direction: column; gap: 8px; }
.pcard { display: flex; gap: 10px; background: #fff; border: 1px solid var(--neutral-100); border-radius: 9px; padding: 12px 14px; cursor: pointer; transition: border-color 0.15s, background 0.15s; }
.pcard:hover { border-color: var(--semantic-200); }
.pcard--on { border-color: var(--semantic-400); background: #f8fbff; }
.pcard--muted { opacity: 0.55; }
.pcard--compact { padding: 9px 12px; }
.pcard__check { margin-top: 3px; flex-shrink: 0; }
.pcard__main { flex: 1; min-width: 0; }
.pcard__title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; }
.pcard__title code { font-size: 11.5px; color: var(--neutral-500); background: var(--neutral-50); padding: 1px 5px; border-radius: 3px; }
.pcard__reason { font-size: 12px; color: var(--neutral-600); margin-top: 5px; line-height: 1.6; }
.pcard__meta { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--neutral-400); margin-top: 5px; }
.pcard__missing { color: #b45309; }
.pcard__attrs { margin-top: 8px; font-size: 12px; }
.pcard__attrs summary { cursor: pointer; color: var(--semantic-600); font-size: 11.5px; }
.pcard__attrs table { margin-top: 6px; border-collapse: collapse; }
.pcard__attrs td { padding: 2px 12px 2px 0; font-size: 11.5px; color: var(--neutral-600); }
.pcard__role { color: var(--neutral-400); }

.conf { font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 3px; }
.conf--high { background: #dcfce7; color: #166534; }
.conf--medium { background: #fef3c7; color: #92400e; }
.conf--low { background: var(--neutral-100); color: var(--neutral-500); }
.tag { font-size: 10px; padding: 1px 7px; border-radius: 3px; background: var(--neutral-100); color: var(--neutral-600); }
.tag--exists { background: #e0e7ff; color: #3730a3; }
.tag--similar { background: #fef3c7; color: #92400e; }
.tag--risk { background: #fee2e2; color: #991b1b; }
.tag--layer { background: #dbeafe; color: #1e40af; }
.tag--date { background: var(--neutral-50); color: var(--neutral-500); }

.disc-result { margin-top: 22px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 16px 18px; }
.disc-result__head { display: flex; align-items: center; justify-content: space-between; }
.disc-result__head h3 { margin: 0; font-size: 14px; color: #166534; }
.disc-result__note { font-size: 12.5px; color: #166534; margin: 8px 0 10px; line-height: 1.6; }
.disc-result__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.disc-result__list li { display: flex; align-items: center; gap: 10px; }
.disc-result__list code { font-size: 11px; color: var(--neutral-500); }
.disc-result__skip { list-style: none; margin: 10px 0 0; padding: 0; font-size: 12px; color: #92400e; }
.disc-result__foot { display: flex; justify-content: flex-end; margin-top: 14px; }
</style>
