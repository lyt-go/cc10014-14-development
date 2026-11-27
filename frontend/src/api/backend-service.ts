import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 环境阈值、判定与复工 ──

export type EnvThreshold = {
  id: number
  施工区域: string
  湿度阈值: number
  status: string
  [key: string]: unknown
}

export type EnvJudgement = {
  id: number
  监测位置: string
  判定结果: string
  湿度阈值: number | null
  前一次读数: number | null
  最近一次读数: number | null
  是否报警: boolean
  读数状态: string
  补录读数: string
  缺测记录: string
  判定说明: string
  判定时间: string
  [key: string]: unknown
}

export type ResumeEntry = {
  id: number
  关联工序ID: number
  工序名称: string
  施工区域: string
  停工原因: string
  判定结果: string
  前一次读数: number | null
  最近一次读数: number | null
  读数状态: string
  状态说明: string
  status: string
}

export type WorkPermit = {
  id: number
  许可编号: string
  工序名称: string
  施工区域: string
  许可类型: string
  许可状态: string
  核实结果: string
  签发时间: string
  status: string
  [key: string]: unknown
}

export type StartStatus = {
  施工区域: string
  工序: string | null
  开工是否受阻: boolean
  阻断原因: string
  判定结果: string | null
  湿度阈值: number | null
  前一次读数: number | null
  最近一次读数: number | null
  读数状态: string
  当前判定: EnvJudgement | null
  开工许可: WorkPermit | null
  开工许可编号: string | null
  许可状态: string | null
  已受理工序: Record<string, unknown>[]
  需暂停工序: Record<string, unknown>[]
  复工清单: ResumeEntry[]
  可以复工: boolean
}

export async function listThresholds(area = ''): Promise<{ items: EnvThreshold[]; total: number }> {
  const query = area ? `?area=${encodeURIComponent(area)}` : ''
  const response = await request(`/api/env-monitor/thresholds${query}`)
  if (!response.ok) throw new Error('湿度阈值读取失败')
  return await response.json()
}

export async function setThreshold(
  values: Record<string, unknown>,
): Promise<{ ok: boolean; message: string; threshold?: EnvThreshold; judgement?: EnvJudgement }> {
  const response = await request('/api/env-monitor/thresholds', {
    method: 'POST',
    body: JSON.stringify(values),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail || '湿度阈值设置失败')
  }
  return await response.json()
}

export async function listJudgements(area = ''): Promise<{ items: EnvJudgement[]; total: number }> {
  const query = area ? `?area=${encodeURIComponent(area)}` : ''
  const response = await request(`/api/env-monitor/judgements${query}`)
  if (!response.ok) throw new Error('环境判定读取失败')
  return await response.json()
}

export async function judgeArea(area: string): Promise<{ ok: boolean; message: string; judgement: EnvJudgement }> {
  const response = await request('/api/env-monitor/judge', {
    method: 'POST',
    body: JSON.stringify({ area }),
  })
  if (!response.ok) throw new Error('重新判定失败')
  return await response.json()
}

export async function fetchStartStatus(area = '', process = ''): Promise<StartStatus> {
  const params = new URLSearchParams()
  if (area) params.set('area', area)
  if (process) params.set('process', process)
  const response = await request(`/api/env-monitor/start-status?${params.toString()}`)
  if (!response.ok) throw new Error('开工状态核实失败')
  return await response.json()
}

export async function listResume(area = ''): Promise<{ items: ResumeEntry[]; total: number }> {
  const query = area ? `?area=${encodeURIComponent(area)}` : ''
  const response = await request(`/api/env-monitor/resume${query}`)
  if (!response.ok) throw new Error('复工清单读取失败')
  return await response.json()
}

export async function confirmResume(id: number): Promise<{ ok: boolean; message: string; entry?: ResumeEntry }> {
  const response = await request(`/api/env-monitor/resume/${id}/confirm`, { method: 'POST' })
  return await response.json()
}

export async function listPermits(area = ''): Promise<{ items: WorkPermit[]; total: number }> {
  const query = area ? `?area=${encodeURIComponent(area)}` : ''
  const response = await request(`/api/env-monitor/permits${query}`)
  if (!response.ok) throw new Error('开工许可读取失败')
  return await response.json()
}

export type DependentStatus = {
  module: string
  entry_id: number
  entry: Record<string, unknown>
  upstream: { module: string; id: number; name: string; status: string }[]
  downstream: { module: string; id: number; name: string; status: string }[]
}

export async function fetchDependentStatus(
  moduleKey: string,
  entryId: number,
): Promise<DependentStatus> {
  const response = await request(`/api/cross-module/dependent-status/${moduleKey}/${entryId}`)
  if (!response.ok) throw new Error('跨模块状态查询失败')
  return await response.json()
}

export async function fetchDependencyGraph(): Promise<{ nodes: unknown[]; edges: unknown[] }> {
  const response = await request('/api/cross-module/dependency-graph')
  if (!response.ok) throw new Error('依赖图加载失败')
  return await response.json()
}
