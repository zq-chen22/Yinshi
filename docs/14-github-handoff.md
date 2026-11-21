# 两台主机不能互相 SSH 时，如何通过 GitHub 协作

唯一源码仓库是 [zq-chen22/Yinshi](https://github.com/zq-chen22/Yinshi)，组件在 `codex-feishu-bridge/`。两台主机只要各自能访问 GitHub，就能读到和提交同一项目；不需要彼此 SSH，也不需要共用机器人、API Key 或 Codex 登录。

## 三个名字，三个用途

| 入口 | 用途 |
| --- | --- |
| `main` | 开发与交接的默认入口，包含实现、测试和操作文档。拉源码不等于服务已升级。 |
| [candidate.json](../deploy/candidate.json) | 已在一台机器验证、可由其他机器按需验证的候选。用标记的 tag 和完整 SHA 复现，不跟随移动的分支。 |
| [stable.json](../deploy/stable.json) | 生产定时发布使用的通道。发布候选源码不代表自动批准升级其他机器。 |

本次候选是 `bridge-v0.4.6-rc.2`，桥 `0.4.6rc2`，配套 Codex CLI `0.170.0`。它包含独立 Claude Code 执行、C 组普通/高级口令、跨组的 `/model` 和 `/status`、按对话共享日志、经用户明确要求的跨组咨询，以及相应测试。[完整功能和使用约束](13-model-groups.md)。这次交接不移动之前的稳定标签 `bridge-v0.4.5`。

## 在另一台 Ubuntu 主机上取得并验证同一份源码

下面从**新的开发目录**开始，不在正在提供服务的目录做 `git pull`。不使用 sudo、API Key 或飞书凭据。只读校验需要 Git 和 Python 3.11+；安装、测试桥组件需要 Python 3.12+。

```bash
git clone https://github.com/zq-chen22/Yinshi.git Yinshi-review
cd Yinshi-review
git fetch origin --tags
python3 codex-feishu-bridge/scripts/verify-release-ref.py --channel candidate
```

核对输出的 tag、完整 SHA、桥和 CLI 版本。此次候选的完整 SHA 是 `eba74e5240e2e3f687ca39b9d3846d8e201ee699`。校验失败即停止，不要覆盖本地 tag。已有独立开发副本的主机先确认 origin、保护未提交改动，再获取最新代码；不要为了同步而清理旧目录。

如要验证候选原样而不是工作中的 main，在上面的新副本内执行：

```bash
git worktree add --detach ../Yinshi-rc2-review refs/tags/bridge-v0.4.6-rc.2
cd ../Yinshi-rc2-review/codex-feishu-bridge
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q
```

这些命令只建立开发副本和测试环境，不启动桥、迁移队列、发飞书消息或调用付费推理。联网下载 Python 依赖和运行可选的真实模型测试是不同的操作；不要打开显式 live 测试开关，除非已经获得相应授权。

## 凭据与上线边界

- Git 分发源码、测试、示例配置和脱敏说明。不要上传真实令牌（包括 `claude-4router.key`）、飞书密钥、SSH 私钥、登录状态、私有节点清单、数据库、附件或聊天日志；不要从另一台机器拷贝它们。
- 未配置 Claude 时可以取得和核验代码，不代表已取得 Claude 调用权限。若准备启用，按[模型组文档的本机配置说明](13-model-groups.md)配置本机自己的客户端和令牌。
- 两边继续开发时，从最新 main 开主题分支，经测试、审查后合回**同一仓库**；不要维护仅在某台机器生效的 site-packages 修改。
- 单独获得上线授权后再按[多主机发布流程](12-fleet-releases.md)在目标主机本地构建、暂存和排空切换。无跨机 SSH 时，仅在目标机使用它自己的单节点私有 inventory（`alias: local`）；选择指定候选须从已验证 tag 的工作树操作，不用会跟随生产 stable 的 `--refresh`。保护原有配置、绑定和队列，核实真实进程及回执，不能把下载或测试成功当作部署完成。
