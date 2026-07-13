# Ubuntu Server、Windows 与多主机部署

## 支持状态

| 平台 | 当前状态 | 推荐方式 |
| --- | --- | --- |
| Ubuntu 24.04 桌面/工作站 | 已有安装器和 systemd 用户服务 | 直接按完整流程部署 |
| Ubuntu Server 24.04 | 核心能力可用 | 专用普通用户 + systemd user + linger |
| 其他 systemd Linux | 大体可复用，未逐发行版验证 | 先在测试机运行 doctor 和完整验收 |
| Windows 11 原生 | Python 核心预计可移植，尚无正式服务安装器 | 当前优先使用 WSL2 或参与完善原生支持 |
| Windows Server | 尚未完成正式验证 | 不建议直接用于无人值守生产 |

这里故意区分“代码可能运行”和“已经提供可重复、可运维的正式部署”。当前 Ubuntu 是主支持路径；Windows 原生仍需补齐服务管理、路径、进程信号、沙箱诊断和测试矩阵。

## Ubuntu Server

服务器不需要安装飞书桌面客户端。桥通过飞书开放平台长连接工作，只要求服务器能够主动访问相关域名。

### 创建专用普通用户

```bash
sudo adduser --disabled-password --gecos '' codexbridge
sudo loginctl enable-linger codexbridge
sudo -iu codexbridge
```

不要以 root 运行 Codex 或桥。以 `codexbridge` 用户完成 Codex CLI 安装与登录、仓库克隆、配置、配对和服务启动。需要访问项目目录时，用 Unix 用户组和最小目录权限授权。

### 无桌面会话运行

完成 [Ubuntu 安装](04-ubuntu-setup.md)后：

```bash
systemctl --user enable --now codex-feishu-bridge.service
loginctl show-user "$USER" -p Linger
```

如果 SSH 中 `systemctl --user` 找不到 session bus，先确认 PAM/systemd 用户会话正确创建；不要用 root system service 草率代替，因为那会改变 HOME、Codex 登录位置和文件权限边界。

### 服务器特别注意

- App Secret 与 Codex 登录材料只存在服务器本地或合规秘密管理器；
- 仅开放 SSH 等确实需要的入站端口，飞书桥本身无需入站端口；
- 对 `~/.local/share/codex-feishu-bridge` 做加密备份和磁盘容量告警；
- 为长任务配置稳定电源、时间同步和出站网络；
- 在系统更新和自动重启前确认没有活动 turn；
- 无人值守使用 Full Access/YOLO 的风险高于个人桌面机，优先保持 Default。

## Windows 的两条路线

### 路线 A：WSL2（当前更现实）

在 Windows 11 安装 Ubuntu WSL2，然后在 WSL 内按 Linux 方式运行 Python、Codex 和桥。需要注意：

- WSL 的 systemd 必须启用；
- Windows 重启后 WSL 实例不一定在用户未登录时自动启动；
- `/mnt/c` 的权限与性能不同于 Linux 文件系统，受管工作区优先放在 WSL 的 ext4 HOME；
- Windows 与 WSL 不要同时向同一 Codex thread 写入；
- 休眠、Modern Standby 和网络切换会中断长连接，恢复后要验证服务。

在 `/etc/wsl.conf` 中启用 systemd：

```ini
[boot]
systemd=true
```

从 PowerShell 执行 `wsl --shutdown` 后重新进入，再确认：

```bash
systemctl --user status
```

WSL 的开机无人值守属于 Windows 侧编排问题，需要额外用任务计划程序启动目标发行版；该流程尚未纳入当前正式安装器。

### 路线 B：原生 Windows（尚未完成正式支持）

理论组件包括：

1. Python 3.12+ 虚拟环境；
2. Windows 版 Codex CLI 与已完成的本地登录；
3. 同样的 `config.toml`、环境变量和 SQLite 数据库；
4. 前台执行 `python -m codex_feishu_bridge.cli ... run`；
5. 通过任务计划程序或 Windows Service Wrapper 实现用户级常驻。

但在标记为正式支持前必须完成：

- 让 `doctor` 按操作系统跳过 Linux 专属 AppArmor/bwrap 检查；
- 验证 Windows 路径、文件权限和 Secret ACL；
- 替换 Unix signal、锁和服务停止语义中的平台假设；
- 提供 PowerShell 安装/升级/卸载脚本；
- 验证任务计划程序或原生 Windows Service 的登录环境与 Codex 凭据；
- 运行完整单元、重启恢复、断网和附件测试；
- 明确 Windows Codex 沙箱所需条件。

在这些工作完成前，不应把 Ubuntu 的 `systemd` 步骤机械翻译为 Windows 命令并宣称完成部署。

## 多主机拓扑

当前推荐“一台主机、一个飞书应用、一个桥进程、一个本地状态库”：

```text
Codex-笔记本Ubuntu  → 笔记本 bridge  → 笔记本 Codex
Codex-主机4090      → 4090 bridge   → 4090 Codex
Codex-实验服务器01  → 服务器 bridge  → 服务器 Codex
```

为每台机器重复：

1. 在同一因时组织创建新的企业自建应用；
2. 添加机器人、权限、事件与卡片回调；
3. 发布并把应用可用范围包含 owner；
4. 在目标机保存该应用自己的 App ID/Secret；
5. 配置唯一 `group_suffix`；
6. 在该机器生成配对码并私聊对应机器人；
7. 完成验收清单。

不要复制其他机器的 `bridge.sqlite` 来“迁移”绑定，也不要让多台机器共享网络文件系统上的状态目录。跨主机集中调度器是未来架构，不是当前桥的功能。

## 从现有机器复用经验，而不是复用秘密

可以复用：

- 本仓库源码和文档；
- 非敏感配置结构与命名规范；
- 权限、事件和验收清单；
- systemd 模板和运维流程。

必须重新创建：

- 飞书应用及其 Secret；
- 本机 Codex 登录；
- owner 配对记录；
- thread 与群绑定；
- 本地数据库、inbox、outbox 和工作区。
