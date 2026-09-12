<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'LunaTVSource' },
  initialConfig: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['save', 'close'])
const saving = ref(false)
const message = reactive({ text: '', type: 'info' })
let messageTimer = null
const defaults = {
  enabled: false,
  debug_mode: false,
  generate_nfo: true,
  config_url: 'https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/LunaTV-config.json',
  source_allowlist: '',
  probe_allowed_private_ranges: '',
  hls_ad_filter_regex: '(?i)(?:adjump|redtraffic|alimama|chenggao|laomaotao|[/_.-](?:ad|ads|advert|advertisement|promo|sponsor)[/_.-])',
  mode: 'download',
  source_strategy: 'first',
  download_root: '',
  download_proxy: '',
  ffmpeg_path: 'ffmpeg',
  poll_minutes: 30,
  queue_minutes: 1,
  request_timeout: 15,
  moviepilot_organize: true,
  mediaserver_name: '',
  max_concurrent_tasks: 2,
  segment_thread_count: 16,
  source_check_minutes: 60,
}
const modeItems = [
  { title: '下载到本地并整理（去广告）', value: 'download' },
  { title: '生成 STRM（原始直链，不去广告）', value: 'strm' },
]
const config = reactive({ ...defaults })

function normalizeBoolean(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (['false', '0', 'no', 'off'].includes(normalized)) return false
    if (['true', '1', 'yes', 'on'].includes(normalized)) return true
  }
  return Boolean(value)
}

function validateIntegerRange(value, label, min, max) {
  const number = Number(value)
  if (!Number.isInteger(number) || number < min || number > max) {
    showMessage(`${label}需为 ${min} 到 ${max} 之间的整数`, 'error')
    return false
  }
  return true
}

function validateNumberRange(value, label, min, max) {
  const number = Number(value)
  if (!Number.isFinite(number) || number < min || number > max) {
    showMessage(`${label}需为 ${min} 到 ${max} 之间的数字`, 'error')
    return false
  }
  return true
}

function validateProxy(value) {
  const raw = String(value || '').trim()
  if (!raw) return true
  try {
    const parsed = new URL(raw)
    if (!['http:', 'socks5:'].includes(parsed.protocol) || !parsed.hostname) {
      throw new Error('unsupported proxy')
    }
    const port = parsed.port ? Number(parsed.port) : 7890
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('invalid proxy port')
    }
    return true
  } catch (_error) {
    showMessage('下载代理需填写 http:// 或 socks5:// 地址', 'error')
    return false
  }
}

function showMessage(text, type = 'info') {
  if (messageTimer !== null) clearTimeout(messageTimer)
  message.text = text
  message.type = type
  messageTimer = text
    ? setTimeout(() => {
      if (message.text === text) message.text = ''
      messageTimer = null
    }, 3500)
    : null
}

function unwrapApiResponse(response) {
  if (response?.success !== undefined) return response
  if (response?.data?.success !== undefined) return response.data
  return response
}

async function saveConfig() {
  if (typeof props.api?.put !== 'function') {
    showMessage('当前 MoviePilot 未提供配置保存接口', 'error')
    return
  }
  if (!validateIntegerRange(config.max_concurrent_tasks, '任务并发数', 1, 4)
    || !validateIntegerRange(config.segment_thread_count, '分片线程数', 4, 32)
    || !validateIntegerRange(config.source_check_minutes, '来源健康检查间隔', 15, 1440)
    || !validateIntegerRange(config.poll_minutes, '订阅刷新间隔', 5, 1440)
    || !validateIntegerRange(config.queue_minutes, '队列间隔', 1, 1440)
    || !validateNumberRange(config.request_timeout, '请求超时', 1, 60)
    || !validateProxy(config.download_proxy)) return
  if (Number(config.max_concurrent_tasks) * Number(config.segment_thread_count) > 64) {
    showMessage('任务并发数 × 分片线程数不能超过 64', 'error')
    return
  }
  saving.value = true
  try {
    const mode = config.mode === 'strm' ? 'strm' : 'download'
    const payload = {
      enabled: Boolean(config.enabled),
      debug_mode: Boolean(config.debug_mode),
      generate_nfo: Boolean(config.generate_nfo),
      config_url: String(config.config_url || '').trim() || defaults.config_url,
      source_allowlist: String(config.source_allowlist || '').trim(),
      probe_allowed_private_ranges: String(config.probe_allowed_private_ranges || '').trim(),
      hls_ad_filter_regex: String(config.hls_ad_filter_regex || '').trim(),
      mode,
      source_strategy: config.source_strategy === 'all' ? 'all' : 'first',
      download_root: String(config.download_root || '').trim(),
      download_proxy: String(config.download_proxy || '').trim(),
      ffmpeg_path: String(config.ffmpeg_path || '').trim() || 'ffmpeg',
      moviepilot_organize: Boolean(config.moviepilot_organize),
      mediaserver_name: String(config.mediaserver_name || '').trim(),
      poll_minutes: Number(config.poll_minutes),
      queue_minutes: Number(config.queue_minutes),
      request_timeout: Number(config.request_timeout),
      max_concurrent_tasks: Number(config.max_concurrent_tasks),
      segment_thread_count: Number(config.segment_thread_count),
      source_check_minutes: Number(config.source_check_minutes),
    }
    const response = await props.api.put(
      `plugin/${props.pluginId || 'LunaTVSource'}`,
      payload,
      { feedback: 'silent' },
    )
    const result = unwrapApiResponse(response)
    if (result?.success === false) throw new Error(result.message || '保存配置失败')
    emit('save', payload)
    showMessage('配置已保存', 'success')
  } catch (error) {
    showMessage(error?.message || '保存配置失败', 'error')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  Object.assign(config, defaults, props.initialConfig || {})
  for (const [key, fallback] of [
    ['enabled', false],
    ['debug_mode', false],
    ['generate_nfo', true],
    ['moviepilot_organize', true],
  ]) {
    config[key] = normalizeBoolean(config[key], fallback)
  }
  config.config_url = String(config.config_url || '').trim() || defaults.config_url
  config.source_strategy = config.source_strategy === 'all' ? 'all' : 'first'
  config.ffmpeg_path = String(config.ffmpeg_path || '').trim() || 'ffmpeg'
  config.mediaserver_name = String(config.mediaserver_name || '').trim()
  config.mode = config.mode === 'strm' ? 'strm' : 'download'
})

onBeforeUnmount(() => {
  if (messageTimer !== null) clearTimeout(messageTimer)
  messageTimer = null
})
</script>

<template>
  <div class="pa-4">
    <VToolbar density="comfortable" color="transparent" class="px-0">
      <VIcon icon="mdi-play-network" color="primary" class="me-2" />
      <div class="text-h6">LunaTV 原生桥接配置</div>
      <VSpacer />
      <VBtn icon="mdi-content-save" variant="text" color="success" :loading="saving" title="保存配置" @click="saveConfig" />
      <VBtn icon="mdi-close" variant="text" title="关闭" @click="emit('close')" />
    </VToolbar>
    <VDivider class="mb-4" />
    <VAlert v-if="message.text" :type="message.type" variant="tonal" density="compact" class="mb-4">{{ message.text }}</VAlert>
    <VAlert type="info" variant="tonal" density="compact" class="mb-4">
      保存后，LunaTV/苹果 CMS 将接入 MoviePilot 的原生搜索、订阅与下载入口。绿联需要季集信息时请选择“下载到本地并整理”，并保持 NFO 与原生整理开启。
    </VAlert>
    <VRow dense>
      <VCol cols="12"><VSwitch v-model="config.enabled" label="启用原生桥接" color="success" hide-details /></VCol>
      <VCol cols="12">
        <VSwitch
          v-model="config.debug_mode"
          label="开启广告拦截调试模式"
          hint="在插件日志和工作台显示每次 HLS 扫描、拦截片段与时长；测试完成后可关闭。"
          persistent-hint
          color="warning"
        />
      </VCol>
      <VCol cols="12">
        <VSwitch
          v-model="config.generate_nfo"
          label="生成 NFO 元数据"
          hint="绿联兼容建议开启；原生整理会生成 tvshow.nfo、season.nfo 和单集同名 NFO。"
          persistent-hint
          color="success"
        />
      </VCol>
      <VCol cols="12">
        <VSwitch
          v-model="config.moviepilot_organize"
          label="下载后调用 MoviePilot 整理链"
          hint="绿联兼容必须开启；NFO 会写入最终媒体库目录。"
          persistent-hint
          color="success"
        />
      </VCol>
      <VCol v-if="config.mode === 'strm'" cols="12">
        <VAlert type="warning" variant="tonal" density="compact">
          STRM 只保存原始直链，不经过原生整理，也不会生成 NFO；需要绿联正确显示季集时请选择本地下载并整理。
        </VAlert>
      </VCol>
      <VCol v-else-if="config.generate_nfo && !config.moviepilot_organize" cols="12">
        <VAlert type="warning" variant="tonal" density="compact">
          当前关闭了原生整理，NFO 不会写入最终媒体库；绿联可能无法显示季号。
        </VAlert>
      </VCol>
      <VCol v-else-if="!config.generate_nfo" cols="12">
        <VAlert type="warning" variant="tonal" density="compact">
          当前关闭了 NFO 生成；绿联可能无法显示季号。打开后，新下载会生成季集元数据；已有错误条目需先让 MoviePilot 覆盖旧 NFO，再在绿联完整覆盖或重新识别。
        </VAlert>
      </VCol>
      <VCol cols="12">
        <VSelect
          v-model="config.mode"
          :items="modeItems"
          label="处理方式"
          hint="本地下载模式会执行 HLS 广告分片过滤并由原生整理生成 NFO；STRM 保留原始直链。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12"><VTextField v-model="config.config_url" label="LunaTV 配置地址" variant="outlined" /></VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.source_allowlist"
          label="启用资源站（可选）"
          placeholder="留空允许配置中的全部来源"
          hint="填写来源 key，使用逗号分隔。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VSelect
          v-model="config.source_strategy"
          :items="[
            { title: '按配置顺序选一个（推荐）', value: 'first' },
            { title: '所有匹配源都排队', value: 'all' },
          ]"
          label="订阅资源站策略"
          hint="默认每集选择一个可用来源；需要多源备份时才选择全部排队。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.hls_ad_filter_regex"
          label="HLS 广告分片 URL 正则（可选）"
          placeholder="例如 adjump|redtraffic|/ad/"
          hint="默认过滤常见广告路径；留空则只删除闭合 CUE-OUT/CUE-IN 标记区间。不要用单独的 DISCONTINUITY 作为删除条件。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.probe_allowed_private_ranges"
          label="可信网络 CIDR（可选）"
          placeholder="例如 198.18.0.0/15"
          hint="默认拒绝私网配置、CMS 和媒体地址；Fake-IP 或可信内网环境才填写。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.download_root"
          label="下载目录（可留空）"
          placeholder="留空自动选择"
          hint="填写后优先使用；留空时依次使用 MoviePilot 传入目录、订阅保存目录、按媒体类型的本地下载目录。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.download_proxy"
          label="下载代理（可选）"
          placeholder="http://192.168.1.2:7890 或 socks5://192.168.1.2:7890"
          hint="仅代理媒体分片和 N_m3u8DL-RE 的 GitHub 下载；留空直连。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.ffmpeg_path"
          label="ffmpeg 路径"
          placeholder="ffmpeg"
          hint="通常保持默认值；只有容器内的 ffmpeg 不在 PATH 时才填写绝对路径。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.max_concurrent_tasks"
          label="最大任务并发数"
          type="number"
          min="1"
          max="4"
          step="1"
          hint="范围 1–4，默认 2。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.poll_minutes"
          label="订阅刷新间隔（分钟）"
          type="number"
          min="5"
          max="1440"
          step="1"
          hint="范围 5–1440；默认 30。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.queue_minutes"
          label="队列间隔（分钟）"
          type="number"
          min="1"
          max="1440"
          step="1"
          hint="范围 1–1440；默认 1。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.request_timeout"
          label="请求超时（秒）"
          type="number"
          min="1"
          max="60"
          step="0.5"
          hint="范围 1–60 秒；默认 15。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.source_check_minutes"
          label="来源健康检查间隔（分钟）"
          type="number"
          min="15"
          max="1440"
          step="1"
          hint="范围 15–1440，默认 60。打开插件页仅读取缓存，定时任务才会执行健康检查。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12" md="6">
        <VTextField
          v-model="config.segment_thread_count"
          label="分片线程数"
          type="number"
          min="4"
          max="32"
          step="1"
          hint="范围 4–32，默认 16；与任务并发数相乘不能超过 64。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
      <VCol cols="12">
        <VTextField
          v-model="config.mediaserver_name"
          label="完成后刷新媒体服务器（可选）"
          placeholder="留空刷新所有已启用服务器，例如 Emby"
          hint="仅控制下载完成后的同步目标，播放仍在 Emby/Jellyfin 页面完成。"
          persistent-hint
          variant="outlined"
        />
      </VCol>
    </VRow>
    <VAlert type="warning" variant="tonal" density="compact" class="mt-3">
      DeepSeek、TMDB、整理规则和链接权限沿用 MoviePilot 全局设置；下载目录留空时复用宿主目录。默认 2 个任务、每任务 16 个分片线程，总分片并发限制为 64；遇到 429、超时或磁盘繁忙时请调低。
    </VAlert>
    <div class="d-flex justify-end mt-4"><VBtn color="primary" :loading="saving" @click="saveConfig">保存配置</VBtn></div>
  </div>
</template>
