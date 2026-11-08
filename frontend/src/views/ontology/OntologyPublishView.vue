<template>
  <div class="publish-page">
    <div class="publish-page__header">
      <h1 class="page-title">本体发布</h1>
      <p class="page-desc">版本化管理本体发布，支持一致性校验、审批流程和版本回滚</p>
    </div>

    <div class="publish-page__body">
      <!-- 左侧：版本时间线 -->
      <div class="version-sidebar">
        <button class="btn-primary btn-block" @click="showCreateDialog = true">创建新版本</button>
        <div class="version-list">
          <div
            v-for="v in versions"
            :key="v.id"
            class="version-item"
            :class="{ 'version-item--active': selectedVersionId === v.id }"
            @click="selectVersion(v.id)"
          >
            <div class="version-item__badge" :class="`version-item__badge--${v.status}`">
              v{{ v.version_number }}
            </div>
            <div class="version-item__info">
              <span class="version-item__name">{{ v.name }}</span>
              <span class="version-item__meta">
                {{ v.entity_count }} 个实体 · {{ formatStatus(v.status) }}
              </span>
            </div>
            <span v-if="v.is_active" class="version-item__active">当前</span>
          </div>
          <div v-if="!versions.length" class="version-empty">暂无版本</div>
        </div>
      </div>

      <!-- 右侧：版本详情 -->
      <div class="version-main">
        <template v-if="detail">
          <!-- 版本元信息 -->
          <div class="meta-card">
            <div class="meta-card__header">
              <h2>v{{ detail.version_number }} — {{ detail.name }}</h2>
              <span class="status-tag" :class="`status-tag--${detail.status}`">{{ formatStatus(detail.status) }}</span>
            </div>
            <p v-if="detail.description" class="meta-card__desc">{{ detail.description }}</p>
            <div class="meta-card__timeline">
              <span v-if="detail.created_at">创建: {{ formatTime(detail.created_at) }}</span>
              <span v-if="detail.submitted_at">提交: {{ formatTime(detail.submitted_at) }}</span>
              <span v-if="detail.published_at">发布: {{ formatTime(detail.published_at) }}</span>
              <span v-if="detail.rejected_at">驳回: {{ formatTime(detail.rejected_at) }}</span>
            </div>
            <div v-if="detail.reject_reason" class="meta-card__reject">
              驳回原因: {{ detail.reject_reason }}
            </div>
            <div v-if="detail.rollback_from" class="meta-card__rollback">
              回滚自 v{{ detail.rollback_from }}
            </div>
          </div>

          <!-- 改了什么（结构 diff） -->
          <div class="diff-card">
            <div class="diff-card__header">
              <h3>
                改了什么
                <span v-if="diffData && !diffData.noBaseline" class="diff-card__count">
                  {{ diffData.changes.length }} 项变更
                  <span v-if="diffData.breakingCount" class="diff-card__breaking">
                    · {{ diffData.breakingCount }} 项破坏性
                  </span>
                </span>
              </h3>
              <div class="diff-card__actions">
                <span v-if="diffData?.baseline" class="diff-card__baseline">
                  对比基线 v{{ diffData.baseline.number }}（{{ diffData.baseline.name }}）
                </span>
                <button class="btn-sm" :disabled="diffLoading" @click="loadDiff">刷新</button>
              </div>
            </div>
            <div v-if="diffLoading" class="diff-card__empty">分析中...</div>
            <div v-else-if="!diffData" class="diff-card__empty">未能加载差异</div>
            <div v-else-if="diffData.noBaseline" class="diff-card__empty">
              {{ diffData.note || '没有可比基线' }}
            </div>
            <div v-else-if="!diffData.changes.length" class="diff-card__empty">
              与基线版本完全一致，没有结构变更
            </div>
            <ul v-else class="diff-list">
              <li v-for="(c, i) in diffData.changes" :key="i" :class="changeClass(c)">
                <span class="chg__scope">{{ scopeLabel(c.scope) }}</span>
                <span class="chg__text">{{ c.label }}</span>
                <span v-if="c.breaking" class="chg__badge">破坏性</span>
              </li>
            </ul>
          </div>

          <!-- 实体列表 -->
          <div class="entity-card">
            <div class="entity-card__header">
              <h3>包含实体 ({{ detail.entities.length }})</h3>
              <button v-if="detail.status === 'draft'" class="btn-sm" @click="showEntitySelector = true">添加实体</button>
            </div>
            <div class="entity-table" v-if="detail.entities.length">
              <div class="entity-table__row entity-table__row--header">
                <span class="col-name">实体名称</span>
                <span class="col-tier">层级</span>
                <span class="col-attrs">属性</span>
                <span class="col-status">就绪</span>
                <span class="col-action" v-if="detail.status === 'draft'">操作</span>
              </div>
              <template v-for="e in detail.entities" :key="e.id">
                <div class="entity-table__row entity-table__row--clickable" @click="toggleEntity(e.id)">
                  <span class="col-name">
                    <span class="entity-caret">{{ expandedEntityId === e.id ? '▾' : '▸' }}</span>
                    <strong>{{ e.name_cn }}</strong>
                    <small>{{ e.name }}</small>
                  </span>
                  <span class="col-tier">T{{ e.tier }}</span>
                  <span class="col-attrs">{{ e.mapped_count }}/{{ e.attribute_count }}</span>
                  <span class="col-status">
                    <span v-if="e.readiness.ready" class="check-pass">✓</span>
                    <span v-else class="check-fail">✗</span>
                  </span>
                  <span class="col-action" v-if="detail.status === 'draft'" @click.stop>
                    <button class="btn-link btn-link--danger" @click="removeEntity(e.source_entity_id)">移除</button>
                  </span>
                </div>

                <!-- 属性明细：草稿状态下可直接编辑 -->
                <div v-if="expandedEntityId === e.id" class="attr-panel">
                  <div class="attr-panel__bar">
                    <span>共 {{ e.attributes.length }} 个属性</span>
                    <span v-if="isDraft" class="attr-panel__hint">草稿可直接编辑，改动会进入 diff</span>
                    <span v-else class="attr-panel__hint attr-panel__hint--frozen">
                      已提交/已发布，属性冻结（要改请新建草稿）
                    </span>
                  </div>
                  <table class="attr-table">
                    <thead>
                      <tr>
                        <th>属性名</th>
                        <th>类型</th>
                        <th>必填</th>
                        <th>来源字段</th>
                        <th v-if="isDraft" class="attr-table__ops">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="a in e.attributes" :key="a.id">
                        <td>
                          <code>{{ a.name }}</code>
                          <span v-if="!a.source_attribute_id" class="attr-new-tag">新增</span>
                        </td>
                        <td>
                          <select
                            v-if="isDraft"
                            v-model="attrDraft[a.id].type"
                            @focus="editAttr(a)"
                            class="attr-input"
                          >
                            <option v-for="t in ['string','number','decimal','integer','boolean','date','datetime','json']" :key="t" :value="t">{{ t }}</option>
                          </select>
                          <span v-else>{{ a.type }}</span>
                        </td>
                        <td>
                          <input
                            v-if="isDraft"
                            type="checkbox"
                            v-model="attrDraft[a.id].required"
                            @change="editAttr(a)"
                          />
                          <span v-else>{{ a.required ? '是' : '' }}</span>
                        </td>
                        <td>
                          <input
                            v-if="isDraft"
                            v-model="attrDraft[a.id].source_field"
                            class="attr-input attr-input--wide"
                            placeholder="(未映射)"
                            @focus="editAttr(a)"
                          />
                          <span v-else>{{ a.source_field || '—' }}</span>
                        </td>
                        <td v-if="isDraft" class="attr-table__ops">
                          <button
                            class="btn-link"
                            :disabled="!attrChanged(a) || attrSaving === a.id"
                            @click="saveAttr(e, a)"
                          >保存</button>
                          <button
                            v-if="attrChanged(a)"
                            class="btn-link btn-link--muted"
                            @click="cancelAttr(a.id)"
                          >撤销</button>
                          <button class="btn-link btn-link--danger" @click="removeAttr(e, a)">删除</button>
                        </td>
                      </tr>
                    </tbody>
                  </table>

                  <div v-if="isDraft" class="attr-add">
                    <template v-if="showAddAttr[e.id]">
                      <input v-model="newAttr.name" class="attr-input" placeholder="属性名" />
                      <select v-model="newAttr.type" class="attr-input">
                        <option v-for="t in ['string','number','decimal','integer','boolean','date','datetime','json']" :key="t" :value="t">{{ t }}</option>
                      </select>
                      <label class="attr-add__check">
                        <input type="checkbox" v-model="newAttr.required" /> 必填
                      </label>
                      <input v-model="newAttr.source_field" class="attr-input attr-input--wide" placeholder="来源字段（可空）" />
                      <button class="btn-sm" :disabled="!newAttr.name.trim() || attrSaving === e.id" @click="addAttr(e)">添加</button>
                      <button class="btn-link btn-link--muted" @click="showAddAttr[e.id] = false">取消</button>
                    </template>
                    <button v-else class="btn-link" @click="showAddAttr[e.id] = true">+ 新增属性</button>
                  </div>
                </div>
              </template>
            </div>
            <div v-else class="entity-empty">暂未添加实体，点击上方按钮选择</div>
          </div>

          <!-- 一致性检查 -->
          <div class="check-card" v-if="checkResult">
            <h3>一致性检查</h3>
            <div class="check-card__summary" :class="checkResult.passed ? 'check-card__summary--pass' : 'check-card__summary--fail'">
              {{ checkResult.summary }}
            </div>
            <div v-if="checkResult.entity_issues.length" class="check-card__issues">
              <div v-for="ei in checkResult.entity_issues" :key="ei.entity_id" class="issue-item">
                <strong>{{ ei.entity_name }}:</strong> {{ ei.issues.join('; ') }}
              </div>
            </div>
            <div v-if="checkResult.relation_issues.length" class="check-card__issues">
              <div v-for="ri in checkResult.relation_issues" :key="ri.relation_name" class="issue-item issue-item--rel">
                {{ ri.issue }}
              </div>
            </div>
          </div>

          <!-- 操作按钮 -->
          <div class="action-bar">
            <template v-if="detail.status === 'draft'">
              <button class="btn-secondary" @click="runCheck" :disabled="loading">一致性检查</button>
              <button class="btn-primary" @click="submitForApproval" :disabled="loading || !detail.entities.length">提交审批</button>
              <button class="btn-danger-outline" @click="deleteVersion">删除草稿</button>
            </template>
            <template v-else-if="detail.status === 'pending_approval'">
              <button class="btn-primary" @click="approveVersion" :disabled="loading">审批通过</button>
              <button class="btn-danger-outline" @click="showRejectDialog = true">驳回</button>
            </template>
            <template v-else-if="detail.status === 'published'">
              <button class="btn-secondary" @click="rollbackVersion" :disabled="loading">回滚到此版本</button>
            </template>
            <button
              v-if="detail.status === 'draft' || detail.status === 'pending_approval'"
              class="btn-outline"
              @click="previewImpact"
            >
              预览影响
            </button>
          </div>
        </template>
        <div v-else class="version-empty-main">
          <p>选择左侧版本查看详情，或创建新版本</p>
        </div>
      </div>
    </div>

    <!-- 创建版本弹窗 -->
    <div v-if="showCreateDialog" class="modal-overlay" @click.self="showCreateDialog = false">
      <div class="modal-box">
        <h3>创建新版本</h3>
        <div class="form-group">
          <label>版本名称</label>
          <input v-model="newVersion.name" placeholder="例如：携号转网实体集" />
        </div>
        <div class="form-group">
          <label>描述（可选）</label>
          <textarea v-model="newVersion.description" rows="3" placeholder="本次发布的说明"></textarea>
        </div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showCreateDialog = false">取消</button>
          <button class="btn-primary" @click="createVersion" :disabled="!newVersion.name.trim()">创建</button>
        </div>
      </div>
    </div>

    <!-- 实体选择弹窗 -->
    <div v-if="showEntitySelector" class="modal-overlay" @click.self="showEntitySelector = false">
      <div class="modal-box modal-box--wide">
        <h3>选择实体</h3>
        <div class="selector-list">
          <label v-for="e in availableEntities" :key="e.id" class="selector-item">
            <input type="checkbox" v-model="selectedEntityIds" :value="e.id" />
            <span class="selector-item__name">{{ e.name_cn }}</span>
            <span class="selector-item__en">{{ e.name }}</span>
            <span class="selector-item__tier">T{{ e.tier }}</span>
          </label>
          <div v-if="!availableEntities.length" class="entity-empty">所有实体已添加</div>
        </div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showEntitySelector = false">取消</button>
          <button class="btn-primary" @click="addEntities" :disabled="!selectedEntityIds.length">添加选中 ({{ selectedEntityIds.length }})</button>
        </div>
      </div>
    </div>

    <!-- 驳回弹窗 -->
    <div v-if="showRejectDialog" class="modal-overlay" @click.self="showRejectDialog = false">
      <div class="modal-box">
        <h3>驳回版本</h3>
        <div class="form-group">
          <label>驳回原因</label>
          <textarea v-model="rejectReason" rows="3" placeholder="请填写驳回原因"></textarea>
        </div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showRejectDialog = false">取消</button>
          <button class="btn-danger" @click="rejectVersion" :disabled="!rejectReason.trim()">确认驳回</button>
        </div>
      </div>
    </div>

    <!-- Impact Preview -->
    <div v-if="showImpact" class="impact-overlay">
      <div class="impact-dialog">
        <div class="impact-dialog__header">
          <h3>发布影响预览</h3>
          <button class="impact-dialog__close" @click="showImpact = false">&times;</button>
        </div>
        <div class="impact-dialog__body">
          <div v-if="impactLoading" class="impact-dialog__loading">分析中...</div>
          <div v-else-if="!impactData" class="impact-dialog__empty">无法加载影响面</div>
          <div v-else-if="impactData.noBaseline" class="impact-dialog__empty">
            {{ impactData.note || '该版本已是生效版本，没有可比基线' }}
          </div>
          <template v-else>
            <!-- 改了什么 -->
            <div v-if="!impactData.changes.length" class="impact-dialog__empty">
              无结构变更，可安全发布
            </div>
            <template v-else>
              <h4>
                结构变更 ({{ impactData.changes.length }})
                <span v-if="impactData.breakingCount" class="impact-dialog__warn">
                  · {{ impactData.breakingCount }} 项破坏性
                </span>
              </h4>
              <ul class="impact-dialog__list">
                <li v-for="(c, i) in impactData.changes" :key="i" :class="changeClass(c)">
                  <span class="chg__scope">{{ scopeLabel(c.scope) }}</span>
                  {{ c.label }}
                  <span v-if="c.breaking" class="chg__badge">破坏性</span>
                </li>
              </ul>

              <!-- 影响谁：按依赖类型分组 -->
              <template v-if="dependentGroups.length">
                <h4>受影响的下游 ({{ dependentGroups.reduce((n, g) => n + g.items.length, 0) }})</h4>
                <div v-for="g in dependentGroups" :key="g.key" class="dep-group">
                  <div class="dep-group__title">
                    {{ g.label }}
                    <span class="dep-group__count">{{ g.items.length }}</span>
                    <span class="dep-group__hint">{{ g.hint }}</span>
                  </div>
                  <ul class="dep-group__list">
                    <li v-for="(it, i) in g.items.slice(0, 8)" :key="i">
                      <strong>{{ it.name || it.id }}</strong>
                      <small v-if="it.reason">{{ it.reason }}</small>
                    </li>
                  </ul>
                  <div v-if="g.items.length > 8" class="dep-group__more">
                    还有 {{ g.items.length - 8 }} 个未显示
                  </div>
                </div>
              </template>
              <div v-else class="impact-dialog__empty">没有下游依赖受影响</div>
            </template>
          </template>
        </div>
      </div>
    </div>

    <!-- 审批前确认：审批会把快照写回实际对象 -->
    <div v-if="showApplyPreview" class="impact-overlay">
      <div class="impact-dialog">
        <div class="impact-dialog__header">
          <h3>审批通过前确认</h3>
          <button class="impact-dialog__close" @click="showApplyPreview = false">&times;</button>
        </div>
        <div class="impact-dialog__body">
          <div v-if="applyPreviewLoading" class="impact-dialog__loading">分析中...</div>
          <template v-else-if="applyPreviewData">
            <div class="apply-warn">
              通过后会把该版本快照<strong>写回实际对象</strong>，不只是改状态。
            </div>
            <h4>将改动的实际数据</h4>
            <ul class="apply-stats">
              <li>
                <span>实体更新 / 新建</span>
                <strong>{{ applyPreviewData.willUpdate?.entities || 0 }} / {{ applyPreviewData.willUpdate?.entitiesCreated || 0 }}</strong>
              </li>
              <li>
                <span>属性变更</span>
                <strong>{{ applyPreviewData.willUpdate?.attributes || 0 }}</strong>
              </li>
              <li>
                <span>关系变更</span>
                <strong>{{ applyPreviewData.willUpdate?.relations || 0 }}</strong>
              </li>
              <li>
                <span>破坏性变更</span>
                <strong :class="{ 'apply-stats__bad': applyPreviewData.breakingCount }">
                  {{ applyPreviewData.breakingCount || 0 }}
                </strong>
              </li>
            </ul>

            <ul v-if="applyPreviewData.changes?.length" class="impact-dialog__list">
              <li v-for="(c, i) in applyPreviewData.changes" :key="i" :class="changeClass(c)">
                <span class="chg__scope">{{ scopeLabel(c.scope) }}</span>
                {{ c.label }}
                <span v-if="c.breaking" class="chg__badge">破坏性</span>
              </li>
            </ul>
            <div v-else class="impact-dialog__empty">与当前生效版本无差异</div>

            <div class="apply-foot">
              <button class="btn-sm" @click="showApplyPreview = false">取消</button>
              <button class="btn-primary" :disabled="approving" @click="confirmApprove">
                {{ approving ? '生效中...' : '确认通过并生效' }}
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 审批回执：实际改了多少 -->
    <div v-if="appliedReport" class="impact-overlay">
      <div class="impact-dialog impact-dialog--narrow">
        <div class="impact-dialog__header">
          <h3>已生效</h3>
          <button class="impact-dialog__close" @click="appliedReport = null">&times;</button>
        </div>
        <div class="impact-dialog__body">
          <ul class="apply-stats">
            <li><span>实体更新</span><strong>{{ appliedReport.entitiesUpdated }}</strong></li>
            <li><span>实体新建</span><strong>{{ appliedReport.entitiesCreated }}</strong></li>
            <li><span>属性更新</span><strong>{{ appliedReport.attributesUpdated }}</strong></li>
            <li><span>属性新建</span><strong>{{ appliedReport.attributesCreated }}</strong></li>
            <li><span>关系更新</span><strong>{{ appliedReport.relationsUpdated }}</strong></li>
          </ul>
          <div v-if="appliedReport.extras?.attributes?.length" class="apply-note">
            实际表里有 {{ appliedReport.extras.attributes.length }} 个属性不在快照中，
            <strong>未自动删除</strong>（删除会打断指标绑定和血缘，需人工确认）。
          </div>
          <div v-for="(w, i) in (appliedReport.warnings || [])" :key="i" class="apply-note">
            {{ w }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { get, post, put, del } from '../../api/client'
import { ontologyPublishApi } from '../../api/aip'

interface VersionSummary {
  id: string; version_number: number; name: string; description: string | null
  status: string; entity_count: number; created_by: string | null
  created_at: string | null; submitted_at: string | null; published_at: string | null
  is_active: boolean; rollback_from: number | null
}

interface Readiness { has_datasource: boolean; mapped_attrs: number; total_attrs: number; mapping_ratio: number; issues: string[]; ready: boolean }

interface VersionAttribute {
  id: string
  source_attribute_id: string | null
  name: string
  type: string
  description: string
  required: boolean
  example: string | null
  source_table: string | null
  source_field: string | null
  data_status: string
}

interface VersionEntity {
  id: string; source_entity_id: string; name: string; name_cn: string
  tier: number; description: string | null; attribute_count: number; mapped_count: number
  readiness: Readiness
  attributes: VersionAttribute[]
}

interface VersionDetail extends VersionSummary {
  rejected_at: string | null; reject_reason: string | null; approved_by: string | null
  entities: VersionEntity[]; relations: any[]
}

/** 一条结构变更（后端 Change.to_dict() + breaking/label） */
interface VersionChange {
  scope: 'entity' | 'attribute' | 'relation'
  action: string
  entity: string
  entityId?: string
  target?: string
  field?: string
  before?: unknown
  after?: unknown
  breaking: boolean
  label: string
}

interface DiffResult {
  version: { id: string; number: number; name: string; status: string }
  baseline: { id: string; number: number; name: string } | null
  changes: VersionChange[]
  breakingCount: number
  noBaseline: boolean
  note: string | null
  dependents: Record<string, any[]>
  summary?: Record<string, number>
}

interface CheckResult { passed: boolean; entity_issues: any[]; relation_issues: any[]; summary: string }
interface EntityItem { id: string; name: string; name_cn: string; tier: number }

const versions = ref<VersionSummary[]>([])
const selectedVersionId = ref<string | null>(null)
const detail = ref<VersionDetail | null>(null)
const checkResult = ref<CheckResult | null>(null)
const loading = ref(false)

const showCreateDialog = ref(false)
const showEntitySelector = ref(false)
const showRejectDialog = ref(false)
const showImpact = ref(false)
const impactData = ref<DiffResult | null>(null)
const impactLoading = ref(false)

// 结构 diff（选中版本时自动加载）
const diffData = ref<DiffResult | null>(null)
const diffLoading = ref(false)

// 属性编辑
const expandedEntityId = ref<string | null>(null)
const attrDraft = ref<Record<string, { type?: string; required?: boolean; source_field?: string }>>({})
const attrSaving = ref<string | null>(null)
const showAddAttr = ref<Record<string, boolean>>({})
const newAttr = reactive({ name: '', type: 'string', required: false, source_field: '' })

// 审批前预览
const showApplyPreview = ref(false)
const applyPreviewData = ref<any>(null)
const applyPreviewLoading = ref(false)
const approving = ref(false)

const newVersion = reactive({ name: '', description: '' })
const rejectReason = ref('')
const selectedEntityIds = ref<string[]>([])
const allEntities = ref<EntityItem[]>([])

const availableEntities = computed(() => {
  if (!detail.value) return allEntities.value
  const inVersion = new Set(detail.value.entities.map(e => e.source_entity_id))
  return allEntities.value.filter(e => !inVersion.has(e.id))
})

/** 只有草稿能编辑——已提交/已发布的版本必须冻结，否则审计不可信 */
const isDraft = computed(() => detail.value?.status === 'draft')

/** 影响面分组：后端返回的 dependents 是 {rules: [...], actions: [...]}，转成可渲染列表 */
const dependentGroups = computed(() => {
  const d = (impactData.value?.dependents || diffData.value?.dependents || {}) as Record<string, any[]>
  const meta: { key: string; label: string; hint: string }[] = [
    { key: 'rules', label: '规则', hint: '条件引用了被改的属性' },
    { key: 'actions', label: '动作', hint: '挂在该对象类型上' },
    { key: 'functions', label: '函数', hint: '挂在该对象类型上' },
    { key: 'metricBindings', label: '指标绑定', hint: '该类型的指标口径' },
    { key: 'constraints', label: '语义约束', hint: '相关口径约束会失效' },
    { key: 'lineage', label: '血缘', hint: '上下游血缘边' },
    { key: 'scenes', label: '场景', hint: '引用了该实体' },
    { key: 'agents', label: '智能体', hint: '引用了该实体' },
  ]
  return meta
    .map(m => ({ ...m, items: d[m.key] || [] }))
    .filter(g => g.items.length > 0)
})

onMounted(async () => {
  await loadVersions()
  await loadAllEntities()
})

async function loadVersions() {
  versions.value = await get('/ontology-publish/versions')
}

async function loadAllEntities() {
  const list = await get('/entities')
  allEntities.value = (list.items || list).map((e: any) => ({ id: e.id, name: e.name, name_cn: e.name_cn, tier: e.tier }))
}

async function selectVersion(id: string) {
  selectedVersionId.value = id
  checkResult.value = null
  diffData.value = null
  expandedEntityId.value = null
  detail.value = await get(`/ontology-publish/versions/${id}`)
  // 必须给每个属性预建草稿对象：
  // 模板里是 v-model="attrDraft[a.id].type"，如果 attrDraft[a.id] 是 undefined，
  // Vue 渲染时对 undefined 取属性会直接抛错，整页白屏。
  const drafts: Record<string, { type?: string; required?: boolean; source_field?: string }> = {}
  for (const e of detail.value?.entities || []) {
    for (const a of e.attributes || []) {
      drafts[a.id] = {
        type: a.type,
        required: a.required,
        source_field: a.source_field || '',
      }
    }
  }
  attrDraft.value = drafts
  await loadDiff()
}

/** 加载结构 diff。生效版本自己跟自己比没有基线，后端会返回 noBaseline */
async function loadDiff() {
  if (!detail.value) return
  diffLoading.value = true
  try {
    diffData.value = await ontologyPublishApi.diff(detail.value.id)
  } catch {
    diffData.value = null
  } finally {
    diffLoading.value = false
  }
}

async function createVersion() {
  const v = await post('/ontology-publish/versions', { name: newVersion.name, description: newVersion.description || null })
  showCreateDialog.value = false
  newVersion.name = ''
  newVersion.description = ''
  await loadVersions()
  await selectVersion(v.id)
}

async function deleteVersion() {
  if (!detail.value) return
  await del(`/ontology-publish/versions/${detail.value.id}`)
  detail.value = null
  selectedVersionId.value = null
  await loadVersions()
}

async function addEntities() {
  if (!detail.value) return
  loading.value = true
  await post(`/ontology-publish/versions/${detail.value.id}/entities`, { entity_ids: selectedEntityIds.value })
  selectedEntityIds.value = []
  showEntitySelector.value = false
  await selectVersion(detail.value.id)
  await loadVersions()
  loading.value = false
}

async function removeEntity(entityId: string) {
  if (!detail.value) return
  await del(`/ontology-publish/versions/${detail.value.id}/entities/${entityId}`)
  await selectVersion(detail.value.id)
  await loadVersions()
}

async function runCheck() {
  if (!detail.value) return
  loading.value = true
  checkResult.value = await get(`/ontology-publish/versions/${detail.value.id}/check`)
  loading.value = false
}

async function submitForApproval() {
  if (!detail.value) return
  loading.value = true
  try {
    await post(`/ontology-publish/versions/${detail.value.id}/submit`)
    await loadVersions()
    await selectVersion(detail.value.id)
  } catch (e: any) {
    checkResult.value = { passed: false, entity_issues: [], relation_issues: [], summary: e?.response?.data?.detail || '提交失败' }
  }
  loading.value = false
}

/** 审批前先看"点通过会改什么、影响谁"——这是版本系统的安全阀 */
async function openApplyPreview() {
  if (!detail.value) return
  showApplyPreview.value = true
  applyPreviewLoading.value = true
  applyPreviewData.value = null
  try {
    applyPreviewData.value = await ontologyPublishApi.applyPreview(detail.value.id)
  } finally {
    applyPreviewLoading.value = false
  }
}

/** 预览确认后才真正审批：审批会把快照写回实际对象 */
async function confirmApprove() {
  if (!detail.value) return
  approving.value = true
  try {
    const res = await post<any>(`/ontology-publish/versions/${detail.value.id}/approve`)
    showApplyPreview.value = false
    appliedReport.value = res?.applied || null
    await loadVersions()
    await selectVersion(detail.value.id)
  } finally {
    approving.value = false
  }
}

// 上一次审批实际改了多少东西（给用户一个回执）
const appliedReport = ref<any>(null)

async function approveVersion() {
  // 改为先预览再确认，不再直接审批
  await openApplyPreview()
}

async function rejectVersion() {
  if (!detail.value) return
  await post(`/ontology-publish/versions/${detail.value.id}/reject`, { reason: rejectReason.value })
  rejectReason.value = ''
  showRejectDialog.value = false
  await loadVersions()
  await selectVersion(detail.value.id)
}

async function rollbackVersion() {
  if (!detail.value) return
  loading.value = true
  const v = await post(`/ontology-publish/versions/${detail.value.id}/rollback`)
  await loadVersions()
  await selectVersion(v.id)
  loading.value = false
}

async function previewImpact() {
  if (!detail.value) return
  showImpact.value = true
  impactLoading.value = true
  try {
    impactData.value = await ontologyPublishApi.previewImpact(detail.value.id)
  } catch {
    impactData.value = null
  } finally {
    impactLoading.value = false
  }
}

// ── 草稿属性编辑 ────────────────────────────────────────────
// 没有这一块的话，"改一个属性"在版本里无法表达，
// diff / 影响面也就永远看不到属性级变更。

function toggleEntity(veId: string) {
  expandedEntityId.value = expandedEntityId.value === veId ? null : veId
}

/** 把属性当前值填进草稿，用户改哪个字段就提交哪个 */
function editAttr(attr: VersionAttribute) {
  attrDraft.value[attr.id] = {
    type: attr.type,
    required: attr.required,
    source_field: attr.source_field || '',
  }
}

function attrChanged(attr: VersionAttribute) {
  const d = attrDraft.value[attr.id]
  if (!d) return false
  return (
    (d.type ?? attr.type) !== attr.type ||
    (d.required ?? attr.required) !== attr.required ||
    (d.source_field ?? (attr.source_field || '')) !== (attr.source_field || '')
  )
}

async function saveAttr(ve: VersionEntity, attr: VersionAttribute) {
  if (!detail.value) return
  const d = attrDraft.value[attr.id]
  if (!d) return
  const body: Record<string, unknown> = {}
  if (d.type !== undefined && d.type !== attr.type) body.type = d.type
  if (d.required !== undefined && d.required !== attr.required) body.required = d.required
  const sf = d.source_field ?? ''
  if (sf !== (attr.source_field || '')) body.source_field = sf || null
  if (Object.keys(body).length === 0) {
    delete attrDraft.value[attr.id]
    return
  }
  attrSaving.value = attr.id
  try {
    await ontologyPublishApi.updateAttribute(detail.value.id, ve.id, attr.id, body)
    await selectVersion(detail.value.id)
  } finally {
    attrSaving.value = null
  }
}

function cancelAttr(attrId: string) {
  delete attrDraft.value[attrId]
}

async function removeAttr(ve: VersionEntity, attr: VersionAttribute) {
  if (!detail.value) return
  attrSaving.value = attr.id
  try {
    await ontologyPublishApi.removeAttribute(detail.value.id, ve.id, attr.id)
    await selectVersion(detail.value.id)
  } finally {
    attrSaving.value = null
  }
}

async function addAttr(ve: VersionEntity) {
  if (!detail.value || !newAttr.name.trim()) return
  attrSaving.value = ve.id
  try {
    await ontologyPublishApi.addAttribute(detail.value.id, ve.id, {
      name: newAttr.name.trim(),
      type: newAttr.type || 'string',
      required: newAttr.required,
      source_field: newAttr.source_field || null,
    })
    newAttr.name = ''
    newAttr.type = 'string'
    newAttr.required = false
    newAttr.source_field = ''
    showAddAttr.value[ve.id] = false
    await selectVersion(detail.value.id)
  } catch (e: any) {
    checkResult.value = {
      passed: false, entity_issues: [], relation_issues: [],
      summary: e?.response?.data?.detail || '新增属性失败',
    }
  } finally {
    attrSaving.value = null
  }
}

/** 变更的展示色：破坏性红、其余蓝 */
function changeClass(c: VersionChange) {
  return c.breaking ? 'chg chg--breaking' : 'chg'
}

function scopeLabel(scope: string) {
  return ({ entity: '实体', attribute: '属性', relation: '关系' } as Record<string, string>)[scope] || scope
}

function formatStatus(status: string) {
  const map: Record<string, string> = { draft: '草稿', pending_approval: '待审批', published: '已发布', rejected: '已驳回' }
  return map[status] || status
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.publish-page { padding: 24px; height: 100%; display: flex; flex-direction: column; }
.publish-page__header { margin-bottom: 20px; }
.page-title { font-size: 20px; font-weight: 700; color: var(--neutral-900); margin: 0; }
.page-desc { font-size: 13px; color: var(--neutral-500); margin: 4px 0 0; }
.publish-page__body { display: flex; gap: 20px; flex: 1; min-height: 0; }

.version-sidebar { width: 280px; flex-shrink: 0; display: flex; flex-direction: column; gap: 12px; }
.version-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.version-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 8px; cursor: pointer; border: 1px solid var(--neutral-100); transition: all 0.15s; }
.version-item:hover { background: var(--neutral-50); }
.version-item--active { background: var(--semantic-50); border-color: var(--semantic-200); }
.version-item__badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px; background: var(--neutral-100); color: var(--neutral-600); }
.version-item__badge--published { background: #dcfce7; color: #166534; }
.version-item__badge--pending_approval { background: #fef3c7; color: #92400e; }
.version-item__badge--rejected { background: #fee2e2; color: #991b1b; }
.version-item__badge--draft { background: #e0e7ff; color: #3730a3; }
.version-item__info { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.version-item__name { font-size: 13px; font-weight: 500; color: var(--neutral-800); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.version-item__meta { font-size: 11px; color: var(--neutral-500); }
.version-item__active { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 3px; background: #dcfce7; color: #166534; }
.version-empty { font-size: 13px; color: var(--neutral-400); text-align: center; padding: 20px; }

.version-main { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
.version-empty-main { display: flex; align-items: center; justify-content: center; height: 100%; color: var(--neutral-400); }

.meta-card { background: var(--neutral-50); border-radius: 10px; padding: 16px; }
.meta-card__header { display: flex; align-items: center; gap: 10px; }
.meta-card__header h2 { font-size: 16px; font-weight: 600; margin: 0; color: var(--neutral-900); }
.meta-card__desc { font-size: 13px; color: var(--neutral-600); margin: 8px 0 0; }
.meta-card__timeline { display: flex; gap: 16px; margin-top: 10px; font-size: 11px; color: var(--neutral-500); }
.meta-card__reject { margin-top: 8px; padding: 8px 10px; background: #fef2f2; border-radius: 6px; font-size: 12px; color: #dc2626; }
.meta-card__rollback { margin-top: 6px; font-size: 11px; color: var(--neutral-500); font-style: italic; }

.status-tag { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; }
.status-tag--draft { background: #e0e7ff; color: #3730a3; }
.status-tag--pending_approval { background: #fef3c7; color: #92400e; }
.status-tag--published { background: #dcfce7; color: #166534; }
.status-tag--rejected { background: #fee2e2; color: #991b1b; }

.entity-card { background: #fff; border: 1px solid var(--neutral-100); border-radius: 10px; padding: 16px; }
.entity-card__header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.entity-card__header h3 { font-size: 14px; font-weight: 600; margin: 0; }
.entity-table__row { display: flex; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--neutral-50); font-size: 13px; }
.entity-table__row--header { font-size: 11px; font-weight: 600; color: var(--neutral-500); text-transform: uppercase; }
.col-name { flex: 2; display: flex; flex-direction: column; gap: 1px; }
.col-name small { font-size: 11px; color: var(--neutral-400); }
.col-tier { width: 50px; text-align: center; }
.col-attrs { width: 70px; text-align: center; }
.col-status { width: 50px; text-align: center; }
.col-action { width: 60px; text-align: center; }
.check-pass { color: #16a34a; font-weight: 700; }
.check-fail { color: #dc2626; font-weight: 700; }
.entity-empty { font-size: 13px; color: var(--neutral-400); text-align: center; padding: 20px; }

.check-card { background: #fff; border: 1px solid var(--neutral-100); border-radius: 10px; padding: 16px; }
.check-card h3 { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
.check-card__summary { font-size: 13px; font-weight: 600; padding: 8px 12px; border-radius: 6px; }
.check-card__summary--pass { background: #dcfce7; color: #166534; }
.check-card__summary--fail { background: #fef2f2; color: #dc2626; }
.check-card__issues { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.issue-item { font-size: 12px; color: #dc2626; padding: 4px 8px; background: #fef2f2; border-radius: 4px; }
.issue-item--rel { color: #92400e; background: #fef3c7; }

.action-bar { display: flex; gap: 10px; align-items: center; padding-top: 8px; }

.btn-primary { padding: 8px 18px; border-radius: 6px; border: none; background: var(--semantic-600); color: #fff; font-size: 13px; font-weight: 500; cursor: pointer; }
.btn-primary:hover:not(:disabled) { background: var(--semantic-700); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { padding: 8px 18px; border-radius: 6px; border: 1px solid var(--neutral-200); background: #fff; color: var(--neutral-700); font-size: 13px; font-weight: 500; cursor: pointer; }
.btn-secondary:hover { background: var(--neutral-50); }
.btn-danger { padding: 8px 18px; border-radius: 6px; border: none; background: #dc2626; color: #fff; font-size: 13px; font-weight: 500; cursor: pointer; }
.btn-danger:hover:not(:disabled) { background: #b91c1c; }
.btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-danger-outline { padding: 8px 18px; border-radius: 6px; border: 1px solid #fca5a5; background: #fef2f2; color: #dc2626; font-size: 13px; font-weight: 500; cursor: pointer; }
.btn-danger-outline:hover { background: #fee2e2; }
.btn-sm { padding: 4px 12px; border-radius: 5px; border: 1px solid var(--neutral-200); background: #fff; font-size: 12px; cursor: pointer; }
.btn-sm:hover { background: var(--neutral-50); }
.btn-block { width: 100%; }
.btn-link { background: none; border: none; font-size: 12px; cursor: pointer; color: var(--semantic-600); }
.btn-link--danger { color: #dc2626; }

.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-box { background: #fff; border-radius: 12px; padding: 24px; width: 400px; max-height: 80vh; overflow-y: auto; }
.modal-box--wide { width: 520px; }
.modal-box h3 { font-size: 16px; font-weight: 600; margin: 0 0 16px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.form-group { margin-bottom: 12px; }
.form-group label { display: block; font-size: 12px; font-weight: 600; color: var(--neutral-600); margin-bottom: 4px; }
.form-group input, .form-group textarea { width: 100%; padding: 8px 10px; border: 1px solid var(--neutral-200); border-radius: 6px; font-size: 13px; resize: vertical; }

.selector-list { max-height: 300px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.selector-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.selector-item:hover { background: var(--neutral-50); }
.selector-item input { margin: 0; }
.selector-item__name { font-weight: 500; }
.selector-item__en { color: var(--neutral-400); font-size: 11px; }
.selector-item__tier { margin-left: auto; font-size: 11px; color: var(--neutral-500); }

.btn-outline { padding: 8px 18px; border-radius: 6px; border: 1px solid var(--neutral-300); background: #fff; color: var(--neutral-700); font-size: 13px; font-weight: 500; cursor: pointer; }
.btn-outline:hover { background: var(--neutral-50); }

.impact-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.impact-dialog {
  background: #fff;
  border-radius: 8px;
  width: 480px;
  max-height: 70vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
}
.impact-dialog__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #e5e7eb;
}
.impact-dialog__header h3 { margin: 0; font-size: 16px; }
.impact-dialog__close {
  background: none;
  border: none;
  font-size: 20px;
  cursor: pointer;
  color: #6b7280;
}
.impact-dialog__body { padding: 16px 20px; }
.impact-dialog__body h4 { margin: 12px 0 6px; font-size: 13px; color: #374151; }
.impact-dialog__list { margin: 0; padding-left: 16px; font-size: 13px; }
.impact-dialog__list li { margin: 4px 0; }
.impact-dialog__loading { text-align: center; color: #6b7280; padding: 20px; }
.impact-dialog__empty { text-align: center; color: #059669; padding: 20px; }
.impact-dialog__tag--del { color: #dc2626; }
.impact-dialog__tag--ren { color: #d97706; }

/* ── 结构 diff 卡片 ─────────────────────────────────────── */
.diff-card { background: #fff; border: 1px solid var(--neutral-100); border-radius: 10px; padding: 14px 16px; }
.diff-card__header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.diff-card__header h3 { margin: 0; font-size: 14px; color: var(--neutral-800); display: flex; align-items: center; gap: 8px; }
.diff-card__count { font-size: 12px; font-weight: 400; color: var(--neutral-500); }
.diff-card__breaking { color: #dc2626; font-weight: 600; }
.diff-card__actions { display: flex; align-items: center; gap: 10px; }
.diff-card__baseline { font-size: 11px; color: var(--neutral-500); }
.diff-card__empty { padding: 14px 4px; font-size: 12px; color: var(--neutral-500); }
.diff-list { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.diff-list .chg { display: flex; align-items: center; gap: 8px; font-size: 12.5px; padding: 6px 9px; border-radius: 6px; background: var(--neutral-50); border-left: 3px solid var(--semantic-300); }
.diff-list .chg--breaking { background: #fef2f2; border-left-color: #dc2626; }
.chg__scope { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 3px; background: #e0e7ff; color: #3730a3; flex-shrink: 0; }
.chg__text { flex: 1; min-width: 0; }
.chg__badge { font-size: 10px; font-weight: 700; color: #dc2626; border: 1px solid #fca5a5; border-radius: 3px; padding: 0 5px; }

/* ── 属性明细面板 ───────────────────────────────────────── */
.entity-table__row--clickable { cursor: pointer; }
.entity-table__row--clickable:hover { background: var(--neutral-50); }
.entity-caret { display: inline-block; width: 12px; color: var(--neutral-400); font-size: 10px; }
.attr-panel { background: #fafafa; border-top: 1px dashed var(--neutral-200); border-bottom: 1px solid var(--neutral-100); padding: 10px 16px 14px; }
.attr-panel__bar { display: flex; align-items: center; gap: 12px; font-size: 11.5px; color: var(--neutral-500); margin-bottom: 8px; }
.attr-panel__hint { color: var(--semantic-600); }
.attr-panel__hint--frozen { color: var(--neutral-400); }
.attr-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.attr-table th { text-align: left; font-weight: 600; color: var(--neutral-500); padding: 5px 8px; border-bottom: 1px solid var(--neutral-200); font-size: 11px; }
.attr-table td { padding: 5px 8px; border-bottom: 1px solid var(--neutral-100); vertical-align: middle; }
.attr-table__ops { text-align: right; white-space: nowrap; }
.attr-table code { font-size: 11.5px; background: #fff; border: 1px solid var(--neutral-200); border-radius: 3px; padding: 1px 5px; }
.attr-new-tag { font-size: 10px; color: #059669; margin-left: 6px; }
.attr-input { font-size: 12px; padding: 2px 6px; border: 1px solid var(--neutral-200); border-radius: 4px; background: #fff; max-width: 110px; }
.attr-input--wide { max-width: 180px; }
.attr-add { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.attr-add__check { font-size: 11.5px; color: var(--neutral-600); display: flex; align-items: center; gap: 4px; }
.btn-link--muted { color: var(--neutral-400); }
.btn-link:disabled { color: var(--neutral-300); cursor: not-allowed; }

/* ── 影响面弹窗：依赖分组 ───────────────────────────────── */
.impact-dialog { width: min(760px, 92vw); max-height: 82vh; display: flex; flex-direction: column; }
.impact-dialog--narrow { width: min(460px, 92vw); }
.impact-dialog__body { overflow-y: auto; }
.impact-dialog__warn { color: #dc2626; font-weight: 600; }
.dep-group { margin: 10px 0 4px; padding: 9px 11px; background: var(--neutral-50); border-radius: 7px; }
.dep-group__title { font-size: 12.5px; font-weight: 600; color: var(--neutral-800); display: flex; align-items: center; gap: 8px; }
.dep-group__count { font-size: 11px; background: #fff; border: 1px solid var(--neutral-200); border-radius: 10px; padding: 0 7px; }
.dep-group__hint { font-size: 11px; font-weight: 400; color: var(--neutral-400); }
.dep-group__list { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.dep-group__list li { font-size: 12px; color: var(--neutral-700); }
.dep-group__list small { color: var(--neutral-400); margin-left: 6px; }
.dep-group__more { font-size: 11px; color: var(--neutral-400); margin-top: 4px; }

/* ── 审批确认 ───────────────────────────────────────────── */
.apply-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; border-radius: 7px; padding: 9px 12px; font-size: 12.5px; }
.apply-stats { list-style: none; margin: 8px 0 12px; padding: 0; display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px 16px; }
.apply-stats li { display: flex; justify-content: space-between; font-size: 12.5px; padding: 5px 9px; background: var(--neutral-50); border-radius: 5px; }
.apply-stats__bad { color: #dc2626; }
.apply-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.apply-note { font-size: 12px; color: #92400e; background: #fffbeb; border-radius: 6px; padding: 8px 10px; margin-top: 8px; }
</style>
