# 故障排查

先从本机事实开始，不要反复重发可能产生副作用的任务：

```bash
systemctl --user status codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -n 200 --no-pager
set -a; source ~/.config/codex-feishu-bridge/secrets.env; set +a
codex-feishu-bridge doctor
codex-feishu-bridge recent
codex --version
```

## 机器人在飞书中不存在或搜不到

检查：

1. 应用是否添加了 Bot 能力；
2. 是否创建并成功发布版本，而非只保存草稿；
3. 可用范围是否包含当前因时账号；
4. 当前飞书是否切换到“因时而得谷”组织；
5. 应用是否被停用或仍在审核。

当前单应用架构只有一个 Codex 机器人，私聊承担原管理员功能，群聊绑定具体对话。无需再寻找独立“管理员机器人”。

## 能私聊机器人，但桥收不到消息

检查开放平台：

- 事件订阅使用长连接；
- 已订阅 `im.message.receive_v1`；
- 消息读取权限已授权并随版本发布；
- 本机只有该 App ID 的一个桥实例；
- `状态` 中长连接不是离线；
- 日志中没有鉴权、网络、tenant 或事件反序列化错误。

修改事件/权限后要发布新版本并重启桥。

## 卡片能显示，但按钮无反应

确认回调订阅已使用长连接并添加 `card.action.trigger`，应用具有更新消息/卡片所需权限，且这些配置已经发布。文本命令是兜底：审批可回复机器人提示的 `批准 短ID` 或 `拒绝 短ID`，设置可重新发送相应斜杠命令。

## 显示“这个群尚未绑定 Codex 对话”

不要在群里猜 thread。私聊机器人发送 `同步`，再执行本机 `recent` 查看绑定。如果群被手工复制、改造成普通群或稳定标签丢失，桥不会仅按相似群名认领。

## 私聊临时任务只显示“已收到”，没有最终结果

依次查看：

1. 私聊发送 `状态`；
2. 本机查看服务日志是否仍有活动 Codex；
3. 查看发件箱计数和飞书发送错误；
4. 检查 Codex App Server是否结束、桥是否重启；
5. 私聊发送 `待确认`；
6. 如果 thread 被锁，先在本机核对再使用 `解除线程 threadID`。

不要因为没有及时显示结果就直接重发，会导致重复外部操作。

## 显示 `Codex: interrupted`

可能来源包括用户 `!stop`、桥进程退出、App Server退出、同 thread 冲突、机器休眠/关机或上游 Codex 中断。该文字不能单独证明任务会继续。

- 桥和 turn 仍活动：用 `!status` 查看；
- 桥重启且提交边界不确定：用 `待确认`；
- thread 被安全锁定：本机核对后 `解除线程`；
- 明确需要从头执行：使用 `重试 消息ID`，并接受副作用可能重复。

## CLI 版本警告或设置命令不可用

桥在检测到不同的 Codex CLI 版本时，会关闭 `/model`、`/fast`、`/permissions` 和 `/status` 的设置写入并推送兼容卡片。发送 `/compat`，选择“检测并修复”。探测会创建内存临时 thread，验证模型目录、thread 启动和设置更新，全部成功才记录新版本。

如果探测失败：

```bash
codex --version
codex-feishu-bridge doctor
journalctl --user -u codex-feishu-bridge.service -n 200 --no-pager
```

不要手工篡改已验证版本记录来绕过失败关闭；应修复协议差异并更新测试。

## Codex 工作区任务被 AppArmor/bubblewrap 拒绝

Ubuntu 24.04 检查：

```bash
command -v bwrap
sudo apparmor_status | grep -F bwrap-userns-restrict
ls -l /etc/apparmor.d/bwrap-userns-restrict
```

按 [Ubuntu 安装流程](04-ubuntu-setup.md#2-安装系统依赖)安装并加载精确 profile。不要全局关闭安全机制。

## 服务启动后立即退出

查看日志并检查：

- `secrets.env` 是否存在、权限是否正确、变量名是否与 TOML 一致；
- App ID 是否填入正确应用；
- `codex_bin` 在 systemd 的 PATH 中是否可见，必要时填绝对路径；
- systemd unit 是否仍指向真实仓库路径；
- 是否已有另一个实例持有 `service.lock`；
- 配置 TOML 是否有语法错误；
- Python 虚拟环境是否被删除。

源码路径变化后重新执行：

```bash
cd /新路径/Yinshi/codex-feishu-bridge
./scripts/install-user-service.sh
systemctl --user restart codex-feishu-bridge.service
```

## 两台机器共用同一应用后消息随机丢失

这是错误拓扑。飞书长连接多客户端可能随机消费事件，不会向每台主机广播。立即停止其中一台，为每台机器创建独立飞书应用和 Secret，分别重新配对。不要依靠重启碰运气。

## 发送图片/文件前仍要求批准

区分两种批准：

- Codex 工具执行批准：由当前 `/permissions` 控制；
- 桥将本轮 outbox 交付物上传飞书：符合专用目录、秘密扫描和大小限制时无需额外人为审批。

如果文件不是本轮在专用 outbox 新生成，桥应拒绝自动外发。不要通过放宽到任意路径来解决。

## 文件没有发出

检查：

1. 文件是否在机器人为本轮声明的专用 outbox；
2. 是否是本轮新生成而不是复制的已有文件；
3. 是否低于配置和飞书实际类型上限；
4. 是否被秘密扫描拦截；
5. 飞书是否授权 `im:resource` 和发送权限；
6. 发件箱是否处于重试或最终失败状态；
7. 磁盘是否已满、文件是否在发送前被删除。

## 图片/视频单独发送后没有执行

这是预期行为。无文字附件会被暂存，直到同一用户在同一会话发送下一条普通文字要求。发送 `状态`、`/model` 等控制命令不会消耗暂存附件。服务重启后暂存状态仍应存在。

## 重启或断网后是否会自动恢复

- systemd 已 enable 且用户会话/linger 正常：重启后自动启动；
- 短暂断网：长连接自动重建，持久队列继续；
- 机器完全关机：期间无法实时处理；
- 处于外部副作用临界区：不会自动重放，进入 `待确认`。

使用以下命令确认，而不是只看飞书页面：

```bash
systemctl --user is-enabled codex-feishu-bridge.service
systemctl --user is-active codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -b --no-pager
```

## SQLite 锁、损坏或状态异常

先停止服务并备份整个状态目录，包括 `-wal` 和 `-shm`：

```bash
systemctl --user stop codex-feishu-bridge.service
cp -a ~/.local/share/codex-feishu-bridge ~/codex-feishu-bridge-state-investigation
```

不要先删除数据库。运行 SQLite 完整性检查，保留日志和副本后再决定恢复：

```bash
sqlite3 ~/.local/share/codex-feishu-bridge/bridge.sqlite 'PRAGMA integrity_check;'
```

数据库包含消息去重和不确定边界；删除它可能导致重复执行和群重复创建。
