<template>
  <div class="lg">
    <a-spin v-if="loading" />
    <a-empty v-else-if="!data || data.nodes.length === 0" description="暂无血缘数据" />
    <VueFlow
      v-else
      class="lg-flow"
      :nodes="flowNodes"
      :edges="flowEdges"
      :default-zoom="0.85"
      :min-zoom="0.3"
      :max-zoom="2"
      :nodes-draggable="true"
      :elements-selectable="true"
      fit-view-on-init
    >
      <Background pattern-color="#dee2e6" :gap="16" />
      <Controls />
      <MiniMap pannable zoomable />
    </VueFlow>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { VueFlow } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
import { Empty as AEmpty, Spin as ASpin } from 'ant-design-vue'
import type { LineageGraph } from '../../types/lineage'

const props = defineProps<{
  data: LineageGraph | null
  loading?: boolean
  labelResolver?: (kind: string, id: string) => string
}>()

const NODE_COLORS: Record<string, string> = {
  asset: '#3b82f6',
  object_type: '#10b981',
  action: '#a855f7',
  rule: '#f59e0b',
  // 字段级：指标偏冷、列偏中性、属性偏暖，和上面三类不撞色
  metric: '#0ea5e9',
  column: '#64748b',
  attribute: '#f97316',
}

function nodeKey(kind: string, id: string) { return `${kind}:${id}` }

const KIND_LABEL: Record<string, string> = {
  asset: '资产',
  object_type: '对象',
  action: '动作',
  rule: '规则',
  metric: '指标',
  column: '列',
  attribute: '属性',
}

function nodeLabel(kind: string, id: string) {
  if (props.labelResolver) return props.labelResolver(kind, id)
  const short = id.slice(0, 8)
  return `${KIND_LABEL[kind] || kind} ${short}`
}

const flowNodes = computed(() => {
  if (!props.data) return []
  // 分列布局。字段级链路是 指标→列→属性→对象，所以把对象列往后挪，
  // 让同一张图里"资产→对象"和"指标→列→属性→对象"两条线都能从左流到右。
  const colByKind: Record<string, number> = {
    asset: 0,
    metric: 0,
    column: 1,
    attribute: 2,
    object_type: 3,
    action: 4,
    rule: 4,
  }
  const groups: Record<string, string[]> = {}
  for (const n of props.data.nodes) {
    (groups[n.kind] ||= []).push(n.id)
  }
  return props.data.nodes.map((n) => {
    const col = colByKind[n.kind] ?? 0
    const idx = (groups[n.kind] || []).indexOf(n.id)
    return {
      id: nodeKey(n.kind, n.id),
      data: { kind: n.kind, originalId: n.id },
      // 字段级节点多（几百个），纵向间距收紧，否则画布高得没法看
      position: { x: col * 280, y: idx * (props.data.nodes.length > 120 ? 46 : 90) },
      label: nodeLabel(n.kind, n.id),
      style: {
        background: NODE_COLORS[n.kind] || '#999',
        color: '#fff',
        padding: '8px 12px',
        borderRadius: '8px',
        border: 'none',
        fontSize: '12px',
        width: 220,
      },
    }
  })
})

const flowEdges = computed(() => {
  if (!props.data) return []
  return props.data.edges.map((e, i) => ({
    id: `e-${i}-${e.source.id.slice(0, 6)}-${e.target.id.slice(0, 6)}`,
    source: nodeKey(e.source.kind, e.source.id),
    target: nodeKey(e.target.kind, e.target.id),
    label: `${e.relation}${e.weight > 1 ? ' ×' + e.weight : ''}`,
    animated: e.relation === 'reads' || e.relation === 'writes',
    style: { stroke: e.weight === 0 ? '#9ca3af' : '#6b7280', strokeWidth: 1.5 },
    labelStyle: { fontSize: '10px', fill: '#374151' },
    type: 'smoothstep',
  }))
})
</script>

<style scoped>
.lg { width: 100%; height: 100%; min-height: 480px; }
.lg-flow { width: 100%; height: 100%; }
</style>
