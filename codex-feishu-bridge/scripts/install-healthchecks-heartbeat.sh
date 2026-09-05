#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/codex-feishu-bridge"
HEARTBEAT_ENV="${CONFIG_DIR}/heartbeat.env"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
ENABLE=0

if [[ "${1:-}" == "--enable" ]]; then
  ENABLE=1
elif [[ $# -ne 0 ]]; then
  echo "用法：$0 [--enable]" >&2
  exit 2
fi

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  echo "错误：请先运行 scripts/install-user-service.sh 创建 .venv。" >&2
  exit 1
fi
if ! "${PROJECT_ROOT}/.venv/bin/python" -c 'import codex_feishu_bridge.heartbeat'; then
  echo "错误：当前虚拟环境尚未安装 heartbeat 模块。" >&2
  exit 1
fi

install -d -m 0700 "${CONFIG_DIR}" "${USER_UNIT_DIR}"
if [[ ! -e "${HEARTBEAT_ENV}" ]]; then
  install -m 0600 "${PROJECT_ROOT}/heartbeat.env.example" "${HEARTBEAT_ENV}"
  echo "已创建 ${HEARTBEAT_ENV}"
else
  chmod 0600 "${HEARTBEAT_ENV}"
  echo "保留已有 ${HEARTBEAT_ENV}"
fi

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

escaped_root="$(escape_sed "${PROJECT_ROOT}")"
escaped_env="$(escape_sed "${HEARTBEAT_ENV}")"
escaped_user="$(escape_sed "$(id -un)")"
escaped_group="$(escape_sed "$(id -gn)")"

render() {
  sed \
    -e "s|@PROJECT_ROOT@|${escaped_root}|g" \
    -e "s|@HEARTBEAT_ENV@|${escaped_env}|g" \
    -e "s|@USER@|${escaped_user}|g" \
    -e "s|@GROUP@|${escaped_group}|g" \
    "$1"
}

temporary="$(mktemp -d)"
trap 'rm -rf -- "${temporary}"' EXIT

for name in \
  codex-feishu-bridge-heartbeat.service \
  codex-feishu-bridge-heartbeat.timer; do
  render "${PROJECT_ROOT}/systemd/${name}" >"${temporary}/${name}"
  install -m 0600 "${temporary}/${name}" "${USER_UNIT_DIR}/${name}"
done
systemctl --user daemon-reload

for name in \
  codex-feishu-host-heartbeat.service \
  codex-feishu-host-heartbeat.timer; do
  render "${PROJECT_ROOT}/systemd/${name}" >"${temporary}/${name}"
  sudo install -m 0644 "${temporary}/${name}" "/etc/systemd/system/${name}"
done
sudo systemctl daemon-reload

if [[ ${ENABLE} -eq 1 ]]; then
  # Start each oneshot first.  This validates the EnvironmentFile through
  # systemd without sourcing a credential-bearing file as shell code and also
  # arms both managed checks immediately.
  sudo systemctl start codex-feishu-host-heartbeat.service
  systemctl --user start codex-feishu-bridge-heartbeat.service
  sudo systemctl enable --now codex-feishu-host-heartbeat.timer
  systemctl --user enable --now codex-feishu-bridge-heartbeat.timer
fi

echo
echo "心跳单元已安装。"
echo "1. 将两个 Healthchecks Ping URL 只写入 ${HEARTBEAT_ENV}。"
echo "2. 运行 $0 --enable 启用并立即验证两个 timer。"
echo "3. Ping URL 和飞书自定义机器人 Webhook 都不得发到聊天或提交 Git。"
