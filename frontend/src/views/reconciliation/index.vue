<template>
  <section class="page" data-module="reconciliation">
    <header class="page-head">
      <div>
        <h2>告警对账台</h2>
        <p class="page-desc">
          风险面、处置通知、防汛事件三源对账：泵站离线即不可通行；同一泵站多次告警以最近一次人工复测为准，
          历史水位按采集时刻冻结；现场处置同事务回写排水台账、防汛清单与路段风险看板。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="loadAll">刷新对账</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in cards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <h3>待处置告警（对账工作队列）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>告警</th><th>设施</th><th>路段</th><th>类型</th><th>告警时刻</th>
          <th>告警水位阶段</th><th>当前风险面</th><th>处置</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="alarm in pendingAlarms" :key="alarm.id">
          <td>#{{ alarm.id }}</td>
          <td>{{ alarm.设施编号 }}</td>
          <td>{{ alarm.所属路段 }}</td>
          <td>{{ alarm.alarm_type }}</td>
          <td>{{ alarm.alarm_time }}</td>
          <td>{{ alarm.stage }}</td>
          <td>
            <template v-for="fac in facilities" :key="fac.id">
              <span v-if="fac.id === alarm.facility_id">
                {{ fac.风险阶段 }} ·
                <span :class="passClass(fac.可通行性)">{{ fac.可通行性 }}</span>
              </span>
            </template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openRetest(alarm)">人工复测</button>
            <button class="link" type="button" @click="recover(alarm)">现场恢复</button>
          </td>
        </tr>
        <tr v-if="!pendingAlarms.length">
          <td colspan="8" class="empty-state">没有待处置告警，三源已全部对齐</td>
        </tr>
      </tbody>
    </table>

    <div v-if="retestAlarm" class="retest-box">
      <strong>告警 #{{ retestAlarm.id }} 人工复测</strong>
      <label><span>复测水位（厘米）</span>
        <input v-model="retestLevel" type="number" step="0.1" placeholder="如 18" />
      </label>
      <label><span>复测人</span><input v-model="retestOperator" placeholder="如 张三" /></label>
      <label><span>复测时刻</span><input v-model="retestAt" placeholder="YYYY-MM-DD HH:MM:SS" /></label>
      <button class="btn primary" type="button" @click="submitRetest">提交复测结论</button>
      <button class="btn ghost" type="button" @click="retestAlarm = null">取消</button>
    </div>

    <h3>设施主档案与当前风险面</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>设施编号</th><th>类型</th><th>路段/桩号</th><th>版本</th>
          <th>风险阶段</th><th>可通行性</th><th>泵站离线</th><th>对账状态</th>
          <th>根因（回灌排水列表/详情）</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="fac in facilities" :key="fac.id">
          <td>
            <button class="link" type="button" @click="showDetail(fac.id)">{{ fac.设施编号 }}</button>
          </td>
          <td>{{ fac.设施类型 }}</td>
          <td>{{ fac.所属路段 }} {{ fac.桩号位置 }}</td>
          <td>v{{ fac.version }}</td>
          <td>{{ fac.风险阶段 ?? '—' }}</td>
          <td :class="passClass(fac.可通行性)">{{ fac.可通行性 ?? '—' }}</td>
          <td>{{ fac.泵站离线 ? '离线' : '在线' }}</td>
          <td :class="fac.对账状态 === '已对齐' ? '' : 'error-text'">{{ fac.对账状态 }}</td>
          <td class="cause-cell">{{ fac.根因 ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="rebuild(fac.id)">重建风险面</button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="detail-box">
      <header>
        <strong>{{ detail.设施编号 }}（{{ detail.设施类型 }} · {{ detail.所属路段 }}）主档案详情</strong>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <p class="page-desc">{{ detail.根因 }}</p>
      <h4>历史水位（采集时刻冻结，共 {{ detail.readings.length }} 条）</h4>
      <table class="data-table sub-table">
        <thead><tr><th>采集时刻</th><th>水位(cm)</th><th>阶段</th><th>来源</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="r in detail.readings" :key="r.id">
            <td>{{ r.collected_at }}</td><td>{{ r.level_cm }}</td><td>{{ r.stage }}</td>
            <td>{{ r.source }}</td><td>{{ r.note ?? '' }}</td>
          </tr>
        </tbody>
      </table>
      <h4>告警（{{ detail.alarms.length }}）</h4>
      <table class="data-table sub-table">
        <thead><tr><th>#</th><th>类型</th><th>时刻</th><th>状态</th><th>有效结论</th></tr></thead>
        <tbody>
          <tr v-for="a in detail.alarms" :key="a.id">
            <td>{{ a.id }}</td><td>{{ a.alarm_type }}</td><td>{{ a.alarm_time }}</td>
            <td>{{ a.status }}</td><td>{{ a.conclusion ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <h4>重建任务</h4>
      <table class="data-table sub-table">
        <thead><tr><th>#</th><th>设施版本</th><th>状态</th><th>说明</th><th>创建时间</th></tr></thead>
        <tbody>
          <tr v-for="t in detail.tasks" :key="t.id">
            <td>{{ t.id }}</td><td>v{{ t.facility_version }}</td><td>{{ t.status }}</td>
            <td>{{ t.detail }}</td><td>{{ t.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <h3>路段风险看板（空间聚合，与风险面同事务）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>路段</th><th>风险阶段</th><th>可通行性</th><th>泵站离线数</th>
          <th>受影响设施</th><th>纳入设施</th><th>根因摘要</th><th>更新时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in boardRows" :key="row.id">
          <td>{{ row.路段 }}</td>
          <td>{{ row.风险阶段 }}</td>
          <td :class="passClass(row.可通行性)">{{ row.可通行性 }}</td>
          <td>{{ row.泵站离线数 }}</td>
          <td>{{ row.受影响设施数 }}</td>
          <td>{{ row.纳入设施数 }}</td>
          <td class="cause-cell">{{ row.根因摘要 }}</td>
          <td>{{ row.更新时间 }}</td>
        </tr>
      </tbody>
    </table>

    <h3>迁移与任务台账</h3>
    <div class="migrate-box">
      <label><span>设施记录（JSON；空设施号按类型+路段+桩号归并主档案）</span>
        <textarea v-model="migrateText" rows="5"></textarea>
      </label>
      <button class="btn primary" type="button" @click="runMigrate">执行迁移</button>
      <span v-if="migrateResult" class="migrate-result">{{ migrateResult }}</span>
    </div>
    <table class="data-table">
      <thead><tr><th>#</th><th>设施</th><th>设施版本</th><th>状态</th><th>说明</th><th>创建时间</th></tr></thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.id">
          <td>{{ t.id }}</td>
          <td>{{ facilityCode(t.facility_id) }}</td>
          <td>v{{ t.facility_version }}</td>
          <td :class="t.status === '失败' ? 'error-text' : ''">{{ t.status }}</td>
          <td>{{ t.detail }}</td>
          <td>{{ t.created_at }}</td>
        </tr>
        <tr v-if="!tasks.length"><td colspan="6" class="empty-state">暂无重建任务</td></tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>同一设施版本重建幂等；重算失败保留上一版风险面；并发处置只保留一个有效结论。</span>
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Json = Record<string, any>

const cards = ref<Json[]>([])
const facilities = ref<Json[]>([])
const pendingAlarms = ref<Json[]>([])
const boardRows = ref<Json[]>([])
const tasks = ref<Json[]>([])
const detail = ref<Json | null>(null)
const message = ref('')
const messageOk = ref(true)

const retestAlarm = ref<Json | null>(null)
const retestLevel = ref('')
const retestOperator = ref('')
const retestAt = ref('')
const migrateText = ref(JSON.stringify([
  { 设施编号: '', 设施类型: '泵站', 所属路段: '河埠路', 桩号位置: 'K4+020', 临时编号: 'TMP-示例' },
], null, 2))
const migrateResult = ref('')

function passClass(passability?: string) {
  if (passability === '不可通行') return 'pass-blocked'
  if (passability === '限速通行') return 'pass-limited'
  return ''
}

function facilityCode(facilityId: number) {
  return facilities.value.find((f) => f.id === facilityId)?.设施编号 ?? `#${facilityId}`
}

function notify(ok: boolean, text: string) {
  messageOk.value = ok
  message.value = text
}

async function post(path: string, body: unknown) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.detail ?? payload.message ?? '操作未生效')
  }
  return payload
}

async function loadAll() {
  try {
    const [overview, alarms, board, taskRows] = await Promise.all([
      fetch('/api/reconciliation/overview').then((r) => r.json()),
      fetch('/api/reconciliation/alarms?status=待处置').then((r) => r.json()),
      fetch('/api/reconciliation/board').then((r) => r.json()),
      fetch('/api/reconciliation/tasks').then((r) => r.json()),
    ])
    cards.value = overview.cards ?? []
    facilities.value = overview.facilities ?? []
    pendingAlarms.value = alarms.items ?? []
    boardRows.value = board.items ?? []
    tasks.value = taskRows.items ?? []
    if (detail.value) await showDetail(detail.value.id)
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '对账台数据加载失败')
  }
}

function openRetest(alarm: Json) {
  retestAlarm.value = alarm
  retestLevel.value = ''
  retestOperator.value = ''
  retestAt.value = ''
}

async function submitRetest() {
  if (!retestAlarm.value) return
  try {
    const payload = await post(`/api/reconciliation/alarms/${retestAlarm.value.id}/handle`, {
      values: {
        action: '人工复测',
        measured_level: Number(retestLevel.value),
        operator: retestOperator.value,
        measured_at: retestAt.value,
      },
    })
    retestAlarm.value = null
    notify(true, payload.message)
    await loadAll()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '复测结论提交失败')
  }
}

async function recover(alarm: Json) {
  try {
    const payload = await post(`/api/reconciliation/alarms/${alarm.id}/handle`, {
      values: { action: '现场恢复', measured_at: '' },
    })
    notify(true, payload.message)
    await loadAll()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '现场恢复失败')
  }
}

async function rebuild(facilityId: number) {
  try {
    const payload = await post(`/api/reconciliation/facilities/${facilityId}/rebuild`, { values: {} })
    notify(true, payload.message)
    await loadAll()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '重建失败')
  }
}

async function runMigrate() {
  try {
    const items = JSON.parse(migrateText.value)
    const payload = await post('/api/reconciliation/migrate', { values: { items } })
    migrateResult.value = payload.message
    notify(true, payload.message)
    await loadAll()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '迁移失败')
  }
}

async function showDetail(facilityId: number) {
  try {
    const response = await request(`/api/reconciliation/facilities/${facilityId}`)
    detail.value = await response.json()
  } catch (error) {
    notify(false, error instanceof Error ? error.message : '设施详情加载失败')
  }
}

onMounted(loadAll)
</script>

<style scoped>
h3 { margin: 18px 0 8px; font-size: 15px; }
h4 { margin: 10px 0 4px; font-size: 13px; }
.pass-blocked { color: #b42318; font-weight: 600; }
.pass-limited { color: #b54708; font-weight: 600; }
.cause-cell { max-width: 320px; color: #475467; }
.retest-box,
.detail-box,
.migrate-box {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
}
.retest-box label,
.migrate-box label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.retest-box input { width: 180px; }
.detail-box { display: block; }
.detail-box header { display: flex; justify-content: space-between; align-items: center; }
.sub-table { margin-bottom: 8px; }
.migrate-box textarea { width: 520px; font-family: monospace; font-size: 12px; }
.migrate-result { color: #067647; font-size: 13px; }
</style>
