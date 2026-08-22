# LunaTV 资源订阅（MoviePilot V3）

独立的 MoviePilot V3 插件，读取 MoonTV/LunaTV 的 `api_site` 配置，接入标准苹果 CMS V10 资源站。

## 第一版范围

- 读取可配置的 LunaTV JSON 配置地址；默认使用个人可维护的配置仓库。
- 资源站按 allowlist 启用，单个源失败不阻塞其它源。
- 使用 MoviePilot 活跃订阅名称搜索资源。
- 任务严格串行执行，不启动并行下载。
- 支持 m3u8/HTTP 资源下载到指定目录，或生成 `.strm`。
- 下载完成后使用稳定的电影/电视剧命名结构，并保留插件内任务历史。
- 任务通知通过 MoviePilot 插件消息能力发送；MoviePilot 的媒体库扫描可以继续接管已完成文件。

## 目录与命名

下载目录由你在插件设置中指定。插件不会改写 MoviePilot 的目录规则；建议将它配置为 MoviePilot 已映射的入库目录，避免下载完成后媒体库看不到文件。

```text
电影名 (年份)/电影名 (年份).mp4
剧名 (年份)/Season 01/剧名 (年份) - S01E01.mp4
```

下载先写入 `.part`，成功后再改为正式文件名，避免媒体库扫描到半成品。

同一任务只会进入一次队列；队列每次只执行一个任务，不会并行下载。详情接口缺少播放地址时，插件会自动补查 Apple CMS `ac=detail`，并识别 `S01E01`、`第 8 集`、`第 1 季` 等标记。

## 配置

默认配置地址：

```text
https://raw.githubusercontent.com/hafrey1/LunaTV-config/main/LunaTV-config.json
```

建议先只启用自己确认可用、且有权访问的资源源。插件不会绕过 DRM、登录保护或付费限制。

使用顺序：保存配置并启用插件 → 配置下载目录和处理方式 → 在 MoviePilot 中创建或修改订阅 → 点击“刷新订阅”；也可以在插件工作台搜索后单集加入队列。下载目录必须是容器内路径，不能填宿主机未映射的路径。

## 开发检查

```bash
python3 -m pytest tests
python3 -m compileall plugins.v3/lunatvsource
git diff --check
```
