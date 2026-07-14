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

## 长上下文与进度治理

桥接器使用 `thread/turns/list` 的分页摘要核对历史，不会在轮询和恢复时把整段图片历史重新装入单条 JSONL。App Server 的实时流保留 128 MiB 上限，以容纳合法的多图完成通知。桥会记录 Codex 上报的输入 token 与模型上下文窗口；达到配置阈值后，在下一条消息开始前调用原生 `thread/compact/start`，并等待对应 `contextCompaction` turn 完成。群聊也可在 thread 空闲时发送 `/compact` 手动压缩；活动或排队时会安全拒绝。

中间事件仍按 `progress_update_seconds` 合并，但无新事件时只按 `progress_heartbeat_seconds` 发送心跳；超过 `progress_stale_seconds` 会明确显示“没有新事件”，避免把计时器变化误报成持续进展。

## 命令

| 命令 | 用途 |
| --- | --- |
| `init` | 创建本地目录并登记最近的持久顶层 Codex 对话 |
| `doctor` | 检查 Codex、飞书凭据、配对状态和 Ubuntu 沙箱前置条件 |
| `recent` | 查看滚动最近对话和飞书群绑定状态 |
| `pair-code` | 生成 15 分钟有效的所有者配对码 |
| `bootstrap` | 创建或认领待绑定的飞书对话群 |
| `run` | 前台运行长连接、执行和同步服务 |

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
