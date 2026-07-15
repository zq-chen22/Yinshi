# Codex Feishu Bridge

这是“因时而得谷”的本地桥接服务。它通过飞书长连接接收消息，把任务送入本机 Codex App Server，并把确认、进度、审批、最终文字和交付附件回传到原会话。

项目级介绍、逐步安装和运维说明位于仓库根目录的 [`docs/`](../docs/README.md)。第一次部署建议依次阅读：

1. [创建并发布飞书应用](../docs/03-feishu-app-setup.md)
2. [Ubuntu 完整安装流程](../docs/04-ubuntu-setup.md)
3. [上线验收清单](../docs/SETUP_CHECKLIST.md)

## 快速安装

要求 Python 3.12+、已登录的 Codex CLI 和已发布的飞书企业自建应用：

```bash
chmod +x scripts/install-user-service.sh
./scripts/install-user-service.sh

$EDITOR ~/.config/codex-feishu-bridge/config.toml
$EDITOR ~/.config/codex-feishu-bridge/secrets.env

set -a
source ~/.config/codex-feishu-bridge/secrets.env
set +a

.venv/bin/codex-feishu-bridge doctor
.venv/bin/codex-feishu-bridge init
.venv/bin/codex-feishu-bridge pair-code
.venv/bin/codex-feishu-bridge run
```

Secret 只能保存在本机 `secrets.env`，不能写入配置、聊天、截图、日志或 Git。

## API 配额与进度更新

飞书 WebSocket 长连接是实时主通道，历史消息 API 只负责补漏。最近 6 小时有活动的会话约每 10 分钟补扫一次；无近期活动时逐级放缓为 30、60、120 分钟，并按会话稳定错峰。

新进度卡的前 2 分钟最多每 5 秒合并更新一次，之后最多每 30 秒更新一次；审批、终态和最终答复立即发送，不受进度节流限制。桥在 SQLite 中按月聚合自身发起的飞书 REST API 操作，私聊机器人发送 `状态` 可以查看累计和主要调用类型。它不实施严格的每日额度降级。

## 长上下文与恢复治理

桥接器使用 `thread/turns/list` 的分页摘要核对历史，不会在轮询和恢复时把整段图片历史重新装入单条 JSONL。App Server 的实时流保留 128 MiB 上限，以容纳合法的多图完成通知。桥会记录 Codex 上报的输入 token、模型上下文窗口及线程是否已经查看/生成图片；输入达到绝对阈值、上下文占比阈值，或图片线程达到更低阈值中的任一条件后，会在下一条消息开始前调用原生 `thread/compact/start`，并等待对应 `contextCompaction` turn 完成。只有观察到匹配 turn 的 `contextCompaction` item 才视为压缩成功，普通 turn 的完成事件不能误解锁。群聊也可在 thread 空闲时发送 `/compact` 手动压缩；活动或排队时会安全拒绝。压缩请求一旦发出，未决状态会持久保存；即使等待超时或服务重启，也会先从 App Server 核实压缩 turn 已终止，绝不会并发启动新的用户 turn。默认压缩等待上限为 30 分钟，超时只停止内存等待，不等同于取消服务端压缩。

如果 App Server 连接仍在但遗漏了 `turn/completed`，进度超过 stale 阈值后会额外用分页摘要审计
该 turn；一旦磁盘状态已是 completed/failed/interrupted，就落终态、停止续租并发送结果。服务
重启时也不会把达到压缩阈值且已经 interrupted 的超大 thread 自动续做，避免恢复指令再次
触发图片历史软卡死；它会先安全结束旧 turn，并让下一条用户消息在 compact 后启动。

App Server 通知由单一 FIFO 消费者按 stdout 到达顺序处理，避免 `turn/completed` 越过最后一条 item；入队前会移除桥不使用的内联图片与工具结果，并在队列深度异常时告警，后台回调任务在关机时也会统一追踪和清理。失败的进度 PATCH 使用指数退避，进度卡写入使用逐消息锁，终态写入后不会再被较早的“执行中”更新覆盖；终态卡片更新失败会进入持久 outbox 重试。最终文字和附件只有成功写入持久 outbox 后，turn 才会从可恢复状态移除。

正常 `systemctl restart` 会先停止接新单，再等待已经接单的 turn、最终结果和附件排空。若崩溃、断电或旧版本强制停止留下一个由桥创建、已经有 turn ID、没有最终答复的 `interrupted` turn，新进程会在同一 thread 中检查工作区现场后续做，而不是重放或回显原始飞书指令。只有发生在 `turn/start` 返回前、无法证明是否已经提交的歧义边界才进入 `待确认`，由所有者决定重试或忽略。

## 命令

| 命令 | 用途 |
| --- | --- |
| `init` | 创建本地目录并登记最近的持久顶层 Codex 对话 |
| `doctor` | 检查 Codex、飞书凭据、配对状态和 Ubuntu 沙箱前置条件 |
| `recent` | 查看滚动最近对话和飞书群绑定状态 |
| `pair-code` | 生成 15 分钟有效的所有者配对码 |
| `bootstrap` | 创建或认领待绑定的飞书对话群 |
| `run` | 前台运行长连接、执行和同步服务 |
| `sync-daily-stats` | 同步今天和昨天的飞书触发 Codex 任务统计 |

## 本地数据

- 配置：`~/.config/codex-feishu-bridge/config.toml`
- Secret：`~/.config/codex-feishu-bridge/secrets.env`
- 状态：`~/.local/share/codex-feishu-bridge/`
- 数据库：`~/.local/share/codex-feishu-bridge/bridge.sqlite`
- 收件箱与发件箱：状态目录下的 `inbox/`、`outbox/`

上述内容均不得提交到仓库。

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin -q
```

源码模块位于 `src/codex_feishu_bridge/`，测试位于 `tests/`。当前正式支持 Ubuntu 用户级 systemd 部署；Windows 原生服务化仍在路线图中。

## 默认安全策略

公开模板默认采用 `approval_policy = "on-request"` 和 `sandbox = "workspace-write"`。单一所有者确有需要时可以主动改为 `never` 与 `danger-full-access`，但这会允许远程消息在用户权限范围内执行高风险操作；请先阅读[安全与数据管理](../docs/07-security-and-data.md)。
