# 日常运维、备份与升级

## 本机管理命令

默认配置路径为 `~/.config/codex-feishu-bridge/config.toml`：

```bash
codex-feishu-bridge doctor
codex-feishu-bridge recent
codex-feishu-bridge pair-code
codex-feishu-bridge bootstrap
codex-feishu-bridge run
```

源码安装未进入 PATH 时使用 `.venv/bin/codex-feishu-bridge`，或为所有命令加 `--config /实际路径/config.toml`。

## 飞书私聊命令

| 命令 | 行为 |
| --- | --- |
| `帮助` / `/help` | 显示可用管理命令 |
| `最近` / `/recent` | 立即扫描并列出已跟进或待创建的对话 |
| `同步` / `/sync` | 立即执行一次最近对话同步 |
| `新对话 名称` | 创建受管工作区、Codex thread 和对应飞书群 |
| `新对话 名称 \| /绝对/工作目录` | 在允许的既有目录创建 thread 和群 |
| `额度` / `/quota` | 读取当前 Codex 账号可见的额度/用量信息 |
| `状态` | 查看桥进程、活动任务、收发箱、长连接和本月飞书 API 调用汇总 |
| `待确认` | 列出崩溃临界区内不能自动判定是否已提交的消息 |
| `重试 消息ID` | 明确重放待确认消息，可能重复外部副作用 |
| `忽略 消息ID` | 明确不再执行待确认消息 |
| `解除线程 threadID` | 本机核对中断 turn 后解除该 thread 的安全锁 |

其他私聊文字会进入不保留上下文的临时 Codex thread。私聊中的图片、视频和文件会先暂存，等下一条普通文字一起提交。

## 对话群命令

| 命令 | 行为 |
| --- | --- |
| `!status` | 查看当前 turn、安全锁和排队数量 |
| `!steer 补充要求` | 当前 turn 活动时向其追加修正；否则作为普通任务入队 |
| `!stop` | 请求中断桥当前启动的 turn，不清空后续队列 |

群内普通文字会按该群绑定的 thread 串行执行。桥只能保护自身队列，无法对另一个独立 Codex CLI/IDE 进程提供绝对跨进程锁，因此不要同时写同一 thread。

群描述由同步任务自动维护，格式为“目录、开始时间、稳定 thread 标签”。升级到支持该功能的版本后，已有群会在下一轮同步中更新；无需删除或重新创建群。开始时间来自 Codex thread 的 `createdAt`，并按桥主机本地时区显示。

## 会话运行配置

私聊临时任务和每个群分别保存配置：

- `/model`：从当前 Codex App Server 的 `model/list` 动态生成模型及 reasoning effort 选择；
- `/fast`：切换当前模型实际声明的 Fast/priority 服务层级；
- `/permissions`：选择 Read Only、Default 或 Full Access；
- `/status`：查看当前模型、推理、Fast、权限、工作目录和 CLI 版本；
- `/compat`：CLI 版本变化后执行真实协议探测并解锁设置命令。

设置不是静态维护的模型表。桥会读取当前 CLI 能力，因此新增模型和推理档位无需手工同步代码。若 Codex CLI 版本不同于已验证基线，设置写入会失败关闭，机器人弹出“检测并修复”卡片；只有 `model/list`、临时 `thread/start` 和 `thread/settings/update` 均成功后才记录新基线。

## systemd 操作

```bash
systemctl --user status codex-feishu-bridge.service
systemctl --user restart codex-feishu-bridge.service
systemctl --user stop codex-feishu-bridge.service
systemctl --user start codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -f
journalctl --user -u codex-feishu-bridge.service --since today --no-pager
```

服务设置为 `Restart=on-failure`，异常退出后等待 5 秒重启。正常手工停止不会立即自启；开机能否运行取决于 `enable` 和用户会话/linger。

`stop` 和 `restart` 会先令桥停止领取新消息，再等待已经接单的 Codex turn、最终消息和附件发送完成。默认最长排空 6 小时，systemd 的停止超时应略大于该值。排空期间新收到的飞书消息仍持久写入 inbox，由下一进程启动后领取。

## 飞书 API 调用节奏

- 实时入站依靠 WebSocket，不用 REST 轮询模拟实时消息；
- 最近 6 小时有活动的私聊或群聊约每 10 分钟补扫一次历史；
- 6—24 小时、1—7 天、超过 7 天未活动时，补扫分别放缓到约 30、60、120 分钟；
- 新进度卡前 2 分钟最多每 5 秒更新一次，之后最多每 30 秒一次；
- 审批、错误终态和最终结果立即发出，不等待下一次进度窗口；
- 本地数据库按月统计桥发起的 REST 操作，`状态` 显示累计和主要类型，但不执行严格的每日额度降级。

历史补扫只是 WebSocket 丢事件、子进程重连或短暂断网时的安全网。服务启动和发现长连接子进程重启时会立即补扫一次；消息 ID 的持久去重保证补扫不会重复执行同一条飞书消息。

## 网络中断、重启和恢复

- 短暂断网：长连接重建，入站补扫和持久发件箱继续处理；
- 正常服务更新/重启：先排空已接单任务，已落库的新消息、暂存附件、发送任务和绑定仍在；
- 主机断电：机器离线期间无法实时执行；恢复后补扫机器人可见历史；
- `turn/start` 网络临界区：无法证明请求是否已到达 Codex 时标记“状态待确认”，绝不自动重放；
- 已有 turn ID 后发生非计划中断：如果这是桥创建的 turn、状态明确为 `interrupted` 且没有最终答复，新进程在同一 thread 中检查已有现场后续做，不重放原始用户指令；
- 无法证明 turn 是否存在或是否仍在执行：锁定对应 thread，要求本机核对后执行 `解除线程`。

恢复 turn 会收到一条桥内部指令，要求先检查工作目录、进程和已经生成的结果，再从未完成处继续；原始飞书消息不会作为“主机侧新消息”再次回显。若原 turn 已经产生最终答复，则只恢复该答复的持久投递，不再续做。用户主动 `!stop` 不会被周期恢复器自动重启。

## 安全备份

需要备份：

- `~/.config/codex-feishu-bridge/config.toml`；
- `~/.config/codex-feishu-bridge/secrets.env`，必须进入独立加密密码库，而不是普通备份仓库；
- `~/.local/share/codex-feishu-bridge/bridge.sqlite`；
- 仍需保留的 `inbox/`、`outbox/` 和 `workspaces/`。

SQLite 可能启用 WAL，不能在持续写入时只复制主数据库。最简单的完整备份流程：

```bash
systemctl --user stop codex-feishu-bridge.service
mkdir -p "$HOME/bridge-backup-$(date +%F)"
cp -a ~/.config/codex-feishu-bridge "$HOME/bridge-backup-$(date +%F)/config"
cp -a ~/.local/share/codex-feishu-bridge "$HOME/bridge-backup-$(date +%F)/state"
systemctl --user start codex-feishu-bridge.service
```

随后立即加密备份并删除未加密临时副本。若必须在线备份，使用 `sqlite3 bridge.sqlite '.backup /安全位置/bridge.sqlite'`，附件目录仍要使用能保证一致性的快照工具。

## 源码升级

先查看变更并备份：

```bash
cd ~/Projects/Yinshi
git status
git fetch origin
git log --oneline --decorate HEAD..origin/main
```

确认无冲突后：

```bash
systemctl --user stop codex-feishu-bridge.service
git pull --ff-only
cd codex-feishu-bridge
.venv/bin/pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin -q
./scripts/install-user-service.sh
set -a; source ~/.config/codex-feishu-bridge/secrets.env; set +a
.venv/bin/codex-feishu-bridge doctor
systemctl --user start codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -n 100 --no-pager
```

安装脚本会保留已有配置和 Secret，并刷新指向当前源码路径的用户服务。

## 回滚

升级前记录旧提交：

```bash
git rev-parse HEAD
```

需要回滚时，先停止服务并恢复与旧代码兼容的数据库备份，再在新的独立工作树检出旧提交，重新安装依赖和服务。不要在未知数据库迁移是否可逆时只回退代码。当前项目尚未承诺稳定存储 schema，发布说明必须明确迁移与回滚要求。

## 飞书应用配置变更

权限、事件、回调或可用范围修改后通常需要在开放平台创建新版本并发布，草稿不会自动作用于线上机器人。修改 App Secret 后应：

1. 停止桥；
2. 只在本机更新 `secrets.env`；
3. `chmod 600`；
4. 运行 `doctor`；
5. 重启服务并观察日志；
6. 在飞书私聊发送 `状态` 验证。
