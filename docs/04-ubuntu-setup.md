# Ubuntu 完整安装流程

本文面向一台全新的 Ubuntu 桌面或工作站。完成后，飞书私聊承担管理和临时任务，机器人创建的群聊分别绑定本机已有 Codex 对话。

## 0. 部署前确认

你需要同时具备：

- Ubuntu 24.04 或兼容发行版的普通用户和必要时的 sudo 权限；
- Python 3.12 或更高版本、`git`、`python3-venv`；
- 已安装、可直接运行且已经登录的 Codex CLI；
- “因时而得谷”飞书组织的管理员权限；
- 按[飞书应用创建与发布](03-feishu-app-setup.md)完成的一台主机专用应用；
- 主机可主动访问飞书开放平台和 Codex 所需网络。

桥使用飞书长连接，不需要公网 IP、域名、HTTPS 证书、端口映射或入站防火墙规则。

每台主机必须使用独立的飞书应用。若本机命名为 `Codex-鼎好主机4090`，建议群后缀设为 `-鼎好主机4090`。同一个 App ID 的多个长连接客户端会被飞书视为同一应用的负载均衡实例，消息可能随机落到任一主机。

## 1. 验证 Codex CLI

先在将来运行服务的同一 Linux 用户下执行：

```bash
command -v codex
codex --version
codex
```

在交互界面里发送一个无副作用的测试问题，确认登录和模型访问正常，再退出。桥不会替你创建或迁移 Codex 登录凭据，systemd 服务也必须运行在这个用户下。

如果 `codex` 不在 `/usr/local/bin`、`/usr/bin`、`~/.local/bin`，记住其绝对路径，稍后填写 `bridge.codex_bin`。

## 2. 安装系统依赖

```bash
sudo apt update
sudo apt install -y git python3 python3-venv bubblewrap apparmor-profiles apparmor-utils
python3 --version
```

Python 必须至少为 3.12。Ubuntu 24.04 对非特权 user namespace 的限制可能阻止 Codex 的 `workspace-write` 沙箱启动。安装并加载 bubblewrap 的精确 AppArmor profile：

```bash
sudo install -m 0644 \
  /usr/share/apparmor/extra-profiles/bwrap-userns-restrict \
  /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_status | grep -F bwrap-userns-restrict
```

不要为省事全局关闭 AppArmor 或 user namespace 限制。

## 3. 获取仓库

```bash
mkdir -p ~/Projects
cd ~/Projects
git clone https://github.com/zq-chen22/Yinshi.git
cd Yinshi/codex-feishu-bridge
```

如果仓库已经存在：

```bash
cd ~/Projects/Yinshi
git status
git pull --ff-only
cd codex-feishu-bridge
```

工作树有未提交改动时不要直接覆盖；先确认这些改动属于谁、是否需要保存。

## 4. 安装用户服务

```bash
chmod +x scripts/install-user-service.sh
./scripts/install-user-service.sh
```

脚本会：

1. 在源码目录创建 `.venv`；
2. 以 editable 模式安装桥；
3. 创建 `~/.config/codex-feishu-bridge/`；
4. 首次复制 `config.example.toml`；
5. 首次创建权限为 `0600` 的 `secrets.env`；
6. 根据真实源码路径生成 systemd 用户服务并执行 daemon-reload；
7. 保留已存在的配置和 Secret，不自动启动服务。

安装结束后确认：

```bash
ls -ld ~/.config/codex-feishu-bridge ~/.local/share/codex-feishu-bridge
ls -l ~/.config/codex-feishu-bridge/secrets.env
```

## 5. 填写非敏感配置

```bash
${EDITOR:-nano} ~/.config/codex-feishu-bridge/config.toml
```

至少修改：

```toml
[bridge]
group_suffix = "-鼎好主机4090"
codex_bin = "codex"
allowed_workspace_roots = ["~"]

[feishu.conversation]
app_id = "cli_这里填写本机飞书应用的AppID"
app_secret_env = "FEISHU_CONVERSATION_APP_SECRET"
```

不要把 App Secret 写进 TOML。`allowed_workspace_roots` 限制从飞书“新对话 名称 | /目录”可选择的工作目录；需要多个合法根目录时显式列出，不要无意中扩大到整个文件系统。

公开模板的安全默认值为：

```toml
approval_policy = "on-request"
sandbox = "workspace-write"
```

如果机器和飞书确实只有同一个所有者使用，并且接受远程消息能够在用户权限范围内修改任意文件、运行命令和访问网络，才主动切换为：

```toml
approval_policy = "never"
sandbox = "danger-full-access"
```

这就是 Full Access/YOLO，不是安装所必需的设置。

## 6. 只在本机填写 App Secret

```bash
${EDITOR:-nano} ~/.config/codex-feishu-bridge/secrets.env
chmod 600 ~/.config/codex-feishu-bridge/secrets.env
```

文件格式：

```bash
FEISHU_CONVERSATION_APP_SECRET='替换为飞书开放平台显示的真实Secret'
```

不要把真实值粘贴到飞书、Codex、Issue、提交、截图或终端录屏。当前终端运行管理命令前静默加载：

```bash
set -a
source ~/.config/codex-feishu-bridge/secrets.env
set +a
```

检查变量是否存在时只看长度，不打印内容：

```bash
test -n "${FEISHU_CONVERSATION_APP_SECRET:-}" && echo 'Secret 已加载'
```

## 7. 首次诊断和登记对话

在 `codex-feishu-bridge` 目录执行：

```bash
BRIDGE="$PWD/.venv/bin/codex-feishu-bridge"
"$BRIDGE" doctor
"$BRIDGE" init
"$BRIDGE" recent
```

`doctor` 应确认配置、Codex App Server、飞书凭据、SQLite 和沙箱前置条件。首次尚未配对时，owner 相关检查允许显示待完成，但 App ID/Secret、Codex 和系统依赖不应报错。

`init` 默认登记最近 3 个持久顶层对话。它不会写死任何 thread ID；实际集合来自本机 Codex。`recent` 可反复运行，后续自动发现新近对话，但不会因旧对话跌出最近集合而删除已有绑定。

## 8. 启动长连接并配对所有者

生成短时配对码：

```bash
"$BRIDGE" pair-code
```

随后前台启动服务并保持终端开启：

```bash
"$BRIDGE" run
```

在“因时而得谷”中私聊本机的 Codex 机器人，发送终端给出的：

```text
配对 XXXXXXXX-XXXXXXXX
```

配对只能在私聊中完成，默认 15 分钟失效。成功后，机器人会记住当前应用作用域下的 owner 身份，并开始创建或认领待绑定群。

如果群没有自动出现，停止前台进程后执行：

```bash
"$BRIDGE" bootstrap
"$BRIDGE" run
```

`bootstrap` 是幂等的：已有稳定标签的群会被认领，不应重复创建。

## 9. 端到端验收

按顺序验证：

1. 私聊发送 `帮助`，收到管理命令列表；
2. 私聊发送 `状态`，看到服务、收发箱和长连接状态；
3. 私聊发送一个无关小问题，看到接收确认、中间进度和最终结果；
4. 在任一对话群发送无副作用任务，确认进入正确的本机 Codex thread；
5. 任务执行中发送 `!steer 补充要求`，确认同一轮被修正；
6. 先发送一张图片，确认不会立即启动任务，再发文字说明并确认二者一起交给 Codex；
7. 让 Codex 在本轮专用 outbox 生成一个小于飞书上限的测试文件，确认自动回传；
8. 暂时断网再恢复，确认长连接自动重连且发送队列继续；
9. 执行 `recent`，核对群名、thread ID 和绑定状态。

不要在验收中让本机 Codex CLI/IDE 与飞书桥同时向同一个 thread 发起 turn。

## 10. 切换为后台服务

前台验收通过后按 Ctrl+C 停止，然后：

```bash
systemctl --user enable --now codex-feishu-bridge.service
systemctl --user status codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -f
```

如果要求退出桌面会话后仍保持运行，可明确决定后开启 linger：

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

重启机器做最终验证：

```bash
sudo reboot
```

重连后检查：

```bash
systemctl --user is-enabled codex-feishu-bridge.service
systemctl --user is-active codex-feishu-bridge.service
journalctl --user -u codex-feishu-bridge.service -b --no-pager
```

电脑关机时无法接收新任务；恢复后长连接会重建并补扫可见消息。Codex 正在执行时突然关机属于不确定边界，桥不会擅自重放可能已经产生副作用的任务，需要在私聊中用 `待确认` 处理。
