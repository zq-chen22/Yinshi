# 主机与飞书桥外部心跳监控

本方案使用托管的 Healthchecks.io Dead Man's Switch 和一个飞书群自定义机器人，
在不自建服务器的情况下通知主机/桥离线与恢复。它只发送状态，不上传聊天、
Codex 内容、文件路径或主机日志。

## 状态模型

建立两个独立检查：

| 检查 | 本机执行位置 | 含义 |
|---|---|---|
| 主机存活 | systemd 系统 timer | 操作系统、Python 环境和到监控站的网络可用 |
| 飞书桥健康 | systemd 用户 timer | 用户会话存在，且 `codex-feishu-bridge.service` active |

- 两项都正常：主机和桥可用；
- 主机正常、桥异常：机器开着，但桥未运行；
- 两项都异常：主机不可达，可能是关机、睡眠或断网，不能据此断言物理关机。

## 1. 创建通知群机器人

在因时组织创建内部群“因时｜主机状态”，添加自定义机器人“因时主机监控”。
安全设置选择自定义关键词，并添加 `主机监控`。不要启用 IP 白名单：托管监控
平台的 Webhook 出站 IP 并不固定。没有中继服务时也不要启用签名，因为签名需
按每次请求的时间戳动态计算。

复制 Webhook 后只在 Healthchecks 集成页面填写，不要写入仓库或聊天。

## 2. 创建 Healthchecks 检查

创建两个 Simple check：

- `Codex-本机名称｜主机存活`
- `Codex-本机名称｜飞书桥健康`

两项均设置：

- Period：2 minutes
- Grace Time：1 minute

本机每分钟上报一次；正常网络抖动有一次容错，最后成功上报约三分钟后判 Down。
两个 Ping URL 都是凭据，不得公开。

## 3. 配置飞书 Webhook 集成

在 Healthchecks 的 Integrations 页面添加 Webhook。Down/Up 都使用飞书自定义
机器人的同一个 Webhook URL、`POST` 方法和请求头：

```text
Content-Type: application/json
```

Down body：

```json
{"msg_type":"text","content":{"text":"主机监控｜⚠️ $NAME 离线或不可达\n状态：$STATUS\n事件时间（UTC）：$NOW\n可能原因：关机、睡眠、断网，或对应服务停止。"}}
```

Up body：

```json
{"msg_type":"text","content":{"text":"主机监控｜✅ $NAME 已恢复在线\n状态：$STATUS\n事件时间（UTC）：$NOW"}}
```

保存后确认两个 check 都启用了该集成。

## 4. 安装本机 timer

先更新虚拟环境中的 editable install，再安装 unit：

```bash
cd ~/Projects/Yinshi/codex-feishu-bridge
.venv/bin/pip install -e '.[test]'
chmod +x scripts/install-healthchecks-heartbeat.sh
./scripts/install-healthchecks-heartbeat.sh
```

编辑本地凭据文件：

```bash
${EDITOR:-nano} ~/.config/codex-feishu-bridge/heartbeat.env
chmod 600 ~/.config/codex-feishu-bridge/heartbeat.env
```

只在本机填写：

```text
HEALTHCHECKS_HOST_PING_URL=https://hc-ping.com/替换为主机检查UUID
HEALTHCHECKS_BRIDGE_PING_URL=https://hc-ping.com/替换为桥检查UUID
```

启用：

```bash
./scripts/install-healthchecks-heartbeat.sh --enable
```

检查：

```bash
systemctl status codex-feishu-host-heartbeat.timer
systemctl --user status codex-feishu-bridge-heartbeat.timer
systemctl list-timers codex-feishu-host-heartbeat.timer
systemctl --user list-timers codex-feishu-bridge-heartbeat.timer
```

## 5. 端到端验收

1. 等两个 Healthchecks check 变为 Up；
2. 停止桥 timer 超过三分钟，确认飞书出现桥 Down；
3. 恢复 timer，确认出现桥 Up；
4. 让整机进入睡眠或短暂关机，确认两项均 Down；
5. 恢复后确认主机和桥分别 Up；
6. 核对消息正文从未包含 Ping URL、Webhook、App Secret 或聊天内容。

测试完成后不要用 `/fail` URL 作为常规 timer 地址；桥检查器只在明确发现用户服务
不 active 时才自动发送一次失败信号。

计划维护时如不希望触发告警，先在 Healthchecks 暂停对应检查，或先停用相应 timer；
维护完成并确认服务恢复后再重新启用。
