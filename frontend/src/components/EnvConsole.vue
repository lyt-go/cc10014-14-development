<template>
  <section class="env-console" data-testid="env-console">
    <header class="env-head">
      <h3>按施工区域判定与复工</h3>
      <p class="env-desc">
        同一位置只取最近两次有效测量判断持续超限；补录旧读数、缺测会显式标出，不能冒充最近一次测量。
      </p>
    </header>

    <form class="env-form" @submit.prevent="saveThreshold">
      <label class="env-field">
        <span>施工区域</span>
        <input v-model="thresholdForm.施工区域" data-testid="threshold-area" placeholder="如：东廊" />
      </label>
      <label class="env-field">
        <span>湿度阈值（%）</span>
        <input v-model="thresholdForm.湿度阈值" data-testid="threshold-value" inputmode="decimal" placeholder="如：85" />
      </label>
      <button class="btn primary" data-testid="threshold-save" type="submit">保存阈值</button>
      <span v-if="message" class="env-message" data-testid="env-message">{{ message }}</span>
    </form>

    <div class="env-grid">
      <article class="env-card">
        <h4>开工核实</h4>
        <form class="env-inline" @submit.prevent="refreshStatus">
          <input v-model="statusArea" data-testid="status-area" placeholder="按施工区域核实，如：东廊" />
          <button class="btn" data-testid="status-query" type="submit">核实</button>
        </form>
        <dl v-if="startStatus" class="env-facts" data-testid="start-status">
          <div><dt>施工区域</dt><dd>{{ startStatus.施工区域 || '—' }}</dd></div>
          <div>
            <dt>开工是否受阻</dt>
            <dd :class="startStatus.开工是否受阻 ? 'env-blocked' : 'env-ok'">
              {{ startStatus.开工是否受阻 ? '受阻' : '未受阻' }}
            </dd>
          </div>
          <div><dt>判定结果</dt><dd>{{ startStatus.判定结果 || '—' }}</dd></div>
          <div><dt>前一次 / 最近一次</dt>
            <dd>{{ startStatus.前一次读数 ?? '—' }}% / {{ startStatus.最近一次读数 ?? '—' }}%</dd></div>
          <div><dt>读数状态</dt><dd>{{ startStatus.读数状态 || '—' }}</dd></div>
          <div><dt>开工许可</dt>
            <dd>{{ startStatus.开工许可编号 || '无有效许可' }}（{{ startStatus.许可状态 || '—' }}）</dd></div>
          <div v-if="startStatus.阻断原因" class="env-wide">
            <dt>阻断原因</dt><dd>{{ startStatus.阻断原因 }}</dd>
          </div>
        </dl>
        <p v-else class="env-empty">输入施工区域后点击核实，查看是否受阻及许可状态。</p>
      </article>

      <article class="env-card">
        <h4>复工入口</h4>
        <p v-if="!resumeRows.length" class="env-empty">当前没有待复工工序。</p>
        <table v-else class="data-table env-table">
          <thead>
            <tr><th>工序</th><th>区域</th><th>读数</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in resumeRows" :key="row.id" :data-testid="`resume-${row.id}`">
              <td>{{ row.工序名称 }}</td>
              <td>{{ row.施工区域 }}</td>
              <td>
                {{ row.前一次读数 ?? '—' }}% / {{ row.最近一次读数 ?? '—' }}%
                <em class="env-tag">{{ row.读数状态 }}</em>
              </td>
              <td>{{ row.status }}</td>
              <td>
                <button class="link" type="button" :data-testid="`resume-confirm-${row.id}`"
                        @click="confirm(row)">确认复工</button>
              </td>
            </tr>
          </tbody>
        </table>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  confirmResume,
  fetchStartStatus,
  listResume,
  setThreshold,
  type ResumeEntry,
  type StartStatus,
} from '@/api/backend-service'

const message = ref('')
const thresholdForm = ref({ 施工区域: '', 湿度阈值: '' })
const statusArea = ref('')
const startStatus = ref<StartStatus | null>(null)
const resumeRows = ref<ResumeEntry[]>([])

async function saveThreshold() {
  message.value = ''
  try {
    const result = await setThreshold({
      施工区域: thresholdForm.value.施工区域,
      湿度阈值: thresholdForm.value.湿度阈值,
    })
    message.value = result.message
    if (result.threshold?.施工区域) statusArea.value = result.threshold.施工区域
    await refreshStatus()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '阈值设置失败'
  }
}

async function refreshStatus() {
  if (!statusArea.value.trim()) return
  try {
    startStatus.value = await fetchStartStatus(statusArea.value.trim())
    await refreshResume()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '开工核实失败'
  }
}

async function refreshResume() {
  try {
    const payload = await listResume(startStatus.value?.施工区域 || '')
    resumeRows.value = payload.items
  } catch {
    resumeRows.value = []
  }
}

async function confirm(row: ResumeEntry) {
  message.value = ''
  try {
    const result = await confirmResume(row.id)
    message.value = result.message
    await refreshStatus()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复工确认失败'
  }
}

onMounted(refreshResume)
</script>

<style scoped>
.env-console { margin-bottom: 24px; padding: 16px; border: 1px solid var(--border-color,#e2e8f0); border-radius: 8px; }
.env-head { display:flex; flex-direction:column; gap:4px; margin-bottom:12px; }
.env-desc { margin:0; font-size:13px; color:var(--text-secondary,#4a5568); }
.env-form, .env-inline { display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; }
.env-field { display:flex; flex-direction:column; gap:4px; font-size:13px; }
.env-field input, .env-inline input { padding:6px 8px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; }
.env-message { font-size:13px; color:#b45309; }
.env-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; margin-top:16px; }
.env-card { border:1px solid var(--border-color,#e2e8f0); border-radius:8px; padding:12px; }
.env-card h4 { margin:0 0 8px; }
.env-facts { margin:8px 0 0; display:grid; gap:6px; font-size:13px; }
.env-facts > div { display:flex; gap:8px; }
.env-facts dt { min-width:110px; color:var(--text-secondary,#4a5568); }
.env-facts dd { margin:0; }
.env-wide { grid-column:1 / -1; }
.env-blocked { color:#b91c1c; font-weight:600; }
.env-ok { color:#047857; font-weight:600; }
.env-empty { font-size:13px; color:var(--text-secondary,#4a5568); }
.env-tag { font-style:normal; font-size:12px; padding:1px 6px; border-radius:10px; background:#eef2ff; color:#4338ca; }
</style>
