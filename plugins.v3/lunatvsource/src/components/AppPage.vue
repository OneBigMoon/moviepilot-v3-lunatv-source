<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'LunaTVSource' },
  navKey: { type: String, default: 'main' },
})

const loading = ref(true)
const error = ref('')
const sources = ref([])
const status = ref({})
const adFilter = ref({ debug_mode: false, summary: {}, events: [] })
const adFilterLoading = ref(true)
const adFilterError = ref('')
const debugModeBusy = ref(false)
const activeTab = ref('sources')
const adEventFilter = ref('all')
const adEventQuery = ref('')
const expandedAdEventKey = ref('')
const clearConfirming = ref(false)
const busySourceKeys = ref(new Set())
const hostTheme = ref('')
let healthPollTimer = null
let healthPollDeadline = 0
let adFilterPollTimer = null
let clearConfirmTimer = null
let themeObserver = null
let componentUnmounted = false
const HEALTH_POLL_INTERVAL_MS = 1000
const HEALTH_POLL_TIMEOUT_MS = 5 * 60 * 1000
const AD_FILTER_POLL_INTERVAL_MS = 2500

const apiCall = (method, path, payload, options) => {
  if (typeof props.api?.[method] !== 'function') {
    return Promise.reject(new Error('MoviePilot API 客户端未注入'))
  }
  const request = props.api[method]
  const url = `plugin/${props.pluginId}${path}`
  if (payload === undefined && options !== undefined) return request(url, options)
  if (options !== undefined) return request(url, payload, options)
  if (payload !== undefined) return request(url, payload)
  return request(url)
}

function unwrap(response) {
  // V3 returns the endpoint payload directly. Keep a narrow fallback for
  // older hosts that still wrap it in an Axios-like { data } object.
  const body = response?.success !== undefined
    ? response
    : response?.data?.success !== undefined
      ? response.data
      : response
  if (body?.success === false) throw new Error(body.message || '请求失败')
  return body?.success === true ? (body.data ?? {}) : (body ?? {})
}

async function load(options = {}) {
  const silent = options?.silent === true
  if (!silent) {
    loading.value = true
    error.value = ''
  }
  try {
    const requestOptions = silent ? { feedback: 'silent' } : undefined
    const [statusResponse, sourceResponse] = await Promise.all([
      apiCall('get', '/status', undefined, requestOptions),
      apiCall('get', '/sources', undefined, requestOptions),
    ])
    status.value = unwrap(statusResponse)
    sources.value = unwrap(sourceResponse) || []
  } catch (loadError) {
    error.value = loadError?.message || '加载 LunaTV 状态失败'
  } finally {
    if (!silent) loading.value = false
  }
  await loadAdFilter({ silent })
}

async function loadAdFilter(options = {}) {
  const silent = options?.silent === true
  if (!silent) {
    adFilterLoading.value = true
    adFilterError.value = ''
  }
  try {
    adFilter.value = unwrap(await apiCall(
      'get',
      '/ad-filter',
      undefined,
      silent ? { feedback: 'silent' } : undefined,
    ))
  } catch (loadError) {
    adFilterError.value = loadError?.message || '读取广告拦截调试记录失败'
  } finally {
    if (!silent) adFilterLoading.value = false
  }
}

function scheduleAdFilterPoll() {
  if (componentUnmounted) return
  if (adFilterPollTimer) clearTimeout(adFilterPollTimer)
  adFilterPollTimer = setTimeout(async () => {
    await loadAdFilter({ silent: true })
    if (!componentUnmounted) scheduleAdFilterPoll()
  }, AD_FILTER_POLL_INTERVAL_MS)
}

async function setDebugMode(enabled) {
  if (debugModeBusy.value || Boolean(adFilter.value.debug_mode) === Boolean(enabled)) return
  debugModeBusy.value = true
  adFilterError.value = ''
  try {
    unwrap(await apiCall('post', '/debug', { enabled }, { feedback: 'silent' }))
    await loadAdFilter({ silent: true })
    status.value = { ...status.value, debug_mode: Boolean(enabled) }
  } catch (requestError) {
    adFilterError.value = requestError?.message || '切换调试模式失败'
  } finally {
    debugModeBusy.value = false
  }
}

async function clearAdFilterEvents() {
  if (!adEvents.value.length) return
  try {
    unwrap(await apiCall('post', '/ad-filter/clear', undefined, { feedback: 'silent' }))
    await loadAdFilter({ silent: true })
    expandedAdEventKey.value = ''
  } catch (requestError) {
    adFilterError.value = requestError?.message || '清空广告拦截记录失败'
  } finally {
    clearConfirming.value = false
    if (clearConfirmTimer) clearTimeout(clearConfirmTimer)
    clearConfirmTimer = null
  }
}

function requestClearAdFilterEvents() {
  if (!adEvents.value.length) return
  if (clearConfirming.value) {
    clearAdFilterEvents()
    return
  }
  clearConfirming.value = true
  if (clearConfirmTimer) clearTimeout(clearConfirmTimer)
  clearConfirmTimer = setTimeout(() => {
    clearConfirming.value = false
    clearConfirmTimer = null
  }, 4500)
}

function cancelClearAdFilterEvents() {
  clearConfirming.value = false
  if (clearConfirmTimer) clearTimeout(clearConfirmTimer)
  clearConfirmTimer = null
}

async function loadHealthStatus() {
  const [statusResponse, sourceResponse] = await Promise.all([
    apiCall('get', '/status', undefined, { feedback: 'silent' }),
    apiCall('get', '/sources', undefined, { feedback: 'silent' }),
  ])
  status.value = unwrap(statusResponse)
  sources.value = unwrap(sourceResponse) || []
}

function clearHealthPoll() {
  if (healthPollTimer) clearTimeout(healthPollTimer)
  healthPollTimer = null
  healthPollDeadline = 0
}

function scheduleHealthPoll() {
  if (componentUnmounted) return
  if (Date.now() >= healthPollDeadline) {
    error.value = '健康检查仍在后台运行，请稍后点击“立即刷新”查看结果'
    clearHealthPoll()
    return
  }
  if (healthPollTimer) clearTimeout(healthPollTimer)
  healthPollTimer = setTimeout(async () => {
    try {
      await loadHealthStatus()
    } catch (pollError) {
      if (componentUnmounted) return
      error.value = pollError?.message || '刷新健康检查状态失败'
      clearHealthPoll()
      return
    }
    if (componentUnmounted) return
    if (sourceHealth.value.running) scheduleHealthPoll()
    else {
      await load({ silent: true })
      clearHealthPoll()
    }
  }, HEALTH_POLL_INTERVAL_MS)
}

function sourceIsBusy(source) {
  return busySourceKeys.value.has(source.key)
}

async function setSourceEnabled(source, enabled) {
  if (!source?.key || sourceIsBusy(source)) return
  const nextBusyKeys = new Set(busySourceKeys.value)
  nextBusyKeys.add(source.key)
  busySourceKeys.value = nextBusyKeys
  error.value = ''
  try {
    const result = unwrap(await apiCall(
      'post',
      '/sources/state',
      { source_key: source.key, enabled },
      { feedback: 'silent' },
    ))
    await load({ silent: true })
    if (enabled && result?.check_started && sourceHealth.value.running) {
      healthPollDeadline = Date.now() + HEALTH_POLL_TIMEOUT_MS
      scheduleHealthPoll()
    }
  } catch (requestError) {
    error.value = requestError?.message || `更新“${source.name || source.key}”状态失败`
  } finally {
    const remainingBusyKeys = new Set(busySourceKeys.value)
    remainingBusyKeys.delete(source.key)
    busySourceKeys.value = remainingBusyKeys
  }
}

function setSourceConfig(source, event) {
  const value = event?.target?.value
  setSourceEnabled(source, value === 'enabled')
}

async function recheckSource(source) {
  if (!source?.key || sourceIsBusy(source)) return
  const nextBusyKeys = new Set(busySourceKeys.value)
  nextBusyKeys.add(source.key)
  busySourceKeys.value = nextBusyKeys
  error.value = ''
  try {
    unwrap(await apiCall(
      'post',
      '/sources/refresh',
      { source_key: source.key },
      { feedback: 'silent' },
    ))
    await load({ silent: true })
    if (sourceHealth.value.running) {
      healthPollDeadline = Date.now() + HEALTH_POLL_TIMEOUT_MS
      scheduleHealthPoll()
    }
  } catch (requestError) {
    error.value = requestError?.message || `重新检查“${source.name || source.key}”失败`
  } finally {
    const remainingBusyKeys = new Set(busySourceKeys.value)
    remainingBusyKeys.delete(source.key)
    busySourceKeys.value = remainingBusyKeys
  }
}

const directoryStatus = computed(() => status.value.directories || {})
const downloadSettings = computed(() => status.value.download_settings || {})
const engineStatus = computed(() => status.value.engine || {})
const subscriptionStatus = computed(() => status.value.subscription || {})
const sourceHealth = computed(() => status.value.source_health || {})
const healthChecked = computed(() => Math.max(0, Number(sourceHealth.value.checked || 0)))
const healthCheckTotal = computed(() => Math.max(0, Number(sourceHealth.value.check_total || 0)))
const healthProgress = computed(() => {
  if (!healthCheckTotal.value) return 0
  return Math.min(100, Math.round((healthChecked.value / healthCheckTotal.value) * 100))
})
const hasCachedHealth = computed(() => sources.value.some(source => (
  Number(source?.last_checked || 0) > 0
  || ['healthy', 'failed'].includes(String(source?.health_status || '').toLowerCase())
)))
const healthProgressLabel = computed(() => {
  if (sourceHealth.value.running && !healthCheckTotal.value) return '正在读取来源清单…'
  if (!healthCheckTotal.value) return hasCachedHealth.value ? '当前显示缓存状态' : '尚未开始健康检查'
  return `${sourceHealth.value.running ? '本轮进度' : '最近一轮'} ${healthChecked.value} / ${healthCheckTotal.value}`
})
const queueStatus = computed(() => status.value.queue || {})
const queueTotal = computed(() => ['pending', 'running', 'paused']
  .reduce((total, state) => total + Number(queueStatus.value[state] || 0), 0))
const followupStatus = computed(() => status.value.followup_status || {})
const subscriptionRefreshStatus = computed(() => followupStatus.value.subscription_refresh || {})
const mediaSyncStatus = computed(() => followupStatus.value.media_server_sync || {})
const adSummary = computed(() => adFilter.value.summary || {})
const adEvents = computed(() => Array.isArray(adFilter.value.events) ? adFilter.value.events : [])
const debugModeEnabled = computed(() => Boolean(adFilter.value.debug_mode ?? status.value.debug_mode))
const latestAdEvent = computed(() => adEvents.value[0] || null)
const adBlockedEventCount = computed(() => adEvents.value.filter(event => Number(event.filtered_segments || 0) > 0).length)
const adCleanEventCount = computed(() => Math.max(0, adEvents.value.length - adBlockedEventCount.value))
const visibleAdEvents = computed(() => {
  const filter = adEventFilter.value
  const query = adEventQuery.value.trim().toLowerCase()
  return adEvents.value.filter(event => {
    const filteredSegments = Number(event.filtered_segments || 0)
    if (filter === 'blocked' && filteredSegments <= 0) return false
    if (filter === 'clean' && filteredSegments > 0) return false
    if (!query) return true
    return [event.title, event.source_name, event.source_key]
      .some(value => String(value || '').toLowerCase().includes(query))
  })
})
const adMonitoringActive = computed(() => Number(queueStatus.value.running || 0) > 0)
const adMonitorStatus = computed(() => {
  if (adMonitoringActive.value) return `有 ${queueStatus.value.running} 个下载任务运行，扫描结果会自动更新`
  if (latestAdEvent.value) return `最近一次扫描于 ${formattedTime(latestAdEvent.value.timestamp)}`
  return '等待本地下载任务开始扫描 HLS 清单'
})
const sourceSummary = computed(() => {
  const summary = { healthy: 0, attention: 0, disabled: 0 }
  for (const source of sources.value) {
    if (source?.manual_disabled || source?.disabled_reason === 'configured') {
      summary.disabled += 1
    } else if (['healthy', 'ready', 'ok'].includes(String(source?.health_status || '').toLowerCase())) {
      summary.healthy += 1
    } else {
      summary.attention += 1
    }
  }
  return summary
})

function adEventKey(event) {
  return `${event?.task_id || 'scan'}-${event?.timestamp || 0}`
}

function toggleAdEvent(event) {
  const key = adEventKey(event)
  expandedAdEventKey.value = expandedAdEventKey.value === key ? '' : key
}

function adEpisodeLabel(event) {
  if (event?.media_type !== 'tv' || !event?.season) return ''
  return `S${String(event.season).padStart(2, '0')}E${String(event.episode || 0).padStart(2, '0')}`
}

function adLogLine(event) {
  const episode = adEpisodeLabel(event)
  const target = [event?.title || '未命名任务', episode].filter(Boolean).join(' ')
  return `[${formattedTime(event?.timestamp)}] ${event?.status_label || '扫描完成'} · ${target} · ${event?.filtered_segments || 0} 段 / ${formattedSeconds(event?.filtered_seconds)} · ${event?.source_name || event?.source_key || 'LunaTV'}`
}

function followupSummary(item) {
  if (item?.running) return '进行中'
  if (!item?.finished_at) return '暂无记录'
  return `${item.success === false ? '失败' : '成功'} · ${formattedTime(item.finished_at)}`
}

function sourceVisualStatus(source) {
  if (
    source?.manual_disabled
    || source?.disabled_reason === 'configured'
    || ['pending', 'unchecked'].includes(source?.health_status)
  ) return 'muted'
  if (source?.search_status === 'restricted') return 'warning'
  return source?.status || 'ready'
}

function sourceSearchVisualStatus(source) {
  if (
    source?.manual_disabled
    || source?.disabled_reason === 'configured'
    || ['pending', 'unchecked'].includes(source?.health_status)
  ) return 'muted'
  return source?.search_status || 'supported'
}

function sourceHealthVisualStatus(source) {
  if (
    source?.manual_disabled
    || source?.disabled_reason === 'configured'
    || ['pending', 'unchecked'].includes(source?.health_status)
  ) return 'muted'
  if (source?.search_status === 'restricted') return 'warning'
  return source?.health_status || 'unknown'
}

function sourceCheckedLabel(source) {
  return source?.check_state === 'pending' ? '等待本轮检查' : formattedTime(source?.last_checked)
}

function formattedTime(value) {
  if (!value) return '未检查'
  const numeric = Number(value)
  const date = new Date(Number.isFinite(numeric) ? numeric * 1000 : value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

function sourceUrl(source) {
  const candidate = String(source?.url || source?.detail || source?.api || '').trim()
  if (!candidate) return ''
  try {
    const parsed = new URL(candidate)
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : ''
  } catch {
    return ''
  }
}

function sourceHost(source) {
  const url = sourceUrl(source)
  return url ? new URL(url).hostname : '—'
}

function formattedSeconds(value) {
  const seconds = Number(value || 0)
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 秒'
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`
}

function syncHostTheme() {
  if (typeof document === 'undefined') return
  hostTheme.value = document.documentElement?.dataset?.theme || ''
}

onMounted(load)
onMounted(scheduleAdFilterPoll)
onMounted(() => {
  syncHostTheme()
  if (typeof MutationObserver === 'undefined' || typeof document === 'undefined') return
  themeObserver = new MutationObserver(syncHostTheme)
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  })
})
onBeforeUnmount(() => {
  componentUnmounted = true
  clearHealthPoll()
  if (adFilterPollTimer) clearTimeout(adFilterPollTimer)
  adFilterPollTimer = null
  if (clearConfirmTimer) clearTimeout(clearConfirmTimer)
  clearConfirmTimer = null
  themeObserver?.disconnect()
  themeObserver = null
})
</script>

<template>
  <div class="lunatv-page" :data-host-theme="hostTheme">
    <div class="lunatv-header">
      <div>
        <div class="lunatv-eyebrow">THIRD-PARTY CMS / M3U8</div>
        <h1>LunaTV 资源订阅</h1>
        <p>接入 MoviePilot 原生搜索、订阅与下载；播放继续交给既有 Emby。</p>
      </div>
      <div class="header-status">
        <span class="chip">
          并发上限：{{ downloadSettings.max_concurrent_tasks || 2 }} 任务 × {{ downloadSettings.segment_thread_count || 16 }} 分片
        </span>
        <span :class="['chip', engineStatus.ready ? 'ready' : 'muted-chip']">
          N_m3u8DL-RE {{ engineStatus.ready ? '已就绪' : (engineStatus.supported ? '内置待安装' : '当前平台不支持') }}
        </span>
        <span :class="['chip', status.ai?.available ? 'ready' : 'muted-chip']">AI {{ status.ai?.available ? '已就绪' : '未启用' }}</span>
        <span :class="['chip', status.media_server_sync_running ? 'busy' : 'muted-chip']">媒体库 {{ status.media_server_sync_running ? '同步中' : '自动刷新' }}</span>
      </div>
      <div class="lunatv-actions">
        <button class="button secondary" type="button" :disabled="loading" aria-label="立即刷新工作台状态" @click="load">{{ loading ? '刷新中…' : '立即刷新' }}</button>
      </div>
    </div>

    <nav class="page-tabs" role="tablist" aria-label="LunaTV 工作台页签">
      <button
        type="button"
        id="sources-tab"
        class="page-tab"
        :class="{ 'is-active': activeTab === 'sources' }"
        role="tab"
        :aria-selected="activeTab === 'sources'"
        aria-controls="sources-panel"
        @click="activeTab = 'sources'"
      >
        <span>资源来源</span>
        <span class="tab-count">{{ loading ? '…' : sources.length }}</span>
      </button>
      <button
        type="button"
        id="ad-filter-tab"
        class="page-tab"
        :class="{ 'is-active': activeTab === 'ad-filter' }"
        role="tab"
        :aria-selected="activeTab === 'ad-filter'"
        aria-controls="ad-filter-panel"
        :aria-label="`广告拦截，${adBlockedEventCount} 次命中，${adEvents.length} 次扫描`"
        @click="activeTab = 'ad-filter'"
      >
        <span>广告拦截</span>
        <span class="tab-count tab-count-wide" :title="`${adBlockedEventCount} 次命中 / ${adEvents.length} 次扫描`">{{ adBlockedEventCount }}/{{ adEvents.length }}</span>
      </button>
    </nav>

    <div v-if="error" class="alert error">{{ error }}</div>
    <template v-if="activeTab === 'sources'">
      <div v-if="sourceHealth.last_error" class="alert error">
        最近一次健康检查失败：{{ sourceHealth.last_error }}
      </div>
      <div v-if="status.source_config?.error" class="alert warning">
        远程来源清单刷新失败，当前使用{{ status.source_config?.origin || '缓存' }}：{{ status.source_config.error }}
      </div>
      <div v-if="subscriptionRefreshStatus.error" class="alert warning">
        最近一次追更失败：{{ subscriptionRefreshStatus.error }}
      </div>
      <div v-if="mediaSyncStatus.error" class="alert warning">
        最近一次媒体库或订阅进度同步失败：{{ mediaSyncStatus.error }}
      </div>

      <section class="overview-grid" aria-label="LunaTV 运行概况">
        <article class="overview-card overview-card-queue">
          <div class="overview-card-heading"><span class="overview-label">下载队列</span><span class="overview-status" :class="{ 'is-live': queueStatus.running }">{{ queueStatus.running ? '运行中' : (queueTotal ? '排队中' : '空闲') }}</span></div>
          <strong>{{ queueStatus.running || 0 }}<small> 个运行中</small></strong>
          <span class="overview-card-meta">等待 {{ queueStatus.pending || 0 }} · 暂停 {{ queueStatus.paused || 0 }} · 活动 {{ queueTotal }}</span>
        </article>
        <article class="overview-card">
          <div class="overview-card-heading"><span class="overview-label">来源健康</span><span class="overview-status" :class="sourceSummary.attention ? 'is-warning' : 'is-good'">{{ sourceSummary.attention ? '需要关注' : '运行正常' }}</span></div>
          <strong>{{ sourceSummary.healthy }}<small> 个正常</small></strong>
          <span class="overview-card-meta">{{ sourceSummary.attention }} 个待处理 · {{ sourceSummary.disabled }} 个已禁用</span>
        </article>
        <article class="overview-card overview-card-wide">
          <div class="overview-card-heading"><span class="overview-label">下载目录</span><span class="overview-status">{{ directoryStatus.source || '未配置' }}</span></div>
          <strong class="overview-path" :title="directoryStatus.configured_root || directoryStatus.auto_roots?.[0]?.download_path || '未配置'">{{ directoryStatus.configured_root || directoryStatus.auto_roots?.[0]?.download_path || '未配置' }}</strong>
          <span class="overview-card-meta">完成后整理 · TMDB 由 MoviePilot 原生链关联</span>
        </article>
        <article class="overview-card">
          <div class="overview-card-heading"><span class="overview-label">自动追更</span><span class="overview-status">每 {{ subscriptionStatus.refresh_minutes || 30 }} 分钟</span></div>
          <strong>{{ followupSummary(subscriptionRefreshStatus) }}</strong>
          <span class="overview-card-meta">媒体库同步：{{ followupSummary(mediaSyncStatus) }}</span>
        </article>
      </section>
    </template>

    <section v-else id="ad-filter-panel" class="panel ad-filter-panel" role="tabpanel" aria-labelledby="ad-filter-tab">
      <div class="ad-page-heading">
        <div>
          <div class="page-kicker">HLS / DEBUG LOG</div>
          <h2 id="ad-filter-title">广告拦截监控 <span :class="['debug-badge', debugModeEnabled ? 'is-on' : 'is-off']">{{ debugModeEnabled ? '调试已开启' : '调试已关闭' }}</span></h2>
          <p>本地下载会在 N_m3u8DL-RE 前扫描 HLS 清单；STRM 原始直链不会经过过滤。调试开关只影响日志详细程度，不改变拦截规则。</p>
        </div>
        <div class="ad-page-actions">
          <span class="auto-refresh"><i class="live-dot" aria-hidden="true"></i>自动更新 · 2.5 秒</span>
          <label class="debug-switch" title="只控制 DEBUG 日志输出，不改变广告拦截规则">
            <input
              type="checkbox"
              :checked="debugModeEnabled"
              :disabled="debugModeBusy"
              @change="setDebugMode($event.target.checked)"
            />
            <span>调试日志</span>
          </label>
          <template v-if="clearConfirming">
            <button class="source-action is-danger" type="button" @click="clearAdFilterEvents">确认清空</button>
            <button class="source-action" type="button" @click="cancelClearAdFilterEvents">取消</button>
          </template>
          <button v-else class="source-action" type="button" :disabled="!adEvents.length" @click="requestClearAdFilterEvents">清空记录</button>
        </div>
      </div>
      <div v-if="adFilterError" class="alert warning">{{ adFilterError }}</div>
      <div class="ad-monitor-state" :class="{ 'is-active': adMonitoringActive, 'is-loading': adFilterLoading }">
        <span class="monitor-icon" aria-hidden="true"><i></i></span>
        <div class="monitor-state-copy">
          <strong>{{ adMonitoringActive ? '监控中' : (latestAdEvent ? '最近扫描已完成' : '等待扫描') }}</strong>
          <span>{{ adMonitorStatus }}</span>
        </div>
        <div class="monitor-state-meta">
          <span>最近记录</span>
          <strong>{{ adSummary.last_scan_at ? formattedTime(adSummary.last_scan_at) : '暂无' }}</strong>
        </div>
      </div>
      <div class="ad-metrics">
        <div class="ad-metric">
          <span class="ad-metric-label">HLS 扫描</span>
          <strong>{{ adSummary.scan_count || 0 }}</strong>
          <small>保留最近 {{ adFilter.retained_events || 100 }} 条</small>
        </div>
        <div class="ad-metric is-blocked">
          <span class="ad-metric-label">拦截片段</span>
          <strong>{{ adSummary.filtered_segments || 0 }}<small> 段</small></strong>
          <small>{{ adSummary.blocked_scan_count || 0 }} 次扫描命中</small>
        </div>
        <div class="ad-metric">
          <span class="ad-metric-label">拦截时长</span>
          <strong>{{ formattedSeconds(adSummary.filtered_seconds) }}</strong>
          <small>按保存记录累计</small>
        </div>
        <div class="ad-metric">
          <span class="ad-metric-label">命中率</span>
          <strong>{{ adSummary.scan_count ? Math.round((adSummary.blocked_scan_count / adSummary.scan_count) * 100) : 0 }}<small>%</small></strong>
          <small>{{ adBlockedEventCount }} 条有拦截记录</small>
        </div>
      </div>
      <div class="ad-log-section">
        <div class="ad-log-heading">
          <div>
            <h3>扫描日志</h3>
            <span>{{ visibleAdEvents.length }} / {{ adEvents.length }} 条扫描记录 · 点击记录查看调试输出</span>
          </div>
          <div class="ad-log-controls">
            <div class="ad-filter-segments" role="group" aria-label="筛选扫描日志">
              <button type="button" :class="{ 'is-active': adEventFilter === 'all' }" @click="adEventFilter = 'all'">全部 {{ adEvents.length }}</button>
              <button type="button" :class="{ 'is-active': adEventFilter === 'blocked' }" @click="adEventFilter = 'blocked'">已拦截 {{ adBlockedEventCount }}</button>
              <button type="button" :class="{ 'is-active': adEventFilter === 'clean' }" @click="adEventFilter = 'clean'">未发现 {{ adCleanEventCount }}</button>
            </div>
            <input v-model="adEventQuery" class="ad-log-search" type="search" placeholder="筛选剧名或来源" aria-label="筛选剧名或来源" />
          </div>
        </div>
        <div v-if="adFilterLoading && !adEvents.length" class="empty">正在读取广告拦截记录…</div>
        <div v-else-if="!adEvents.length" class="empty ad-empty-state"><strong>还没有扫描记录</strong><span>下载电视剧后，HLS 扫描结果会自动出现在这里。</span></div>
        <div v-else-if="!visibleAdEvents.length" class="empty ad-empty-state"><strong>没有匹配的记录</strong><span>换一个筛选条件或清空搜索关键词。</span></div>
        <div v-else class="ad-event-list">
          <button
            v-for="event in visibleAdEvents"
            :key="adEventKey(event)"
            type="button"
            class="ad-event"
            :class="{ 'is-expanded': expandedAdEventKey === adEventKey(event) }"
            :aria-expanded="expandedAdEventKey === adEventKey(event)"
            @click="toggleAdEvent(event)"
          >
            <div class="ad-event-main">
              <div class="ad-event-title">
                <span :class="['ad-event-status', event.filtered_segments ? 'is-blocked' : 'is-clean']">
                  {{ event.filtered_segments ? '已拦截' : '未发现' }}
                </span>
                <strong>{{ event.title || '未命名任务' }}</strong>
                <span v-if="adEpisodeLabel(event)" class="muted">{{ adEpisodeLabel(event) }}</span>
              </div>
              <div class="ad-event-meta">
                <span>{{ event.source_name || event.source_key || 'LunaTV' }}</span>
                <span>{{ formattedTime(event.timestamp) }}</span>
              </div>
            </div>
            <div class="ad-event-detail">
              <strong>{{ event.filtered_segments || 0 }} 段 · {{ formattedSeconds(event.filtered_seconds) }}</strong>
              <span>CUE {{ event.cue_segments || 0 }}</span>
              <span>结构 {{ event.splice_segments || 0 }}</span>
              <span>同资产 {{ event.same_asset_splice_segments || 0 }}</span>
              <span>正则 {{ event.regex_segments || 0 }}</span>
              <span class="ad-event-chevron" aria-hidden="true">{{ expandedAdEventKey === adEventKey(event) ? '收起' : '详情' }}</span>
            </div>
            <div v-if="expandedAdEventKey === adEventKey(event)" class="ad-event-expanded">
              <code>{{ adLogLine(event) }}</code>
              <div class="ad-breakdown">
                <span>闭合 CUE：{{ event.cue_segments || 0 }} 段 / {{ formattedSeconds(event.cue_seconds) }}</span>
                <span>结构拼接：{{ event.splice_segments || 0 }} 段 / {{ formattedSeconds(event.splice_seconds) }}</span>
                <span>同资产插入：{{ event.same_asset_splice_segments || 0 }} 段 / {{ formattedSeconds(event.same_asset_splice_seconds) }}</span>
                <span>URL 正则：{{ event.regex_segments || 0 }} 段</span>
                <span>未闭合 CUE：{{ event.unclosed_cue || 0 }}</span>
                <span>DATERANGE：{{ event.daterange_candidates || 0 }} · DISCONTINUITY：{{ event.discontinuity || 0 }}</span>
              </div>
            </div>
          </button>
        </div>
      </div>
    </section>

    <section v-if="activeTab === 'sources'" id="sources-panel" class="panel source-panel" role="tabpanel" aria-labelledby="sources-tab">
      <div class="section-heading source-panel-heading">
        <div>
          <div class="section-title">资源站 <span class="muted">{{ loading ? '…' : sources.length }}</span></div>
          <span class="source-caption">页面读取缓存；来源会按后台健康检查结果参与搜索，单源可单独测试。</span>
        </div>
        <div class="source-health-summary" aria-label="来源状态汇总">
          <span class="summary-item is-good"><i class="legend-dot is-healthy" aria-hidden="true"></i>{{ sourceSummary.healthy }} 正常</span>
          <span class="summary-item is-warning"><i class="legend-dot is-pending" aria-hidden="true"></i>{{ sourceSummary.attention }} 待关注</span>
          <span class="summary-item"><i class="legend-dot is-pending" aria-hidden="true"></i>{{ sourceSummary.disabled }} 已禁用</span>
        </div>
      </div>
      <div v-if="!loading && sources.length" :class="['health-overview', { 'is-running': sourceHealth.running }]">
        <div class="health-progress-block">
          <div class="health-progress-heading">
            <span class="health-progress-title">{{ sourceHealth.running ? '正在逐个检查来源' : '来源健康状态' }}</span>
            <span class="health-progress-count">{{ healthProgressLabel }}</span>
          </div>
          <div
            class="health-progress-track"
            role="progressbar"
            :aria-label="healthProgressLabel"
            :aria-valuemin="0"
            :aria-valuemax="100"
            :aria-valuenow="healthProgress"
          >
            <span :style="{ width: `${healthProgress}%` }"></span>
          </div>
        </div>
        <div class="health-legend" aria-label="健康状态图例">
          <span><i class="legend-dot is-pending" aria-hidden="true"></i>待检查</span>
          <span><i class="legend-dot is-healthy" aria-hidden="true"></i>正常</span>
          <span><i class="legend-dot is-failed" aria-hidden="true"></i>不可用</span>
        </div>
      </div>
      <div v-if="loading" class="empty">正在读取资源站配置…</div>
      <div v-else-if="!sources.length" class="empty">暂未读取到资源站配置</div>
      <div v-else class="source-table-wrap">
        <table class="source-table">
          <thead>
            <tr>
              <th scope="col">状态</th>
              <th scope="col">资源名称</th>
              <th scope="col">网址</th>
              <th scope="col">搜索功能</th>
              <th scope="col">最近检查</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="source in sources" :key="source.key" :class="{ 'is-pending': source.check_state === 'pending' }">
              <td>
                <span :class="['source-state', `is-${sourceVisualStatus(source)}`]">
                  <i class="state-dot" aria-hidden="true"></i>
                  {{ source.status_label || '已加载' }}
                </span>
                <div class="health-status">
                  <span :class="['health-state', `is-${sourceHealthVisualStatus(source)}`]">
                    {{ source.health_label || '未检查' }}
                  </span>
                                    <div class="network-metrics">
                    <span>{{ source.network_label || '待检查' }}</span>
                    <span>成功 {{ source.network_successes || 0 }} 次</span>
                    <span>失败 {{ source.network_failures || 0 }} 次</span>
                  </div>
<span v-if="source.last_error && source.check_state !== 'pending'" class="source-error" :title="source.last_error">{{ source.last_error }}</span>
                </div>
              </td>
              <td>
                <div class="source-identity">
                  <span class="source-name">{{ source.name }}</span>
                  <span class="source-key">{{ source.key }}</span>
                </div>
              </td>
              <td>
                <a
                  v-if="sourceUrl(source)"
                  class="source-link"
                  :href="sourceUrl(source)"
                  target="_blank"
                  rel="noopener noreferrer"
                >{{ sourceHost(source) }}</a>
                <span v-else class="muted">—</span>
              </td>
              <td>
                <span :class="['search-state', `is-${sourceSearchVisualStatus(source)}`]">
                  {{ source.search_label || '支持' }}
                </span>
              </td>
              <td><span :class="{ 'pending-time': source.check_state === 'pending' }">{{ sourceCheckedLabel(source) }}</span></td>
              <td>
                <div class="source-actions">
  <select
    class="source-config-select"
    :value="source.manual_disabled ? 'disabled' : 'enabled'"
    :disabled="sourceIsBusy(source)"
    :aria-label="'配置' + (source.name || source.key) + '来源'"
    @change="setSourceConfig(source, $event)"
  >
    <option value="enabled">配置启用</option>
    <option value="disabled">配置禁用</option>
  </select>
  <button
    class="source-action"
    :disabled="sourceIsBusy(source)"
    :aria-label="'测试来源 ' + (source.name || source.key)"
    @click="recheckSource(source)"
  >{{ sourceIsBusy(source) ? '测试中…' : '测试' }}</button>
</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'sources'" class="panel help-panel">
      <div class="section-heading help-heading">
        <div class="section-title">使用说明</div>
        <span class="source-caption">常用规则与处理方式</span>
      </div>
      <div class="help-grid">
        <p><strong>目录</strong>：目录留空时按媒体类型读取 MoviePilot 的本地目录；填写插件目录则优先使用插件目录。</p>
        <p><strong>多季合集</strong>：有明确季号或 TMDB 季集数能完整对应时才会自动分季；无法确认时会暂停，避免错放。</p>
        <p><strong>自动追更</strong>：MoviePilot 活跃电视剧订阅会定期重新搜索；已完成和正在下载的集数会跳过，只排队新增集。</p>
        <p><strong>媒体库</strong>：目录内没有正在下载的缓存文件后才显示完整文件夹；完成后可请求 Emby/Jellyfin 刷新。</p>
        <p><strong>播放</strong>：插件不内置 m3u8 播放器，播放仍由已有 Emby/Jellyfin 页面负责。</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.lunatv-page {
  color: rgb(var(--v-theme-on-background, 232, 231, 241));
  width: 100%;
  max-width: none;
  margin: 0;
  padding: 32px;
  box-sizing: border-box;
  /* The host repaints its wallpaper behind plugin pages, so every surface is
     expressed as a token that the theme block at the end of this file can
     swap for MoviePilot's own glass/transparency material. */
  --ltv-page-bg: rgb(var(--v-theme-background, 16, 16, 24));
  --ltv-surface: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity-heavy, 1));
  --ltv-surface-soft: rgba(var(--v-theme-on-surface, 232, 231, 241), .035);
  --ltv-surface-quiet: rgba(var(--v-theme-on-surface, 232, 231, 241), .018);
  --ltv-border: rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .12));
  --ltv-border-soft: rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .1));
  --ltv-shadow: 0 14px 34px rgba(0, 0, 0, .12);
  --ltv-shadow-soft: 0 10px 26px rgba(0, 0, 0, .08);
  --ltv-blur: none;
  background: var(--ltv-page-bg);
  min-height: 100%;
}
.lunatv-header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 24px; }
.lunatv-eyebrow { color: rgb(var(--v-theme-primary, 139, 92, 246)); font-size: 12px; letter-spacing: .14em; font-weight: 700; }
h1 { margin: 8px 0; font-size: 32px; }
p { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); margin: 0; }
.header-status { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.page-tabs { display: flex; gap: 4px; margin-bottom: 18px; padding: 4px; border: 1px solid var(--ltv-border); border-radius: 12px; background: var(--ltv-surface); backdrop-filter: var(--ltv-blur); }
.page-tab { flex: 0 0 auto; border: 0; border-radius: 9px; padding: 9px 16px; color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .68)); background: transparent; cursor: pointer; font: inherit; font-size: 13px; font-weight: 650; }
.page-tab:hover { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-on-surface, 232, 231, 241), .06); }
.page-tab.is-active { color: rgb(var(--v-theme-on-primary, 255, 255, 255)); background: rgb(var(--v-theme-primary, 139, 92, 246)); box-shadow: 0 4px 12px rgba(var(--v-theme-primary, 139, 92, 246), .24); }
.chip { border-radius: 999px; background: rgba(var(--v-theme-primary, 139, 92, 246), .14); color: rgb(var(--v-theme-primary, 139, 92, 246)); padding: 6px 9px; font-size: 12px; white-space: nowrap; }
.chip.ready { background: rgba(var(--v-theme-success, 76, 175, 80), .16); color: rgb(var(--v-theme-on-surface, 232, 231, 241)); }
.chip.busy { background: rgba(var(--v-theme-warning, 251, 140, 0), .16); color: rgb(var(--v-theme-on-surface, 232, 231, 241)); }
.chip.muted-chip { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); background: rgba(var(--v-theme-on-surface, 232, 231, 241), .08); }
.lunatv-actions { display: flex; gap: 10px; align-items: center; flex: 0 0 auto; }
.button, .episode-button { border: 0; border-radius: 10px; background: rgb(var(--v-theme-primary, 139, 92, 246)); color: rgb(var(--v-theme-on-primary, 255, 255, 255)); padding: 10px 16px; cursor: pointer; font-weight: 650; white-space: nowrap; }
.button.secondary { background: rgba(var(--v-theme-primary, 139, 92, 246), .14); color: rgb(var(--v-theme-primary, 139, 92, 246)); }
.button:disabled { opacity: .55; cursor: default; }
.panel { background: var(--ltv-surface); border: 1px solid var(--ltv-border); border-radius: 16px; padding: 18px; margin-bottom: 18px; backdrop-filter: var(--ltv-blur); }
.section-title { font-size: 17px; font-weight: 700; margin-bottom: 14px; }
.muted, small { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; }
.alert { border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }
.alert.error { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-error, 244, 67, 54), .16); }
.alert.success { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-success, 76, 175, 80), .16); }
.alert.warning { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-warning, 251, 140, 0), .16); }
.setup-strip { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; }
.setup-strip span { border: 1px solid var(--ltv-border); border-radius: 999px; padding: 6px 9px; background: var(--ltv-surface); }
.section-heading { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 14px; }
.section-heading .section-title { margin-bottom: 0; }
.source-caption { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; white-space: nowrap; }
.ad-filter-heading { align-items: flex-start; }
.ad-filter-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.debug-switch { display: inline-flex; align-items: center; gap: 7px; color: rgb(var(--v-theme-on-surface, 232, 231, 241)); font-size: 12px; font-weight: 650; cursor: pointer; }
.debug-switch input { accent-color: rgb(var(--v-theme-primary, 139, 92, 246)); width: 15px; height: 15px; }
.debug-switch input:disabled { cursor: default; }
.debug-badge, .ad-event-status { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: 11px; font-weight: 650; vertical-align: middle; }
.debug-badge { margin-left: 6px; }
.debug-badge.is-on { color: rgb(var(--v-theme-success, 76, 175, 80)); background: rgba(var(--v-theme-success, 76, 175, 80), .16); }
.debug-badge.is-off { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); background: rgba(var(--v-theme-on-surface, 232, 231, 241), .08); }
.ad-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.ad-metric { display: grid; gap: 4px; min-width: 0; padding: 13px 14px; border: 1px solid var(--ltv-border); border-radius: 12px; background: var(--ltv-surface-soft); }
.ad-metric.is-blocked { border-color: rgba(var(--v-theme-warning, 251, 140, 0), .34); background: rgba(var(--v-theme-warning, 251, 140, 0), .07); }
.ad-metric-label { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; }
.ad-metric strong { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); font-size: 19px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ad-metric small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ad-event-list { display: grid; gap: 8px; max-height: 420px; overflow-y: auto; }
.ad-event { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 11px 12px; border: 1px solid var(--ltv-border-soft); border-radius: 11px; background: var(--ltv-surface-quiet); }
.ad-event-main { min-width: 0; display: grid; gap: 5px; }
.ad-event-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
.ad-event-title strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ad-event-status.is-blocked { color: rgb(var(--v-theme-warning, 251, 140, 0)); background: rgba(var(--v-theme-warning, 251, 140, 0), .16); }
.ad-event-status.is-clean { color: rgb(var(--v-theme-success, 76, 175, 80)); background: rgba(var(--v-theme-success, 76, 175, 80), .16); }
.ad-event-meta, .ad-event-detail { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 11px; }
.ad-event-detail { justify-content: flex-end; white-space: nowrap; }
.ad-event-detail strong { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); font-size: 12px; }
.source-table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.source-table { width: 100%; min-width: 810px; border-collapse: collapse; font-size: 13px; }
.source-table th, .source-table td { padding: 11px 12px; border-bottom: 1px solid rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .12)); text-align: left; white-space: nowrap; }
.source-table th { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; font-weight: 650; }
.source-table tbody tr:last-child td { border-bottom: 0; }
.source-name { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); font-weight: 600; }
.source-link { color: rgb(var(--v-theme-primary, 139, 92, 246)); text-decoration: none; }
.source-link:hover { color: rgb(var(--v-theme-primary, 139, 92, 246)); text-decoration: underline; }
.source-state, .search-state { display: inline-flex; align-items: center; gap: 6px; min-height: 22px; border-radius: 999px; font-size: 12px; font-weight: 650; }
.source-state { padding: 3px 8px; background: rgba(var(--v-theme-on-surface, 232, 231, 241), .08); color: rgb(var(--v-theme-on-surface, 232, 231, 241)); }
.state-dot { width: 6px; height: 6px; border-radius: 50%; background: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); }
.source-state.is-ready { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-success, 76, 175, 80), .16); }
.source-state.is-ready .state-dot { background: rgb(var(--v-theme-success, 76, 175, 80)); }
.source-state.is-warning { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-warning, 251, 140, 0), .16); }
.source-state.is-warning .state-dot { background: rgb(var(--v-theme-warning, 251, 140, 0)); }
.source-state.is-error { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-error, 244, 67, 54), .16); }
.source-state.is-error .state-dot { background: rgb(var(--v-theme-error, 244, 67, 54)); }
.health-status { display: grid; gap: 4px; margin-top: 5px; }
.health-state { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 12px; }
.health-state.is-healthy, .health-state.is-ready { color: rgb(var(--v-theme-success, 76, 175, 80)); }
.health-state.is-unhealthy, .health-state.is-error, .health-state.is-failed { color: rgb(var(--v-theme-error, 244, 67, 54)); }
.health-state.is-warning, .health-state.is-degraded { color: rgb(var(--v-theme-warning, 251, 140, 0)); }
.network-metrics {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 11px;
}
.source-config-select {
  min-width: 92px;
  border: 1px solid rgba(var(--v-theme-primary, 139, 92, 246), .6);
  border-radius: 8px;
  padding: 6px 8px;
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  background: var(--ltv-surface);
  font: inherit;
}
.source-config-select:disabled {
  opacity: .55;
}
.source-error { color: rgb(var(--v-theme-error, 244, 67, 54)); font-size: 12px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-state { padding: 3px 8px; color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-primary, 139, 92, 246), .14); }
.search-state.is-unavailable { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-error, 244, 67, 54), .16); }
.search-state.is-unsupported { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-on-surface, 232, 231, 241), .08); }
.search-state.is-disabled { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-error, 244, 67, 54), .16); }
.search-state.is-empty, .search-state.is-degraded { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-warning, 251, 140, 0), .16); }
.search-state.is-restricted { color: rgb(var(--v-theme-on-surface, 232, 231, 241)); background: rgba(var(--v-theme-warning, 251, 140, 0), .16); }
.source-action { border: 1px solid rgba(var(--v-theme-primary, 139, 92, 246), .45); border-radius: 8px; background: transparent; color: rgb(var(--v-theme-primary, 139, 92, 246)); padding: 5px 10px; cursor: pointer; font-weight: 650; }
.source-action:disabled { cursor: default; opacity: .55; }
.source-actions { display: flex; gap: 6px; }
.empty { color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); padding: 16px 0; }
.help-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 24px; color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62)); font-size: 13px; line-height: 1.6; }
.help-grid p { margin: 0; }
@media (max-width: 760px) { .lunatv-page { padding: 18px; } .lunatv-header { flex-direction: column; align-items: stretch; } .lunatv-actions { justify-content: flex-start; } .section-heading { align-items: flex-start; flex-direction: column; gap: 4px; } .page-tabs { margin-bottom: 14px; } .page-tab { flex: 1 1 0; } }
@media (max-width: 760px) { .help-grid { grid-template-columns: 1fr; } }

/* Follow MoviePilot's own themes: the glass and transparency modes paint a
   wallpaper behind the page, so the plugin page drops its opaque background
   and borrows the host's surface material instead of inventing its own. */
.lunatv-page[data-host-theme='glass'] {
  --ltv-page-bg: transparent;
  --ltv-surface: var(--glass-surface-raised, rgba(11, 19, 34, .32));
  --ltv-surface-soft: var(--glass-control, rgba(255, 255, 255, .07));
  --ltv-surface-quiet: var(--glass-surface, rgba(11, 19, 34, .24));
  --ltv-border: var(--glass-border-raised, rgba(255, 255, 255, .14));
  --ltv-border-soft: var(--glass-border, rgba(255, 255, 255, .1));
  --ltv-shadow: var(--glass-shadow-raised, 0 16px 40px rgba(3, 7, 18, .32));
  --ltv-shadow-soft: var(--glass-shadow, 0 10px 28px rgba(3, 7, 18, .24));
  --ltv-blur: var(--glass-raised-backdrop-filter, none);
}

/* MoviePilot paints its own chips and cards with a sheen over a tinted fill.
   Borrow that material so the status row and panels stay legible when the
   wallpaper shows through instead of dissolving into it. */
.lunatv-page[data-host-theme='glass'] .chip {
  background-color: rgba(11, 19, 34, .52);
  background-image: var(--glass-chip-sheen, none);
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  box-shadow: var(--glass-control-shadow, none);
}
.lunatv-page[data-host-theme='glass'] .chip.ready { background-color: rgba(var(--v-theme-success, 76, 175, 80), .34); }
.lunatv-page[data-host-theme='glass'] .chip.busy { background-color: rgba(var(--v-theme-warning, 251, 140, 0), .34); }
.lunatv-page[data-host-theme='glass'] .chip.muted-chip { background-color: rgba(11, 19, 34, .4); }
.lunatv-page[data-host-theme='glass'] .panel,
.lunatv-page[data-host-theme='glass'] .page-tabs { background-image: var(--glass-sheen, none); }
.lunatv-page[data-host-theme='glass'] .button.secondary {
  border: 1px solid var(--glass-border, rgba(255, 255, 255, .1));
  background-color: var(--glass-control, rgba(11, 19, 34, .52));
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
}

.lunatv-page[data-host-theme='transparent'] {
  --ltv-page-bg: transparent;
  --ltv-surface: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity, .3));
  --ltv-surface-soft: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity-light, .2));
  --ltv-surface-quiet: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity-light, .2));
  --ltv-shadow: 0 16px 36px rgba(0, 0, 0, .18);
  --ltv-shadow-soft: 0 10px 24px rgba(0, 0, 0, .12);
  --ltv-blur: blur(var(--transparent-blur, 10px)) saturate(1.2);
}
.lunatv-page[data-host-theme='transparent'] .chip {
  background-color: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity, .3));
  backdrop-filter: blur(var(--transparent-blur, 10px));
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
}
.lunatv-page[data-host-theme='transparent'] .chip.ready { background-color: rgba(var(--v-theme-success, 76, 175, 80), .28); }
.lunatv-page[data-host-theme='transparent'] .chip.busy { background-color: rgba(var(--v-theme-warning, 251, 140, 0), .28); }
.lunatv-page[data-host-theme='transparent'] .button.secondary {
  border: 1px solid var(--ltv-border);
  background-color: rgba(var(--v-theme-surface, 23, 23, 34), var(--transparent-opacity, .3));
}

.lunatv-page {
  padding: clamp(18px, 3vw, 32px);
}

.lunatv-header {
  gap: 20px;
}

.panel {
  box-shadow: var(--ltv-shadow);
}

.health-overview {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 14px 16px;
  margin-bottom: 14px;
  border: 1px solid var(--ltv-border);
  border-radius: 12px;
  background: var(--ltv-surface-soft);
}

.health-overview.is-running {
  border-color: rgba(var(--v-theme-primary, 139, 92, 246), .38);
  background: rgba(var(--v-theme-primary, 139, 92, 246), .07);
}

.health-progress-block {
  flex: 1;
  min-width: 220px;
}

.health-progress-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.health-progress-title {
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 13px;
  font-weight: 700;
}

.health-progress-count {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 12px;
  white-space: nowrap;
}

.health-progress-track {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .10);
}

.health-progress-track span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, rgb(var(--v-theme-primary, 139, 92, 246)), rgb(var(--v-theme-success, 76, 175, 80)));
  transition: width .25s ease;
}

.health-legend {
  display: flex;
  align-items: center;
  gap: 12px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 12px;
  white-space: nowrap;
}

.health-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.legend-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .34);
}

.legend-dot.is-healthy { background: rgb(var(--v-theme-success, 76, 175, 80)); }
.legend-dot.is-failed { background: rgb(var(--v-theme-error, 244, 67, 54)); }

.source-table-wrap {
  border: 1px solid var(--ltv-border-soft);
  border-radius: 12px;
  background: var(--ltv-surface-quiet);
}

.source-table {
  border-collapse: separate;
  border-spacing: 0;
}

.source-table thead {
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .045);
}

.source-table th,
.source-table td {
  padding: 13px 12px;
}

.source-table tbody tr {
  transition: background-color .18s ease;
}

.source-table tbody tr:hover,
.source-table tbody tr.is-pending {
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .028);
}

.source-identity {
  display: grid;
  gap: 3px;
}

.source-key {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
}

.source-state.is-muted,
.search-state.is-muted {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .08);
}

.source-state.is-muted .state-dot,
.legend-dot.is-pending {
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .34);
}

.health-state.is-pending,
.health-state.is-unchecked,
.health-state.is-unknown,
.pending-time {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
}

@media (max-width: 900px) {
  .ad-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .ad-event {
    align-items: flex-start;
    flex-direction: column;
  }

  .ad-event-detail {
    justify-content: flex-start;
  }

  .health-overview {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .health-legend {
    flex-wrap: wrap;
  }
}

@media (max-width: 760px) {
  .lunatv-page {
    padding: 16px;
  }

  .health-progress-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }

  .ad-metrics {
    grid-template-columns: 1fr 1fr;
  }

  .ad-filter-actions {
    justify-content: flex-start;
  }

  .source-caption {
    white-space: normal;
  }
}

.tab-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 24px;
  height: 20px;
  margin-left: 8px;
  padding: 0 7px;
  border-radius: 999px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .72);
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .09);
  font-size: 11px;
  line-height: 1;
}

.tab-count-wide {
  min-width: 38px;
}

.page-tab.is-active .tab-count {
  color: rgb(var(--v-theme-on-primary, 255, 255, 255));
  background: rgba(var(--v-theme-on-primary, 255, 255, 255), .18);
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.overview-card {
  display: grid;
  gap: 8px;
  min-width: 0;
  padding: 15px 16px;
  border: 1px solid var(--ltv-border);
  border-radius: 14px;
  background: var(--ltv-surface);
  box-shadow: var(--ltv-shadow-soft);
  backdrop-filter: var(--ltv-blur);
}

.overview-card-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.overview-label,
.overview-card-meta {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 11px;
}

.overview-card > strong {
  min-width: 0;
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 22px;
  line-height: 1.2;
}

.overview-card > strong small {
  font-size: 12px;
  font-weight: 550;
}

.overview-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 14px !important;
}

.overview-status,
.summary-item,
.auto-refresh {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 999px;
  padding: 3px 8px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .68);
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .07);
  font-size: 10px;
  font-weight: 650;
  white-space: nowrap;
}

.overview-status.is-live,
.overview-status.is-good,
.summary-item.is-good {
  color: rgb(var(--v-theme-success, 76, 175, 80));
  background: rgba(var(--v-theme-success, 76, 175, 80), .13);
}

.overview-status.is-warning,
.summary-item.is-warning {
  color: rgb(var(--v-theme-warning, 251, 140, 0));
  background: rgba(var(--v-theme-warning, 251, 140, 0), .13);
}

.source-panel-heading,
.ad-page-heading {
  align-items: flex-start;
}

.source-health-summary {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 7px;
  flex-wrap: wrap;
}

.summary-item .legend-dot {
  width: 6px;
  height: 6px;
}

.ad-filter-panel {
  padding: 20px;
}

.ad-page-heading {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 18px;
}

.page-kicker {
  margin-bottom: 6px;
  color: rgb(var(--v-theme-primary, 139, 92, 246));
  font-size: 10px;
  font-weight: 750;
  letter-spacing: .14em;
}

.ad-page-heading h2 {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 7px;
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 20px;
  line-height: 1.25;
}

.ad-page-heading p {
  font-size: 12px;
  line-height: 1.6;
}

.ad-page-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.auto-refresh {
  padding: 5px 9px;
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgb(var(--v-theme-success, 76, 175, 80));
  box-shadow: 0 0 0 4px rgba(var(--v-theme-success, 76, 175, 80), .11);
}

.debug-switch {
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--ltv-border);
  border-radius: 8px;
  background: var(--ltv-surface-soft);
}

.source-action.is-danger {
  border-color: rgba(var(--v-theme-error, 244, 67, 54), .48);
  color: rgb(var(--v-theme-error, 244, 67, 54));
  background: rgba(var(--v-theme-error, 244, 67, 54), .08);
}

.ad-monitor-state {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  padding: 13px 14px;
  border: 1px solid var(--ltv-border);
  border-radius: 13px;
  background: var(--ltv-surface-soft);
}

.ad-monitor-state.is-active {
  border-color: rgba(var(--v-theme-success, 76, 175, 80), .28);
  background: linear-gradient(90deg, rgba(var(--v-theme-success, 76, 175, 80), .08), rgba(var(--v-theme-on-surface, 232, 231, 241), .02));
}

.ad-monitor-state.is-loading {
  opacity: .65;
}

.monitor-icon {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 11px;
  background: rgba(var(--v-theme-primary, 139, 92, 246), .12);
}

.monitor-icon i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .45);
}

.ad-monitor-state.is-active .monitor-icon {
  background: rgba(var(--v-theme-success, 76, 175, 80), .13);
}

.ad-monitor-state.is-active .monitor-icon i {
  background: rgb(var(--v-theme-success, 76, 175, 80));
  box-shadow: 0 0 0 5px rgba(var(--v-theme-success, 76, 175, 80), .12);
  animation: monitor-pulse 1.8s ease-in-out infinite;
}

.monitor-state-copy,
.monitor-state-meta {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.monitor-state-copy strong,
.monitor-state-meta strong {
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 13px;
}

.monitor-state-copy span,
.monitor-state-meta span {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 11px;
}

.monitor-state-meta {
  text-align: right;
}

.ad-metrics {
  gap: 12px;
  margin-bottom: 0;
}

.ad-metric {
  gap: 6px;
  padding: 15px 16px;
  border-radius: 13px;
}

.ad-metric strong {
  font-size: 22px;
}

.ad-metric strong small {
  font-size: 12px;
  font-weight: 550;
}

.ad-log-section {
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .10));
}

.ad-log-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.ad-log-heading h3 {
  margin: 0 0 3px;
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 15px;
}

.ad-log-heading > div > span {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), var(--v-medium-emphasis-opacity, .62));
  font-size: 11px;
}

.ad-log-controls {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.ad-filter-segments {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: 9px;
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .055);
}

.ad-filter-segments button {
  border: 0;
  border-radius: 7px;
  padding: 6px 9px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .66);
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}

.ad-filter-segments button.is-active {
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .10);
}

.ad-log-search {
  width: 172px;
  min-height: 32px;
  padding: 6px 10px;
  border: 1px solid rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .14));
  border-radius: 9px;
  outline: none;
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .035);
  font: inherit;
  font-size: 11px;
}

.ad-log-search::placeholder {
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .38);
}

.ad-log-search:focus {
  border-color: rgba(var(--v-theme-primary, 139, 92, 246), .62);
  box-shadow: 0 0 0 3px rgba(var(--v-theme-primary, 139, 92, 246), .10);
}

.ad-event-list {
  gap: 7px;
  max-height: 500px;
  padding-right: 4px;
}

.ad-event {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto;
  align-items: center;
  gap: 12px 20px;
  width: 100%;
  padding: 12px 13px;
  text-align: left;
  color: inherit;
  font: inherit;
  cursor: pointer;
  transition: border-color .16s ease, background-color .16s ease, transform .16s ease;
}

.ad-event:hover {
  border-color: rgba(var(--v-theme-primary, 139, 92, 246), .28);
  background: rgba(var(--v-theme-primary, 139, 92, 246), .035);
}

.ad-event.is-expanded {
  border-color: rgba(var(--v-theme-primary, 139, 92, 246), .34);
  background: rgba(var(--v-theme-primary, 139, 92, 246), .045);
}

.ad-event-detail {
  flex-wrap: nowrap;
}

.ad-event-chevron {
  min-width: 28px;
  color: rgb(var(--v-theme-primary, 139, 92, 246));
  text-align: right;
}

.ad-event-expanded {
  display: grid;
  grid-column: 1 / -1;
  gap: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(var(--v-border-color, 232, 231, 241), var(--v-border-opacity, .09));
}

.ad-event-expanded code {
  display: block;
  overflow-x: auto;
  padding: 10px 11px;
  border-radius: 9px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .86);
  background: rgba(0, 0, 0, .20);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  line-height: 1.55;
  white-space: nowrap;
}

.ad-breakdown {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.ad-breakdown span {
  padding: 4px 7px;
  border-radius: 7px;
  color: rgba(var(--v-theme-on-surface, 232, 231, 241), .62);
  background: rgba(var(--v-theme-on-surface, 232, 231, 241), .055);
  font-size: 10px;
}

.ad-empty-state {
  display: grid;
  place-items: center;
  gap: 6px;
  min-height: 180px;
  text-align: center;
}

.ad-empty-state strong {
  color: rgb(var(--v-theme-on-surface, 232, 231, 241));
  font-size: 14px;
}

.ad-empty-state span {
  font-size: 12px;
}

.help-heading {
  margin-bottom: 12px;
}

.page-tab:focus-visible,
.button:focus-visible,
.source-action:focus-visible,
.ad-filter-segments button:focus-visible,
.ad-event:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary, 139, 92, 246));
  outline-offset: 2px;
}

@keyframes monitor-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(.72); opacity: .68; }
}

@media (max-width: 1180px) {
  .overview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .ad-page-heading,
  .ad-log-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .ad-page-actions,
  .ad-log-controls {
    justify-content: flex-start;
  }
}

@media (max-width: 760px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }

  .source-health-summary {
    justify-content: flex-start;
  }

  .ad-filter-panel {
    padding: 16px;
  }

  .ad-page-heading h2 {
    align-items: flex-start;
    flex-direction: column;
  }

  .ad-monitor-state {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .monitor-state-meta {
    grid-column: 2;
    text-align: left;
  }

  .ad-log-controls,
  .ad-log-search {
    width: 100%;
  }

  .ad-filter-segments {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    width: 100%;
  }

  .ad-event {
    grid-template-columns: 1fr;
  }

  .ad-event-detail {
    justify-content: flex-start;
    overflow-x: auto;
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ad-monitor-state.is-active .monitor-icon i {
    animation: none;
  }
}
</style>
