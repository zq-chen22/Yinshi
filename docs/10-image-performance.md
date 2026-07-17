# 图像任务性能与代理治理

本章区分两类经常混为一谈的“生图很慢”：一类是图像模型本身生成当前图片所需的时间；
另一类是图片、截图和生成结果持续写入同一 Codex thread 后，连后续纯文本 turn 也越来越慢。
前者可以通过尺寸、质量和模型选择改善；后者必须通过隔离线程、缩小视觉输入和避免重复查看
治理，单纯等待或升级小版本通常无效。

## 官方行为边界

- OpenAI 的[图像生成指南](https://developers.openai.com/api/docs/guides/image-generation#limitations)
  说明复杂提示可能需要约两分钟；尺寸、质量和渲染所需输出 token 都会影响延迟，低质量草稿
  和 JPEG 通常更快。
- [视觉输入指南](https://developers.openai.com/api/docs/guides/images-vision#calculating-costs)
  说明图片输入按 token 计量。GPT-5.6 的 `auto`/`original` 会按原始 patch 数处理，较大的图片
  会增加输入 token 和延迟；需要控时应物理缩小图片，或明确选择较低 detail。
- [部署检查表](https://developers.openai.com/api/docs/guides/deployment-checklist#set-image-detail-intentionally)
  也要求显式选择图片 detail，并在不需要坐标、密集 OCR 或细小图表时缩小图片。
- Responses 图像工具会把生成结果作为会话 item 返回；多轮调用还会继续携带前序上下文，见
  [图像生成工具指南](https://developers.openai.com/api/docs/guides/tools-image-generation)。
- [Computer Use 指南](https://developers.openai.com/api/docs/guides/tools-computer-use)
  的标准循环会在动作后捕获新截图并回传。截图过多时应先下采样；网页状态轮询不能无界地
  把连续帧加入长期历史。

安装版本 `codex-cli 0.144.3` 的官方源码还给出两个重要限制：

- [历史管理器](https://github.com/openai/codex/blob/rust-v0.144.3/codex-rs/core/src/context_manager/history.rs)
  会保留消息和 ImageGenerationCall；
- [输出截断](https://github.com/openai/codex/blob/rust-v0.144.3/codex-rs/utils/output-truncation/src/lib.rs)
  不会像文本那样截断 InputImage；

官方 Codex issue tracker 中也有方向一致的用户报告，例如
[#19936](https://github.com/openai/codex/issues/19936) 报告连续图片后出现 30–60 秒冻结，
该 issue 是用户现场而不是 OpenAI 的根因确认；本项目的结论仍以本机 rollout、计时和网络
指标为准。

## 本机受控实验

2026-07-16 在新的短生命周期 thread 中，使用同一模型、服务层和只回复 `TEST_OK` 的文本任务，
逐步增加一张输入图片。`wall` 是端到端时间，`turn` 是服务端 turn 时间：

| 输入 | rollout 中图片字节 | wall | turn | 本轮 input tokens |
|---|---:|---:|---:|---:|
| 纯文本 | 0 | 7.85 秒 | 2.86 秒 | 12,545 |
| 25 KB JPEG | 32,959 | 5.39 秒 | 3.86 秒 | 12,487 |
| 314 KB JPEG | 427,639 | 15.91 秒 | 10.65 秒 | 14,309 |
| 5.55 MB 原 PNG | 4,900,294 | 101.95 秒 | 100.60 秒 | 15,370 |

这组结果说明实际序列化和上传字节会显著影响延迟，不能只看 token 计数。对同一大图调用
`view_image(detail="high")` 时，工具约 0.3 秒返回 4.90 MB 数据，但随后等待 317 秒并经历两次
WebSocket 在 `response.completed` 前关闭；同一图片的 `high` 和 `original` 在该版本返回的
图像字节完全相同。把图片物理缩小为 314 KB 后，`view_image` 在 11.42 秒完成。

2026-07-17 又用 Codex CLI 0.144.3、全新 ephemeral thread、相同 low-effort `TEST_OK` 提示
复核了一次。5.56 MB、1500×2100 PNG 的两次 wall 为 18.89/23.40 秒；物理缩到
192,728 B、732×1024 JPEG 后为 13.53/11.80 秒。均值从 21.15 降到 12.67 秒（约快 1.67×），
进程最大 RSS 也从约 199 MB 降到约 121 MB。两组实验绝对耗时受网络和服务负载影响，但方向
一致，而且实验未改变 Codex 的上下文生命周期，因此可以单独归因到图片载荷治理。

持久效应也能复现：纯文本基线 thread 的下一轮为 8.69 秒、25,123 input tokens；只加入一次
4.90 MB `view_image` 的 thread，下一条纯文本已变为 11.60 秒、43,392 input tokens。真实原画
thread 更极端：rollout 为 52,351,041 字节，约 98.9% 是图片字符串，4 个 turn 含 18 个图片块。

本机旧任务还能把生成时间和后续停顿分开：原生图片实际生成通常约 2–6 分钟，但结果返回后
还出现 12–65 分钟空档。这不是图像模型继续渲染，而是超大历史、连接重试和再次查看生成图
共同造成的长尾。

## 已确认的根因

1. **生成服务有固有延迟。** 高质量、大尺寸、复杂编辑的当前 turn 本来就可能需要分钟级。
2. **视觉工具把图片写入历史。** `view_image` 会持久化完整 data URL；降低 detail 影响模型如何
   理解图片，但在本机版本中不会缩小 rollout 里的 Base64。必须先物理缩放、裁切和压缩。
3. **原生生成结果被重复加载。** 原生 imagegen 的结果已经返回发起线程；随后整图
   `view_image` 会再次加入同一素材。
4. **网页轮询会放大问题。** ChatGPT 网页操作本身不必然慢；只用 DOM/标题/文件哈希轮询的
   本机任务约 5.5 分钟正常完成。把每个网页状态截图交给视觉模型后才进入同一污染路径。
5. **大请求对连接异常更敏感。** 目标连接曾出现约 301 KB Send-Q、约 2.48 MB 重传及长时间
   无接收；几十 MB 历史使一次短暂网络问题变成长时间重连和软卡死。
6. **桥接器不应改变官方上下文生命周期。** 本项目只治理进入视觉工具的图片载荷；上下文何时
   由 Codex 自动整理仍完全交给 Codex 本身，避免普通对话因桥侧阈值而提前丢失可用历史。

## 实施规范

### 长期主 thread

- 只保存需求、项目文件路径、哈希、尺寸和文字结论。
- 完整原图先生成最长边不超过 1024 的 JPEG 代理，再最多查看一次。
- 精确缺陷用最长边不超过 768 的局部裁剪；不同时携带原图和多张重复 crop。
- imagegen 结果不要再次整图查看。交付时直接从磁盘路径上传文件。

### 图像 worker

- 多轮生图、多候选、网页生图或 Computer Use 截图操作放进不继承主历史的短生命周期
  worker/thread。
- worker 可以查看输入和生成结果，但最终只把路径、SHA-256、尺寸和短摘要交给主 thread，
  不把 inline 图片、Base64 或截图跨回父会话。
- worker 完成即归档或销毁。需要继续编辑时再开新的短 thread，并从磁盘文件开始。

### ChatGPT 网页

- 优先用标题、DOM 文本、下载目录、文件大小和 SHA-256 判断状态。
- 状态截图使用 Chrome 窗口裁剪、最长边不超过 960 的 JPEG；状态未变化不再查看。
- 下载原图只做磁盘校验；需要视觉核验时，在隔离 worker 中查看物理缩小代理。
- chatgpt.com 的外部脚本不应当被当作长期无人值守生产 API；个人服务条款限制自动化抽取，
  见 [OpenAI 使用条款](https://openai.com/policies/row-terms-of-use/)。

### 飞书桥

飞书桥不提供 `/compact`，也不会根据 token、图片历史或任何自定义阈值主动调用
`thread/compact/start`。桥继续记录 input、cached input 和 context-window telemetry，并展示
Codex 官方自动 `contextCompaction` 事件的进度，但不改变其触发时机。

桥侧治理仅发生在图片读取边界：飞书图片和 `view_image` 输入必须先生成物理缩小的代理图，
Codex 只能观察代理路径，不能直接拉取原图。严重污染的旧 thread 应先把可复核结论写入项目
文件，再绑定一个干净 thread 续接；桥不会用提前压缩代替这一迁移。

2026-07-17 在安装版 Codex CLI 0.144.3 上做了两条真实 ephemeral smoke test：
直接 `view_image(detail=original)` 和 code-mode `exec` 内的 `tools.view_image` 都触发了
`PreToolUse`，各只生成一张代理，turn 均正常 completed；直接调用上报的
`imageView.path` 位于代理目录，且不等于原图路径。

## 验证清单

1. 新图片任务是否运行在隔离 worker，而不是长期主 thread；
2. 主 thread 是否只收到路径、哈希、尺寸和摘要；
3. 是否存在 imagegen 后再次整图 `view_image`；
4. 网页等待是否只比较文本、哈希和下载状态；
5. 桥是否从未发送 `thread/compact/start`，且没有 `/compact` 命令或桥侧压缩阈值；
6. Codex 官方自动 `contextCompaction` 发生时，飞书卡片是否仍能展示对应进度；
7. 后续纯文本 turn 的 input tokens、rollout 大小和完成时间是否恢复稳定。
