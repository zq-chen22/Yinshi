# 多主机统一发布与无损迁移

唯一源码仓库为 `https://github.com/zq-chen22/Yinshi`。不要再从独立旧仓库部署，
也不要在正在运行的 source 或 site-packages 中修改代码。正式运行包必须来自
不可变 release tag，并记录完整 Git SHA、桥版本和 Codex CLI 版本。

## 统一的代码与独立的主机状态

- 同一发布包包含中文停止/追加、富文本附件解析、群协作者授权、持久收发、
  任务恢复、图片代理、统计及心跳模块。镜像由 SSH 分发，节点不必访问 GitHub。
- App Secret、配对、host_id、数据库、Codex 登录和文件全部保留在各自节点。
- 主机名称、网络代理、工作区授权范围、统计错峰、外部心跳 URL 是本机配置，
  不复制其他节点的配置，也不以统一能力为由扩大工作区权限。
- 迁移现有私有部署时 `data_retention_days=0`，不引入新的历史删除政策。

## 模型策略

配置支持 `random_model`、`random_reasoning_effort`、`random_service_tier`，对标题
包含 random（忽略大小写）的新对话生效。其他新对话继承桥的普通默认值。
每个对话的显式 `/model`、`/effort`、`/fast` 仍有最高优先级。

首次迁移会用一个明确的 `policy_revision` 修改所有已有对话的模型、强度和
速度，保留权限和历史。相同 revision 在后续更新中不重复覆盖用户新选择。
每次切换前读取真实 `model/list`，核实两种模型、强度以及 Fast 层级可用。

## 发布与同步

1. 在 Yinshi 分支开发，完整测试、检查配置模板、打 wheel、扫描待发布内容。
2. 更新 `deploy/stable.json` 的桥/CLI 版本及新 tag；提交源码并发布不可变 tag。
3. 创建 Git bundle、wheelhouse 和逐文件 SHA-256 清单，不包含任何用户数据。
4. `fleet-controller.py reconcile` 经已有、严格校验主机密钥的 SSH 别名分发。
5. 节点在本机 `releases/<sha>` 建立 detached Git worktree 和独立 venv。
6. 用户 timer 检查持久队列；有活动任务或待投递结果时延期，不杀掉当前任务。
7. 持续空闲后才停止旧桥；旧桥的 graceful drain 防护检查与停止之间的新消息。
8. SQLite backup API 在服务停止后生成本机一致性备份。切换只更改运行目录和
   ExecStart，保留原服务的环境文件、代理及配对。
9. 短暂以 KillMode=process 停止旧主进程，避免清理仍有研究用途的后台子进程；
   新服务恢复原来的 mixed 行为。后台研究工作应逐步移到独立用户 service。
10. 核对新 MainPID 的运行路径、active 状态、版本与模型策略，并持久投递部署通知。

控制节点定时从 Yinshi 读取 stable channel，比较所有节点实际 deployment.json。
节点断网时保持现有服务，下次连接再分发。缺少已验证的发布产物时拒绝自动
部署，不能下载一个未经测试的 main 然后冒充成熟发布。

## 运行命令

节点清单只放在控制主机的私有状态目录，示例字段：

```json
{
  "nodes": [{"alias": "local", "home": "/home/USER", "python": "/absolute/python3.12"}],
  "policy": {"model": "gpt-6.1-sol", "effort": "xhigh", "random_model": "gpt-6-astra", "random_effort": "ultra"},
  "policy_revision": "OWNER_APPROVED_REVISION",
  "artifacts_root": "/private/verified-release-artifacts"
}
```

```bash
python3 codex-feishu-bridge/scripts/fleet-controller.py reconcile \
  --repo /path/to/Yinshi --inventory /private/fleet.json
python3 codex-feishu-bridge/scripts/fleet-controller.py status \
  --repo /path/to/Yinshi --inventory /private/fleet.json
```

每个节点的 `codex-feishu-release-activate.timer` 等待空闲窗口；控制节点的用户
timer 定期执行 reconcile。控制主机离线时不会破坏其他节点已运行的版本。

## 回滚与边界

### 构建可重复验证的发布产物

发布者用组件 requirements.lock 准备对应 Python/CPU 平台的依赖 wheelhouse，
从提交的源码构建 bridge wheel；再校验 wheel 与 tag 逐文件一致并创建产物：

    python3 codex-feishu-bridge/scripts/build-fleet-artifact.py \
      --repo /path/to/Yinshi --tag bridge-vVERSION \
      --wheelhouse /private/wheelhouse \
      --cli-package /path/to/official/standalone/package \
      --output-root /private/verified-release-artifacts

cli-package 只能是含 codex-package.json 的官方无状态安装包目录，不是整个
CODEX_HOME。控制 timer 每小时第 20 分钟核对，节点每 30 秒等待空闲；这些检查
走 SSH/本机 SQLite，不消耗飞书 API 额度。既有统计 timer 错峰保持不变。
已安装的统计及桥心跳 job 的 ExecStart 随主服务指向同一发布包，环境变量不变。

### 回滚规则

- 保留原安装目录及私有未提交改动；不执行 reset --hard 或覆盖旧工作树。
- 启动失败时恢复配置与 systemd drop-in，并重新启动旧桥。
- 不把数据库备份直接覆盖回 live DB：这会丢失备份之后收到的消息。
- 本次迁移不删除群、不重建配对、不复制数据库、不强制切断活动 turn。
- 统计/外部心跳要运行仍需各节点拥有文档权限和独立监控 URL。代码能力统一
  不代表可以凭空获得外部服务授权。
