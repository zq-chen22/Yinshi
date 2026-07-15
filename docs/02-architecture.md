# 系统架构与数据流

## 总览

```text
手机/桌面飞书
      │
      │ 飞书长连接事件、REST API
      ▼
FeishuGateway + WS 子进程
      │ 只做快速标准化与持久化
      ▼
SQLite inbox/outbox/绑定/租约
      │
      ▼
BridgeService 路由、队列、恢复
      │ JSONL stdio
      ▼
Codex App Server
      │
      ▼
本地 thread、工作目录、命令和文件
```

## 主要组件

### `FeishuGateway`

- 使用企业自建应用 App ID/Secret 创建飞书 REST 客户端。
- 在独立 `multiprocessing` 子进程中运行飞书 WebSocket SDK。
- 接收 `im.message.receive_v1` 和 `card.action.trigger`。
- 把事件标准化后迅速写入 SQLite，避免在飞书要求的短回调窗口内运行 Codex。
- 发送/更新文字、卡片、图片、文件和历史补扫请求。

### `BridgeDB`

SQLite 是本机事实来源，保存：

- `settings`：配对、兼容状态、运行配置和恢复标志
- `bindings`：Codex thread 与飞书群绑定
- `inbox_messages`：飞书收件队列、租约、重试和状态待确认
- `outbox_messages`：飞书发件队列、顺序、租约和重试
- `turn_jobs`：飞书消息与 Codex turn 的关联
- `api_call_usage`：按月、应用角色和操作类型聚合的飞书 REST 调用计数
- `pending_approvals`：待处理的 Codex 审批
- 附件暂存和运行配置历史

数据库启用 WAL。运行中的数据库不能通过普通复制保证一致性；备份应先停服务或使用 SQLite 在线备份。

`bindings` 会分别保存 thread 的工作目录、原始创建时间和最近更新时间。桥在建群时写入群描述，并在首次升级同步或元数据变化后调用飞书更新群接口；描述同步使用内容哈希避免每轮轮询重复写入。

### `BridgeService`

负责：

- owner 认证和租户校验
- 私聊/群聊控制面路由
- 每个 thread 的顺序队列
- thread 租约和活跃 turn 跟踪
- Codex 通知转为飞书进度
- 模糊提交边界保护
- 历史消息补扫
- App Server、WebSocket 子进程和发送队列监控
- 自适应历史补扫和分阶段进度卡节流
- 平滑排空、启动恢复和桥自有 turn 的外部回显抑制

### `CodexAppServer`

桥以 `codex app-server --listen stdio://` 启动本机 Codex App Server，通过省略 `jsonrpc` 字段的 JSONL RPC 调用：

- `thread/list/read/start/resume`
- `turn/start/steer/interrupt`
- `model/list`
- `thread/settings/update`
- 额度和使用量接口
- 审批请求与流式通知

桥使用 App Server 的实际模型目录动态生成 `/model`、推理强度和 Fast 选项，不维护静态模型表。

## 一条普通群消息的生命周期

1. 飞书 WebSocket 子进程收到消息事件。
2. 校验事件 App ID，移除机器人 mention，提取身份、文字和附件键。
3. 把完整标准化消息写入 `inbox_messages`；重复 message ID 不会再次插入。
4. 主服务租约领取 inbox 消息。
5. 校验 owner、tenant、chat/thread 绑定。
6. 若只有附件，则下载到隔离 inbox，状态改为 `held` 并等待文字。
7. 创建/更新进度卡片，把消息加入该 thread 队列。
8. 获取 thread 租约，确认没有另一个活动 turn 或状态不明的外部写入。
9. 在 `turn/start` 前把 inbox 标为 `dispatching`，建立不可自动重放的持久边界。
10. Codex 接受 turn 后记录 `turn_jobs`。
11. commentary、计划和工具事件在新卡前 2 分钟最多每 5 秒、之后最多每 30 秒合并更新；终态立即更新。
12. `turn/completed` 后把最终文字和交付物写入持久化 outbox。
13. outbox worker 按顺序发送；成功后把 turn 标为 `delivered`。

## 为什么需要“状态待确认”

网络可能恰好在桥发送 `turn/start` 后、收到返回前断开。此时桥无法证明 Codex 是否已经开始执行；盲目重试可能重复删除文件、发请求或修改系统。

因此消息在 RPC 前先进入 `dispatching`。进程在这个边界中断后，启动恢复会把它标记为 `ambiguous`，要求所有者在私聊中明确选择重试或忽略。桥不会自动重放状态不明的副作用。

这与已经取得 turn ID 后的基础设施中断不同。桥会把已确认的 turn 写入 `turn_jobs`；若启动时发现这个桥自有 turn 明确为 `interrupted`、没有最终答复，则在同一 thread 内创建恢复 turn。恢复输入不包含原用户原文，而是要求 Codex 检查对话和工作区现场、避免重复已经完成的副作用并继续收尾。旧 turn 先标记为已同步，新恢复 turn 仍关联同一飞书 message ID，因此不会被“本机外部更新”扫描器重复回显。

## 停机和历史补扫

收到 SIGTERM/SIGINT 后，服务进入 drain：不再领取 inbox 新任务，但继续处理已经排队或活动的 turn、终态卡、持久 outbox 和附件。全部排空后才关闭 App Server；systemd 为此提供比桥内 6 小时排空上限略长的停止窗口。

WebSocket 是实时消息主路径。REST 历史补扫以每个会话最后活动时间单独调度：活跃会话约 10 分钟，较冷会话约 30、60、120 分钟，并带稳定错峰。启动和 WebSocket 子进程重启会强制补扫；持久 message ID 去重使重叠时间窗保持幂等。

## 同一 thread 的并发边界

桥在本进程和数据库层维护顺序队列与租约，但独立 Codex App Server 进程之间没有可证明的全局互斥。桥运行时，不应同时在另一 CLI/IDE/App Server 向同一个受管 thread 启动 turn。

需要并行工作时，应创建不同 thread，而不是让两个执行者同时写一个 thread。

## 多机器拓扑

### 当前推荐：一机一应用

```text
飞书组织“因时而得谷”
├── Codex-笔记本A ── 长连接 ── 笔记本A桥 ── 本机Codex A
├── Codex-4090主机 ─ 长连接 ── 4090桥 ───── 本机Codex B
└── Codex-服务器01 ─ 长连接 ── 服务器桥 ─── 本机Codex C
```

每台机器独立拥有：

- App ID/Secret
- owner 配对
- SQLite 状态
- Codex 登录与 sessions
- 群名后缀和 workspace allowlist

飞书长连接对同一应用的多个 client 采用随机集群投递，不会广播，也不会按机房或环境路由。因此多个独立机器不能同时复用一个 App ID。

### 未来可选：中央调度器

如果未来希望手机只看到一个机器人，需要新增中央控制平面：只有中央服务连接飞书，再通过带身份认证的队列或 mTLS 通道把任务发送到指定主机 agent。

这要求额外设计：

- 主机注册、在线状态与能力目录
- 端到端任务身份与授权
- 主机选择 UI
- 中央和 agent 之间的消息持久化
- 网络分区、重复投递和撤销语义
- 远程通道加密、密钥轮换和审计

当前代码不包含该架构，不能通过在多台机器复制相同 Secret 来模拟。
