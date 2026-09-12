# LunaTVSource

MoviePilot V3 的 LunaTV/MoonTV 苹果 CMS 资源插件。

## 运行边界

- 插件主类是 `LunaTVSource`，目录名必须保持为 `lunatvsource`，市场索引位于仓库根目录的 `package.v3.json`。
- 配置、运行状态和缓存使用 MoviePilot 插件基类提供的接口；不要把运行数据写回插件源码目录。
- 目录、智能助手、TMDB 关联、媒体识别、整理规则和链接权限由 MoviePilot 全局设置负责。插件配置只保存 LunaTV 来源、下载模式、并发、可选目录覆盖和媒体服务器刷新目标。
- `use_moviepilot_dirs`、`ai_enabled`、`tmdb_association`、`native_recognize` 是历史配置键，保留读取兼容但不再作为独立开关；这样升级后不会出现“界面关闭、运行仍开启”的误导。

## 绿联媒体库

需要季集信息时使用“下载到本地并整理”，保持“生成 NFO 元数据”和“下载后调用 MoviePilot 整理链”开启。成功整理后，目标目录应包含 `tvshow.nfo`、`season.nfo` 和单集同名 NFO。STRM 模式只保存原始直链，不生成这些本地元数据。

已有错误条目不会由插件自动迁移。应先让 MoviePilot 覆盖旧 NFO，必要时备份并删除旧的 `tvshow.nfo`、`season.nfo` 和单集 NFO，再在绿联中执行完整覆盖或重新识别。

## 宿主兼容

插件优先使用 V3 SDK 和公开 Chain 接口；较早宿主没有统一目录设置 SDK 时，才回退读取 `DirectoryHelper`。该兼容导入与媒体识别导入相互隔离，单个旧路径缺失不会让整个插件失效。

Vue 联邦组件使用宿主注入的 `api`，按 V3 规则优先读取最终 endpoint payload，同时兼容旧宿主的 Axios-like `data` 外壳；不要在组件内再创建独立 HTTP 客户端。

搜索和下载还包含可恢复的宿主兼容桥接，以适配部分早期 V3 调度路径。桥接在插件停用或重载时释放，但它属于进程级排他资源，当前不支持同一宿主内创建多个 LunaTVSource 虚拟分身。

插件动态 API 明确注册 `schemas.Response[...]` 响应模型；在真实 V3 宿主中由响应模型承载 `success`、`message` 和 `data`，脱离宿主运行的单元测试才使用等价字典。新增接口时应继续保持这三个顶层字段，不要把业务字段放到 `message`，并为新增 `data` 结构补充具体 DTO。

## 支持矩阵与已知边界

- MoviePilot：V3，清单声明最低 `3.0.0`；实际发布前仍需在目标 MoviePilot 实例中完成一次加载、保存配置、原生搜索、下载和停用回收验证。
- 运行平台：插件逻辑可在宿主 Python 环境运行；随包提供的 N_m3u8DL-RE 受管二进制目前只覆盖 Linux x86_64 和 aarch64，其他平台需由宿主提供兼容执行环境。
- 兼容导入：`app.application.directory` 和 `app.api.endpoints.download` 只作为早期 V3 的回退桥接，新功能应优先使用 `app.sdk`、`app.chain.*` 和 `app.db.oper.*` 的公开入口。
- 网络例外：CMS、HLS 和内置下载器保留插件自有的标准库传输层，用于逐跳 DNS 固定、私网/CIDR 白名单、重定向校验和 SOCKS5；新增普通 MoviePilot HTTP 调用时仍应使用 `app.sdk.network`，不要把这套安全传输直接替换掉。
- 虚拟分身：搜索/下载桥接和固定的 LunaTV 下载器标识仍是进程级资源，因此不要在同一宿主内创建多个本插件分身；这是当前明确限制，不应被误认为已支持多实例隔离。
- 响应 DTO：现有 CMS 搜索、状态和任务数据字段较动态，响应 envelope 已标准化，但部分 `data` 仍使用兼容性的字典形状。后续新增或稳定字段时，应优先抽成 Pydantic DTO，并同步更新 OpenAPI 响应模型。
- 测试：测试位于 `tests/v3/lunatvsource/`，并按生产路径 `app.plugins.lunatvsource` 加载插件；不要把测试放进插件源码目录，否则市场同步时可能把测试一并复制到运行目录。
- 多语言：当前插件专用页面文案以中文为主，尚未提供独立语言资源；宿主只会统一处理标准 API envelope 的反馈，后续新增语言时应把界面文案抽到插件自己的资源文件。

## 开发检查

在仓库根目录执行：

```bash
python3 -m pytest tests/v3/lunatvsource
python3 -m compileall plugins.v3/lunatvsource
cd plugins.v3/lunatvsource && npm ci && npm run build
cd ../..
python3 .github/scripts/check_federation_css.py
git diff --check
```

`dist` 是发布内容的一部分，前端源码修改后必须重新构建并提交生成结果。联邦组件使用宿主提供的 Vue/Vuetify，不能发布 `__federation_shared_vuetify/styles-*.css` 或未限定作用域的全局 Vuetify 选择器。

## 维护者清单

1. 修改 Python 或配置合同后运行 `python3 -m pytest tests/v3/lunatvsource` 和 `python3 -m compileall plugins.v3/lunatvsource`。
2. 修改 Vue 后在 `plugins.v3/lunatvsource` 执行 `npm ci`、`npm run build`，再运行联邦 CSS 门禁，并把新的 `dist` 产物一起提交。
3. 发布时同步 `LunaTVSource.plugin_version`、`package.v3.json`、插件 `package.json`/`package-lock.json` 和 `history` 顶部记录；不要把版本号写死在测试里。
4. 发布前在真实 MoviePilot V3 宿主验证安装、启动、保存配置、重载、停用、原生搜索、下载入队、停止任务和媒体库可见性；单元测试不能替代这一步。
5. 新增宿主调用优先使用 `app.sdk`、`app.chain.*` 或 `app.db.oper.*`；兼容旧宿主的内部导入必须单独隔离，并且不得把异常原文返回到 API `message` 或状态字段。

参考：

- [MoviePilot V3 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)
- [MoviePilot V3 API 响应适配](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V3_API_Response_Adaptation.md)
- [仓库根目录 README](../../README.md)
