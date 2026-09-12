import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,withCtx:_withCtx,toDisplayString:_toDisplayString,createTextVNode:_createTextVNode,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = { class: "pa-4" };
const _hoisted_2 = { class: "d-flex justify-end mt-4" };

const {onBeforeUnmount,onMounted,reactive,ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Config',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'LunaTVSource' },
  initialConfig: { type: Object, default: () => ({}) },
},
  emits: ['save', 'close'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;
const saving = ref(false);
const message = reactive({ text: '', type: 'info' });
let messageTimer = null;
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
};
const modeItems = [
  { title: '下载到本地并整理（去广告）', value: 'download' },
  { title: '生成 STRM（原始直链，不去广告）', value: 'strm' },
];
const config = reactive({ ...defaults });

function normalizeBoolean(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['false', '0', 'no', 'off'].includes(normalized)) return false
    if (['true', '1', 'yes', 'on'].includes(normalized)) return true
  }
  return Boolean(value)
}

function validateIntegerRange(value, label, min, max) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < min || number > max) {
    showMessage(`${label}需为 ${min} 到 ${max} 之间的整数`, 'error');
    return false
  }
  return true
}

function validateNumberRange(value, label, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) {
    showMessage(`${label}需为 ${min} 到 ${max} 之间的数字`, 'error');
    return false
  }
  return true
}

function validateProxy(value) {
  const raw = String(value || '').trim();
  if (!raw) return true
  try {
    const parsed = new URL(raw);
    if (!['http:', 'socks5:'].includes(parsed.protocol) || !parsed.hostname) {
      throw new Error('unsupported proxy')
    }
    const port = parsed.port ? Number(parsed.port) : 7890;
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('invalid proxy port')
    }
    return true
  } catch (_error) {
    showMessage('下载代理需填写 http:// 或 socks5:// 地址', 'error');
    return false
  }
}

function showMessage(text, type = 'info') {
  if (messageTimer !== null) clearTimeout(messageTimer);
  message.text = text;
  message.type = type;
  messageTimer = text
    ? setTimeout(() => {
      if (message.text === text) message.text = '';
      messageTimer = null;
    }, 3500)
    : null;
}

function unwrapApiResponse(response) {
  if (response?.success !== undefined) return response
  if (response?.data?.success !== undefined) return response.data
  return response
}

async function saveConfig() {
  if (typeof props.api?.put !== 'function') {
    showMessage('当前 MoviePilot 未提供配置保存接口', 'error');
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
    showMessage('任务并发数 × 分片线程数不能超过 64', 'error');
    return
  }
  saving.value = true;
  try {
    const mode = config.mode === 'strm' ? 'strm' : 'download';
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
    };
    const response = await props.api.put(
      `plugin/${props.pluginId || 'LunaTVSource'}`,
      payload,
      { feedback: 'silent' },
    );
    const result = unwrapApiResponse(response);
    if (result?.success === false) throw new Error(result.message || '保存配置失败')
    emit('save', payload);
    showMessage('配置已保存', 'success');
  } catch (error) {
    showMessage(error?.message || '保存配置失败', 'error');
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  Object.assign(config, defaults, props.initialConfig || {});
  for (const [key, fallback] of [
    ['enabled', false],
    ['debug_mode', false],
    ['generate_nfo', true],
    ['moviepilot_organize', true],
  ]) {
    config[key] = normalizeBoolean(config[key], fallback);
  }
  config.config_url = String(config.config_url || '').trim() || defaults.config_url;
  config.source_strategy = config.source_strategy === 'all' ? 'all' : 'first';
  config.ffmpeg_path = String(config.ffmpeg_path || '').trim() || 'ffmpeg';
  config.mediaserver_name = String(config.mediaserver_name || '').trim();
  config.mode = config.mode === 'strm' ? 'strm' : 'download';
});

onBeforeUnmount(() => {
  if (messageTimer !== null) clearTimeout(messageTimer);
  messageTimer = null;
});

return (_ctx, _cache) => {
  const _component_VIcon = _resolveComponent("VIcon");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VToolbar = _resolveComponent("VToolbar");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VSwitch = _resolveComponent("VSwitch");
  const _component_VCol = _resolveComponent("VCol");
  const _component_VSelect = _resolveComponent("VSelect");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VRow = _resolveComponent("VRow");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createVNode(_component_VToolbar, {
      density: "comfortable",
      color: "transparent",
      class: "px-0"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_VIcon, {
          icon: "mdi-play-network",
          color: "primary",
          class: "me-2"
        }),
        _cache[21] || (_cache[21] = _createElementVNode("div", { class: "text-h6" }, "LunaTV 原生桥接配置", -1)),
        _createVNode(_component_VSpacer),
        _createVNode(_component_VBtn, {
          icon: "mdi-content-save",
          variant: "text",
          color: "success",
          loading: saving.value,
          title: "保存配置",
          onClick: saveConfig
        }, null, 8, ["loading"]),
        _createVNode(_component_VBtn, {
          icon: "mdi-close",
          variant: "text",
          title: "关闭",
          onClick: _cache[0] || (_cache[0] = $event => (emit('close')))
        })
      ]),
      _: 1
    }),
    _createVNode(_component_VDivider, { class: "mb-4" }),
    (message.text)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 0,
          type: message.type,
          variant: "tonal",
          density: "compact",
          class: "mb-4"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(message.text), 1)
          ]),
          _: 1
        }, 8, ["type"]))
      : _createCommentVNode("", true),
    _createVNode(_component_VAlert, {
      type: "info",
      variant: "tonal",
      density: "compact",
      class: "mb-4"
    }, {
      default: _withCtx(() => [...(_cache[22] || (_cache[22] = [
        _createTextVNode(" 保存后，LunaTV/苹果 CMS 将接入 MoviePilot 的原生搜索、订阅与下载入口。绿联需要季集信息时请选择“下载到本地并整理”，并保持 NFO 与原生整理开启。 ", -1)
      ]))]),
      _: 1
    }),
    _createVNode(_component_VRow, { dense: "" }, {
      default: _withCtx(() => [
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSwitch, {
              modelValue: config.enabled,
              "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((config.enabled) = $event)),
              label: "启用原生桥接",
              color: "success",
              "hide-details": ""
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSwitch, {
              modelValue: config.debug_mode,
              "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((config.debug_mode) = $event)),
              label: "开启广告拦截调试模式",
              hint: "在插件日志和工作台显示每次 HLS 扫描、拦截片段与时长；测试完成后可关闭。",
              "persistent-hint": "",
              color: "warning"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSwitch, {
              modelValue: config.generate_nfo,
              "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((config.generate_nfo) = $event)),
              label: "生成 NFO 元数据",
              hint: "绿联兼容建议开启；原生整理会生成 tvshow.nfo、season.nfo 和单集同名 NFO。",
              "persistent-hint": "",
              color: "success"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSwitch, {
              modelValue: config.moviepilot_organize,
              "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((config.moviepilot_organize) = $event)),
              label: "下载后调用 MoviePilot 整理链",
              hint: "绿联兼容必须开启；NFO 会写入最终媒体库目录。",
              "persistent-hint": "",
              color: "success"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        (config.mode === 'strm')
          ? (_openBlock(), _createBlock(_component_VCol, {
              key: 0,
              cols: "12"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_VAlert, {
                  type: "warning",
                  variant: "tonal",
                  density: "compact"
                }, {
                  default: _withCtx(() => [...(_cache[23] || (_cache[23] = [
                    _createTextVNode(" STRM 只保存原始直链，不经过原生整理，也不会生成 NFO；需要绿联正确显示季集时请选择本地下载并整理。 ", -1)
                  ]))]),
                  _: 1
                })
              ]),
              _: 1
            }))
          : (config.generate_nfo && !config.moviepilot_organize)
            ? (_openBlock(), _createBlock(_component_VCol, {
                key: 1,
                cols: "12"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_VAlert, {
                    type: "warning",
                    variant: "tonal",
                    density: "compact"
                  }, {
                    default: _withCtx(() => [...(_cache[24] || (_cache[24] = [
                      _createTextVNode(" 当前关闭了原生整理，NFO 不会写入最终媒体库；绿联可能无法显示季号。 ", -1)
                    ]))]),
                    _: 1
                  })
                ]),
                _: 1
              }))
            : (!config.generate_nfo)
              ? (_openBlock(), _createBlock(_component_VCol, {
                  key: 2,
                  cols: "12"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_VAlert, {
                      type: "warning",
                      variant: "tonal",
                      density: "compact"
                    }, {
                      default: _withCtx(() => [...(_cache[25] || (_cache[25] = [
                        _createTextVNode(" 当前关闭了 NFO 生成；绿联可能无法显示季号。打开后，新下载会生成季集元数据；已有错误条目需先让 MoviePilot 覆盖旧 NFO，再在绿联完整覆盖或重新识别。 ", -1)
                      ]))]),
                      _: 1
                    })
                  ]),
                  _: 1
                }))
              : _createCommentVNode("", true),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSelect, {
              modelValue: config.mode,
              "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((config.mode) = $event)),
              items: modeItems,
              label: "处理方式",
              hint: "本地下载模式会执行 HLS 广告分片过滤并由原生整理生成 NFO；STRM 保留原始直链。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.config_url,
              "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((config.config_url) = $event)),
              label: "LunaTV 配置地址",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.source_allowlist,
              "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((config.source_allowlist) = $event)),
              label: "启用资源站（可选）",
              placeholder: "留空允许配置中的全部来源",
              hint: "填写来源 key，使用逗号分隔。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VSelect, {
              modelValue: config.source_strategy,
              "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((config.source_strategy) = $event)),
              items: [
            { title: '按配置顺序选一个（推荐）', value: 'first' },
            { title: '所有匹配源都排队', value: 'all' },
          ],
              label: "订阅资源站策略",
              hint: "默认每集选择一个可用来源；需要多源备份时才选择全部排队。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.hls_ad_filter_regex,
              "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((config.hls_ad_filter_regex) = $event)),
              label: "HLS 广告分片 URL 正则（可选）",
              placeholder: "例如 adjump|redtraffic|/ad/",
              hint: "默认过滤常见广告路径；留空则只删除闭合 CUE-OUT/CUE-IN 标记区间。不要用单独的 DISCONTINUITY 作为删除条件。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.probe_allowed_private_ranges,
              "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => ((config.probe_allowed_private_ranges) = $event)),
              label: "可信网络 CIDR（可选）",
              placeholder: "例如 198.18.0.0/15",
              hint: "默认拒绝私网配置、CMS 和媒体地址；Fake-IP 或可信内网环境才填写。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.download_root,
              "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((config.download_root) = $event)),
              label: "下载目录（可留空）",
              placeholder: "留空自动选择",
              hint: "填写后优先使用；留空时依次使用 MoviePilot 传入目录、订阅保存目录、按媒体类型的本地下载目录。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.download_proxy,
              "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((config.download_proxy) = $event)),
              label: "下载代理（可选）",
              placeholder: "http://192.168.1.2:7890 或 socks5://192.168.1.2:7890",
              hint: "仅代理媒体分片和 N_m3u8DL-RE 的 GitHub 下载；留空直连。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.ffmpeg_path,
              "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((config.ffmpeg_path) = $event)),
              label: "ffmpeg 路径",
              placeholder: "ffmpeg",
              hint: "通常保持默认值；只有容器内的 ffmpeg 不在 PATH 时才填写绝对路径。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.max_concurrent_tasks,
              "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((config.max_concurrent_tasks) = $event)),
              label: "最大任务并发数",
              type: "number",
              min: "1",
              max: "4",
              step: "1",
              hint: "范围 1–4，默认 2。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.poll_minutes,
              "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((config.poll_minutes) = $event)),
              label: "订阅刷新间隔（分钟）",
              type: "number",
              min: "5",
              max: "1440",
              step: "1",
              hint: "范围 5–1440；默认 30。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.queue_minutes,
              "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((config.queue_minutes) = $event)),
              label: "队列间隔（分钟）",
              type: "number",
              min: "1",
              max: "1440",
              step: "1",
              hint: "范围 1–1440；默认 1。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.request_timeout,
              "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((config.request_timeout) = $event)),
              label: "请求超时（秒）",
              type: "number",
              min: "1",
              max: "60",
              step: "0.5",
              hint: "范围 1–60 秒；默认 15。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.source_check_minutes,
              "onUpdate:modelValue": _cache[18] || (_cache[18] = $event => ((config.source_check_minutes) = $event)),
              label: "来源健康检查间隔（分钟）",
              type: "number",
              min: "15",
              max: "1440",
              step: "1",
              hint: "范围 15–1440，默认 60。打开插件页仅读取缓存，定时任务才会执行健康检查。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, {
          cols: "12",
          md: "6"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.segment_thread_count,
              "onUpdate:modelValue": _cache[19] || (_cache[19] = $event => ((config.segment_thread_count) = $event)),
              label: "分片线程数",
              type: "number",
              min: "4",
              max: "32",
              step: "1",
              hint: "范围 4–32，默认 16；与任务并发数相乘不能超过 64。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        }),
        _createVNode(_component_VCol, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_VTextField, {
              modelValue: config.mediaserver_name,
              "onUpdate:modelValue": _cache[20] || (_cache[20] = $event => ((config.mediaserver_name) = $event)),
              label: "完成后刷新媒体服务器（可选）",
              placeholder: "留空刷新所有已启用服务器，例如 Emby",
              hint: "仅控制下载完成后的同步目标，播放仍在 Emby/Jellyfin 页面完成。",
              "persistent-hint": "",
              variant: "outlined"
            }, null, 8, ["modelValue"])
          ]),
          _: 1
        })
      ]),
      _: 1
    }),
    _createVNode(_component_VAlert, {
      type: "warning",
      variant: "tonal",
      density: "compact",
      class: "mt-3"
    }, {
      default: _withCtx(() => [...(_cache[26] || (_cache[26] = [
        _createTextVNode(" DeepSeek、TMDB、整理规则和链接权限沿用 MoviePilot 全局设置；下载目录留空时复用宿主目录。默认 2 个任务、每任务 16 个分片线程，总分片并发限制为 64；遇到 429、超时或磁盘繁忙时请调低。 ", -1)
      ]))]),
      _: 1
    }),
    _createElementVNode("div", _hoisted_2, [
      _createVNode(_component_VBtn, {
        color: "primary",
        loading: saving.value,
        onClick: saveConfig
      }, {
        default: _withCtx(() => [...(_cache[27] || (_cache[27] = [
          _createTextVNode("保存配置", -1)
        ]))]),
        _: 1
      }, 8, ["loading"])
    ])
  ]))
}
}

};

export { _sfc_main as default };
