# 因时文档中心

本文档目录记录“因时而得谷”的目标、架构、逐步部署、日常运维、安全边界和未来开源流程。文档默认读者拥有自己的飞书组织、飞书企业自建应用管理权限，以及目标计算机的合法管理权限。

## 推荐阅读路线

### 第一次部署 Ubuntu 主机

1. [项目定义与边界](01-project-overview.md)
2. [系统架构与数据流](02-architecture.md)
3. [飞书应用创建与发布](03-feishu-app-setup.md)
4. [Ubuntu 完整安装](04-ubuntu-setup.md)
5. [上线验收清单](SETUP_CHECKLIST.md)

### 部署第二台或更多机器

1. [架构：多机器拓扑](02-architecture.md#多机器拓扑)
2. [Windows 与服务器部署](05-windows-and-server.md)
3. [安全与数据管理](07-security-and-data.md)

### 已经上线，需要维护

1. [日常运维、备份与升级](06-operations.md)
2. [故障排查](09-troubleshooting.md)
3. [图像任务性能与上下文治理](10-image-performance.md)
4. [开发与未来开源](08-development-and-open-source.md)

## 文档清单

| 文档 | 内容 |
|---|---|
| [01-project-overview.md](01-project-overview.md) | 项目为何存在、解决什么问题、不解决什么问题 |
| [02-architecture.md](02-architecture.md) | 组件、消息流、持久化、恢复、多机器设计 |
| [03-feishu-app-setup.md](03-feishu-app-setup.md) | 从零创建机器人、权限、事件、回调、发布 |
| [04-ubuntu-setup.md](04-ubuntu-setup.md) | Ubuntu 桌面/工作站逐命令安装流程 |
| [05-windows-and-server.md](05-windows-and-server.md) | Ubuntu Server、Windows、WSL 和平台差异 |
| [06-operations.md](06-operations.md) | 命令、日志、备份、更新、CLI 兼容门禁 |
| [07-security-and-data.md](07-security-and-data.md) | 凭据、身份、YOLO、附件、数据保留 |
| [08-development-and-open-source.md](08-development-and-open-source.md) | 测试、发布、贡献、许可证和路线图 |
| [09-troubleshooting.md](09-troubleshooting.md) | 常见症状与逐层排查 |
| [10-image-performance.md](10-image-performance.md) | 生图固有延迟、图片历史膨胀、受控实验和隔离工作流 |
| [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md) | 可勾选的上线验收表 |

## 文档维护规则

- 配置、命令或界面变化必须同步更新对应文档。
- 文档示例只能使用占位符，不得放入真实凭据和身份 ID。
- 飞书控制台名称可能随版本变化；同时记录中文含义和稳定的权限/事件标识。
- 未在真实平台验证的能力必须明确标记为“计划中”或“尚未验证”。
