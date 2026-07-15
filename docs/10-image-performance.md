# 图像任务性能与上下文治理

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
- [Remote Compaction V2](https://github.com/openai/codex/blob/rust-v0.144.3/codex-rs/core/src/compact_remote_v2.rs)
  的 retained-message 预算主要按文本计算，不能把 compact 当成必然删除图片的保证。

官方 Codex issue tracker 中也有方向一致的用户报告，例如
[#19936](https://github.com/openai/codex/issues/19936) 报告连续图片后出现 30–60 秒冻结，
[#24388](https://github.com/openai/codex/issues/24388) 报告 compact 后输入图仍使请求达到 MB 级。
这些 issue 是用户现场而不是 OpenAI 的根因确认；本项目的结论仍以本机 rollout、计时和网络
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
6. **旧桥压缩门槛过晚。** 旧逻辑要求同时达到 100,000 tokens 和上下文 65%。258,400 窗口下
   实际要到 167,960 才压缩，118,704-token 的异常图片 thread 因此一直未被治理。

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

桥现在在下一条消息开始前，按以下任一条件触发原生 compact：

- 输入达到 `auto_compact_min_input_tokens`（默认 100,000）；
- 上下文占比达到 `auto_compact_ratio`（默认 65%）；
- thread 已完成 imageGeneration/imageView，且输入达到
  `auto_compact_visual_input_tokens`（默认 60,000）。

飞书直接上传的图片会在启动 turn 前设置同一 visual-history 标记。进度长时间没有事件时，桥会
用 image-free 的 turn 摘要核对磁盘终态；若 `turn/completed` 通知丢失但 turn 已终止，会停止
续租并完成飞书终态。重启时，对已经 interrupted 且达到压缩阈值的历史不会自动追加恢复
turn，以免在超大图像上下文中形成第二次卡死。

compact 是恢复手段，不是图片清除保证。严重污染、compact 后仍慢或 rollout 已达几十 MB 的
thread，应先把可复核结论写入项目文件，再绑定一个干净 thread 续接。

## 验证清单

1. 新图片任务是否运行在隔离 worker，而不是长期主 thread；
2. 主 thread 是否只收到路径、哈希、尺寸和摘要；
3. 是否存在 imagegen 后再次整图 `view_image`；
4. 网页等待是否只比较文本、哈希和下载状态；
5. 输入达到任一压缩阈值时，下一条飞书任务是否先显示“正在整理长上下文”；
6. compact 完成后 visual-history 标记是否清除；
7. 后续纯文本 turn 的 input tokens、rollout 大小和完成时间是否恢复稳定。
