# 开发、发布与未来开源

## 当前项目阶段

仓库已经包含在单一所有者 Ubuntu 主机上运行过的桥接实现、单元测试、安装器和部署文档，但仍处于早期工程化阶段。公共 API、数据库 schema、Windows 支持和发布兼容性尚未承诺稳定。

仓库当前没有许可证。公开可见不自动授予复制、修改和再分发权；正式开放外部贡献前，项目所有者需要选择并提交明确的 `LICENSE`，同时确认依赖许可证兼容性。

## 开发环境

```bash
git clone https://github.com/zq-chen22/Yinshi.git
cd Yinshi/codex-feishu-bridge
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

运行测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/pytest -p pytest_asyncio.plugin -q
```

关闭全局 pytest 插件自动加载可以避免宿主机 ROS 等环境污染；干净环境可直接 `.venv/bin/pytest -q`。

## 代码结构

```text
src/codex_feishu_bridge/
├── cli.py            # 命令入口、doctor、init、bootstrap、常驻生命周期
├── config.py         # TOML、环境变量和目录配置
├── codex_client.py   # Codex App Server JSON-RPC 客户端
├── feishu.py         # 飞书 API、长连接、消息和卡片标准化
├── db.py             # SQLite 状态与队列
├── models.py         # 内部数据模型
└── service.py        # 路由、执行、恢复、进度、审批和附件主逻辑
```

配套内容：

- `tests/`：配置、数据库、飞书标准化、Codex 客户端和服务行为测试；
- `config.example.toml`：不含秘密的安全默认模板；
- `scripts/install-user-service.sh`：Ubuntu 用户级安装；
- `systemd/`：服务模板；
- 仓库根 `docs/`：全流程文档。

## 设计约束

改动必须保持这些核心不变量：

1. App Secret 不进入 TOML、日志、测试夹具或 Git；
2. 同一飞书消息通过稳定消息 ID 幂等去重；
3. 同一 thread 的桥内任务串行执行；
4. 无法确定 `turn/start` 是否成功时失败关闭，不自动重放；
5. 进度和最终结果先持久化，再尝试外发；
6. 附件与下一条普通文字原子合并，桥命令不误消费附件；
7. 只有本轮专用 outbox 中的新交付物可自动外发；
8. 模型和推理能力来自 Codex App Server 动态目录，不维护静态白名单；
9. CLI 升级后的设置命令必须经过实际协议探测；
10. 群与 thread 的绑定不能靠群名猜测，必须有稳定标签和数据库记录。

## 贡献流程

1. 从最新主分支创建功能分支；
2. 为行为变更先补充或更新测试；
3. 实现最小范围改动；
4. 更新用户文档、示例配置和升级说明；
5. 运行完整测试和秘密扫描；
6. 在测试飞书应用上完成端到端验收；
7. 提交不含本地状态、凭据和真实用户数据的 PR。

不要提交 `.venv`、`config.toml`、`secrets.env`、SQLite/WAL、inbox/outbox、真实 thread/chat/open ID、日志转储和用户附件。

## 兼容性原则

### Codex CLI

不要在代码中猜测未来模型 ID、reasoning effort 或服务层。尽量通过 App Server 的能力发现接口获取。Codex 协议变化时，先把设置写入门禁关闭，再验证当前版本并更新测试基线。

### 飞书开放平台

权限和控制台文案可能变化，代码与文档应优先记录稳定 scope、事件名和回调名。API 变更需覆盖：消息接收、历史补扫、资源上传下载、卡片更新、建群/认领和成员管理。

### 数据库

schema 变化必须提供显式迁移、幂等性测试、备份要求和回滚说明。不能假设用户可以删除数据库重新配对，因为数据库包含去重和不确定边界状态。

## 发布前检查清单

```bash
git status --short
rg -n --hidden --glob '!.git/**' \
  '(FEISHU_.*SECRET=.+|app_secret\s*=|sk-[A-Za-z0-9]|ou_[A-Za-z0-9]{12,}|oc_[A-Za-z0-9]{12,})' .
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  codex-feishu-bridge/.venv/bin/pytest -p pytest_asyncio.plugin -q \
  codex-feishu-bridge/tests
```

还要人工确认：

- README 的安装路径和当前脚本一致；
- 示例配置使用安全默认值和占位符；
- 新增飞书权限有必要性说明；
- 新增持久字段具备升级与回滚策略；
- Ubuntu 真实测试应用完成私聊、群聊、附件、重启和断网验收；
- 发布说明明确破坏性变化、迁移和已知问题。

## 建议版本策略

在接口稳定前使用 `0.x.y`：

- patch：兼容的修复和文档；
- minor：新能力、schema 兼容迁移或新增平台实验支持；
- major：项目稳定后才承诺的破坏性版本。

Python 包版本、Git tag、Release 标题和迁移文档应一致。不要只在飞书应用后台发布一个 `1.0.0` 就把本地桥也视为同版本；飞书应用版本与桥软件版本是两个独立版本域。

## 路线图

近期优先级：

1. CI：Linux 版本矩阵、测试、格式和秘密扫描；
2. 选择许可证、完善安全联系方式和贡献治理；
3. 正式数据库迁移框架与备份/恢复集成测试；
4. OS-aware doctor，避免在 Windows 报 Linux 专属错误；
5. Windows PowerShell 安装器、服务管理和完整测试；
6. 软件包/Release 构建，减少 editable 源码安装依赖；
7. 飞书文档型长任务报告，支持图片、视频和图表增量更新；
8. 更清晰的磁盘配额、数据保留和附件安全策略；
9. 可观测性指标和健康检查；
10. 未来可选的多主机中央注册/调度层，同时保留每机身份隔离。
