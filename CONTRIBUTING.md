# 贡献指南

感谢参与“因时而得谷”。当前项目仍在建立稳定接口与跨平台安装流程，提交变更前请先阅读 [项目边界](docs/01-project-overview.md)、[开发与开源计划](docs/08-development-and-open-source.md) 和 [安全说明](SECURITY.md)。

## 开发流程

1. 从主分支创建主题分支。
2. 只提交源码、测试、模板和文档；不得提交 Secret、用户消息、数据库、Codex 登录信息或真实附件。
3. 为行为变化补充测试。
4. 在 `codex-feishu-bridge/` 下运行完整测试。
5. 更新相关文档和配置示例。
6. 在 Pull Request 中说明风险、验证证据以及是否影响数据库或配置兼容性。

## 提交前检查

```bash
cd codex-feishu-bridge
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/pytest -p pytest_asyncio.plugin -q
```

还应检查：

- `git diff --check`
- 没有真实 App ID/Secret、访问令牌、Cookie、私聊 open_id 或 chat_id
- 没有 `*.sqlite*`、inbox、outbox、`.venv`、`auth.json`
- 新增外部写操作有清晰授权和幂等边界
- 崩溃恢复不会自动重放状态不明的外部副作用

## 兼容性原则

- 飞书事件 handler 必须快速持久化后返回，实际工作由后台队列完成。
- Codex App Server 协议升级必须先探测能力，不得只按版本号假定兼容。
- 数据库迁移必须可重复执行并保留旧数据。
- 同一 Codex thread 同时只能有一个明确的前台写入者。
- Windows 支持变更必须在 Windows CI 或真实 Windows 主机验证。
