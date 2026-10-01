<template>
  <section class="page" data-module="reconciliation">
    <header class="page-head">
      <div>
        <h2>告警对账台</h2>
        <p class="page-desc">
          风险面、处置通知、防汛事件同一水位口径；泵站离线不得放行。现场处置结论同事务回写
          排水台账、防汛清单与路段风险看板。重建按设施版本幂等，重算失败保留上一版风险面。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="mergeBlank">归并空设施号</button>
        <button class="btn primary" type="button" @click="rebuildAll">全量幂等重建</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :class="item.tone">{{ item.value }}</strong>
      </article>
    </div>

    <h3 class="block-title">路段风险看板（空间聚合）</h3>
    <table class="data-table road-table">
      <thead>
        <tr>
          <th>路段</th><th>最严水位阶段</th><th>通行口径</th>
          <th>泵站离线</th><th>设施数</th><th>在险数</th><th>风险等级</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="road in roads" :key="road.road" :class="boardTone(road.stage)">
          <td>{{ road.road }}</td>
          <td>{{ road.stage }}</td>
          <td><strong>{{ road.passable }}</strong></td>
          <td>{{ road.pump_offline ? '是' : '否' }}</td>
          <td>{{ road.facility_count }}</td>
          <td>{{ road.active_count }}</td>
          <td><span class="risk-tag" :class="`risk-${boardTone(road.stage)}`">{{ road.risk_level }}</span></td>
        </tr>
        <tr v-if="!roads.length"><td colspan="7" class="empty-state">暂无路段聚合数据</td></tr>
      </tbody>
    </table>

    <h3 class="block-title">设施对账明细（三处口径核对）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>设施编号</th><th>类型</th><th>所属路段</th><th>版本</th>
          <th>排水台账状态</th><th>根因</th>
          <th>风险面水位</th><th>风险面通行</th>
          <th>处置通知水位</th><th>看板通行</th>
          <th>泵站在线</th><th>对账</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.facility_id">
          <td>{{ row.facility_code || '（空号待归并）' }}</td>
          <td>{{ row.facility_type }}</td>
          <td>{{ row.road }}</td>
          <td>v{{ row.version }}</td>
          <td>{{ row.ledger_status }}</td>
          <td>{{ row.root_cause_text || '—' }}</td>
          <td>{{ row.surface_stage ?? '—' }}</td>
          <td :class="passableTone(row.surface_passable)">{{ row.surface_passable ?? '—' }}</td>
          <td>{{ row.notice_stage ?? '—' }}</td>
          <td :class="passableTone(row.board_passable)">{{ row.board_passable ?? '—' }}</td>
          <td>{{ row.pump_online === false ? '否' : '是' }}</td>
          <td>
            <span class="risk-tag" :class="isConsistent(row) ? 'risk-low' : 'risk-high'">
              {{ isConsistent(row) ? '一致' : '不一致' }}
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row.facility_id)">详情/复测</button>
            <button class="link" type="button" @click="rebuildOne(row.facility_id)">重建</button>
          </td>
        </tr>
        <tr v-if="!rows.length"><td colspan="13" class="empty-state">暂无对账数据</td></tr>
      </tbody>
    </table>

    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>{{ detail.facility.code }} · {{ detail.facility.facility_type }}</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </header>

        <p class="page-desc">
          {{ detail.facility.road }} {{ detail.facility.chainage }} ·
          台账状态 {{ detail.facility.status }} · 版本 v{{ detail.facility.version }} ·
          根因：{{ detail.root_cause_text || '—' }}
        </p>

        <h4 class="block-title">冻结水位（按采集时刻）</h4>
        <table class="data-table sub-table">
          <thead><tr><th>采集时刻</th><th>阶段</th><th>水位(cm)</th><th>泵站在线</th><th>来源</th></tr></thead>
          <tbody>
            <tr v-for="r in detail.readings" :key="r.id">
              <td>{{ r.collected_at }}</td><td>{{ r.stage }}</td><td>{{ r.water_cm }}</td>
              <td>{{ r.pump_online ? '是' : '否' }}</td>
              <td>{{ r.source === 'manual' ? '人工复测' : '传感器' }}</td>
            </tr>
          </tbody>
        </table>

        <h4 class="block-title">告警与处置通知（以最近人工复测为准）</h4>
        <table class="data-table sub-table">
          <thead><tr><th>时刻</th><th>阶段</th><th>来源</th><th>根因</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="a in detail.alerts" :key="a.id" :class="a.superseded ? 'superseded' : ''">
              <td>{{ a.occurred_at }}</td>
              <td>{{ a.stage }}</td>
              <td>{{ a.source === 'manual' ? '人工复测' : '传感器' }}</td>
              <td>{{ a.root_cause_text || '—' }}</td>
              <td>{{ a.superseded ? '已被复测取代' : (a.disposition_id ? `已处置 #${a.disposition_id}` : '待处置') }}</td>
            </tr>
          </tbody>
        </table>

        <h4 class="block-title">现场处置记录（并发只保留一个有效结论）</h4>
        <table class="data-table sub-table">
          <thead><tr><th>处置时刻</th><th>结果</th><th>复测阶段</th><th>处置人</th><th>有效性</th></tr></thead>
          <tbody>
            <tr v-for="d in detail.dispositions" :key="d.id">
              <td>{{ d.handled_at }}</td>
              <td>{{ d.result === 'cleared' ? '确认消退' : '确认险情' }}</td>
              <td>{{ d.measured_stage ?? '—' }}</td>
              <td>{{ d.operator || '—' }}</td>
              <td>{{ d.valid ? '有效' : `作废：${d.voided_reason}` }}</td>
            </tr>
            <tr v-if="!detail.dispositions.length"><td colspan="5" class="empty-state">暂无处置记录</td></tr>
          </tbody>
        </table>

        <h4 class="block-title">登记现场处置 / 人工复测</h4>
        <form class="filter-bar" @submit.prevent="submitDisposition">
          <label class="filter-item"><span>处置告警</span>
            <select v-model="dispForm.alert_id">
              <option v-for="a in pendingAlerts" :key="a.id" :value="a.id">
                #{{ a.id }} {{ a.occurred_at }} {{ a.stage }}
              </option>
            </select>
          </label>
          <label class="filter-item"><span>处置时刻</span>
            <input v-model="dispForm.handled_at" placeholder="2026-10-01T10:00:00" />
          </label>
          <label class="filter-item"><span>结论</span>
            <select v-model="dispForm.result">
              <option value="confirmed">确认险情</option>
              <option value="cleared">确认消退</option>
            </select>
          </label>
          <label class="filter-item"><span>复测水位阶段</span>
            <select v-model="dispForm.measured_stage">
              <option :value="null">不复测</option>
              <option v-for="s in stages" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label class="filter-item"><span>泵站在线</span>
            <select v-model="dispForm.pump_online"><option :value="true">在线</option><option :value="false">离线</option></select>
          </label>
          <label class="filter-item"><span>处置人</span><input v-model="dispForm.operator" /></label>
          <button class="btn primary" type="submit">提交并回写</button>
        </form>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ rows.length }} 座主档案设施 · {{ roads.length }} 个路段</span>
      <span v-if="message" :class="messageError ? 'error-text' : ''">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type DeskRow = Record<string, any>
type RoadRow = Record<string, any>

const stages = ['低水位', '警戒水位', '危险水位']
const rows = ref<DeskRow[]>([])
const roads = ref<RoadRow[]>([])
const detail = ref<any>(null)
const message = ref('')
const messageError = ref(false)

const dispForm = reactive({
  alert_id: null as number | null,
  handled_at: new Date().toISOString().slice(0, 19),
  result: 'cleared',
  measured_stage: '低水位' as string | null,
  pump_online: true,
  operator: '',
})

const stats = computed(() => {
  const danger = rows.value.filter(r => r.surface_stage === '危险水位' || r.pump_online === false).length
  const warn = rows.value.filter(r => r.surface_stage === '警戒水位' && r.pump_online !== false).length
  const blocked = roads.value.filter(r => r.passable === '禁止通行').length
  const inconsistent = rows.value.filter(r => !isConsistent(r)).length
  return [
    { label: '在险设施（危险/泵站离线）', value: danger, tone: 'tone-danger' },
    { label: '警戒设施', value: warn, tone: 'tone-warn' },
    { label: '禁行路段', value: blocked, tone: 'tone-danger' },
    { label: '口径不一致', value: inconsistent, tone: inconsistent ? 'tone-danger' : '' },
  ]
})

const pendingAlerts = computed(() => {
  const alerts = (detail.value?.alerts ?? []) as any[]
  // 可处置对象：未被处置核销、且未被更新的人工复测取代的告警
  return alerts.filter((a: any) => !a.disposition_id && !a.superseded)
})

function isConsistent(row: DeskRow): boolean {
  if (row.surface_stage == null) return true
  return row.surface_stage === row.notice_stage &&
    row.surface_passable === row.board_passable
}

function boardTone(stage: string): string {
  return stage === '危险水位' ? 'row-danger' : stage === '警戒水位' ? 'row-warn' : 'row-low'
}

function passableTone(passable: string | null): string {
  if (passable === '禁止通行') return 'cell-danger'
  if (passable === '减速缓行') return 'cell-warn'
  return ''
}

async function reload() {
  message.value = ''
  try {
    const [desk, board] = await Promise.all([
      request('/api/reconciliation/desk').then(r => r.json()),
      request('/api/reconciliation/roads').then(r => r.json()),
    ])
    rows.value = desk.rows ?? []
    roads.value = board.items ?? []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '对账台加载失败'
    messageError.value = true
  }
}

async function postJson(path: string, body: unknown) {
  const resp = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail ?? `接口返回 ${resp.status}`)
  return data
}

async function rebuildAll() {
  try {
    const data = await postJson('/api/reconciliation/rebuild', {})
    const failed = data.failed?.length ?? 0
    message.value = `全量重建完成：${data.results?.length ?? 0} 座` +
      (failed ? `，${failed} 座重算失败已保留上一版风险面` : '，全部口径一致')
    messageError.value = failed > 0
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '全量重建失败'
    messageError.value = true
  }
}

async function rebuildOne(id: number) {
  try {
    const data = await postJson('/api/reconciliation/rebuild', { facility_id: id })
    message.value = data.idempotent
      ? `设施 #${id} 版本 v${data.version} 已对账（幂等命中）`
      : `设施 #${id} 已按 v${data.version} 重算：${data.stage} / ${data.passable}`
    messageError.value = false
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '重建失败，已保留上一版风险面'
    messageError.value = true
  }
}

async function mergeBlank() {
  try {
    const data = await postJson('/api/reconciliation/merge-blank', {})
    message.value = `空设施号归并完成：${data.merged.length} 条残档已并入主档案`
    messageError.value = false
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '归并失败'
    messageError.value = true
  }
}

async function openDetail(id: number) {
  const resp = await request(`/api/reconciliation/facilities/${id}`)
  detail.value = await resp.json()
  const lastOpen = [...(detail.value.alerts ?? [])]
    .reverse()
    .find((a: any) => !a.disposition_id)
  dispForm.alert_id = lastOpen ? lastOpen.id : (detail.value.alerts.at(-1)?.id ?? null)
}

async function submitDisposition() {
  if (!detail.value || dispForm.alert_id == null) return
  try {
    const data = await postJson('/api/reconciliation/dispositions', {
      facility_id: detail.value.facility.id,
      alert_id: dispForm.alert_id,
      handled_at: dispForm.handled_at,
      result: dispForm.result,
      measured_stage: dispForm.measured_stage,
      pump_online: dispForm.pump_online,
      operator: dispForm.operator,
    })
    if (!data.accepted) {
      message.value = `处置结论作废：${data.voided_reason}`
      messageError.value = true
    } else if (data.rebuild_error) {
      message.value = `处置已登记，但风险面重算失败：${data.rebuild_error}（已保留上一版）`
      messageError.value = true
    } else {
      message.value = '处置已回写排水台账、防汛清单与路段风险看板'
      messageError.value = false
    }
    await openDetail(detail.value.facility.id)
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '处置提交失败'
    messageError.value = true
  }
}

onMounted(reload)
</script>

<style scoped>
.block-title { font-size: 14px; margin: 18px 0 8px; }
.tone-danger { color: #b42318; }
.tone-warn { color: #b54708; }
.cell-danger { color: #b42318; font-weight: 600; }
.cell-warn { color: #b54708; font-weight: 600; }
.row-danger td { background: #fef3f2; }
.row-warn td { background: #fffaeb; }
.risk-tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.risk-high { background: #fee4e2; color: #b42318; }
.risk-mid { background: #fef0c7; color: #b54708; }
.risk-low { background: #dcfae6; color: #027a48; }
.superseded { color: #98a2b3; text-decoration: line-through; }
.sub-table { margin-bottom: 12px; }
.drawer-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); z-index: 20;
  display: flex; justify-content: flex-end; }
.drawer { width: 760px; max-width: 92vw; background: #fff; height: 100%; overflow-y: auto;
  padding: 16px 20px; }
.drawer-head { display: flex; justify-content: space-between; align-items: center; }
.road-table { margin-bottom: 4px; }
</style>
