#!/usr/bin/env bash
# Hermes Console 一键安装脚本
# 用法：bash install.sh [安装目录，默认 ~/hermes-console]
# 功能：下载代码 → 装依赖 → 自动扫描本机Hermes实例 → 启动
set -euo pipefail

INSTALL_DIR="${1:-$HOME/hermes-console}"
PORT="${CONSOLE_PORT:-8787}"
REPO="https://github.com/qq5871480/hermes-console.git"

echo "==> Hermes Console 一键安装"

# 0. 依赖检查
command -v python3 >/dev/null || { echo "✗ 需要 python3（3.10+）"; exit 1; }
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "✗ Python 版本 $PYV 太低，需要 3.10+"; exit 1; }
command -v git >/dev/null || { echo "✗ 需要 git（国内网络不通可用镜像：git clone https://gitclone.com/github.com/qq5871480/hermes-console）"; exit 1; }

# 1. 下载代码（已存在则更新）
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> 已有安装，拉取最新代码…"
  git -C "$INSTALL_DIR" pull --ff-only || echo "  （拉取失败，用现有代码继续）"
else
  echo "==> 下载代码到 $INSTALL_DIR …"
  git clone --depth 1 "$REPO" "$INSTALL_DIR" || {
    echo "  GitHub 直连失败，尝试镜像…"
    git clone --depth 1 "https://gitclone.com/github.com/qq5871480/hermes-console.git" "$INSTALL_DIR"
  }
fi
cd "$INSTALL_DIR"

# 2. 装依赖
echo "==> 创建虚拟环境并安装依赖…"
# Ubuntu/Debian 常缺 python3-venv（ensurepip不可用），实际试建检测
if ! python3 -m venv /tmp/.hc_venv_test >/dev/null 2>&1; then
  rm -rf /tmp/.hc_venv_test
  PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  echo "  缺少 python3-venv，尝试自动安装（需要sudo）…"
  if command -v apt-get >/dev/null && sudo -n true 2>/dev/null; then
    sudo apt-get install -y -q "python${PYVER}-venv" >/dev/null 2>&1 || sudo apt-get install -y -q python3-venv >/dev/null 2>&1
  fi
  python3 -m venv /tmp/.hc_venv_test >/dev/null 2>&1 || { echo "✗ venv创建失败，请手动执行：sudo apt install python${PYVER}-venv"; exit 1; }
fi
rm -rf /tmp/.hc_venv_test
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
# 国内pip慢可用：./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
./venv/bin/pip install -q -r requirements.txt

# 3. 自动扫描 Hermes 实例
echo "==> 扫描本机 Hermes 实例…"
HERMES_DIR="${HERMES_HOME:-}"
if [ -z "$HERMES_DIR" ]; then
  for d in "$HOME/.hermes" /opt/hermes /home/*/.hermes; do
    if [ -f "$d/config.yaml" ]; then HERMES_DIR="$d"; break; fi
  done
fi
if [ -z "$HERMES_DIR" ]; then
  echo "  ⚠ 未找到 Hermes 实例（config.yaml）。将使用默认 ~/.hermes。"
  echo "    如你的实例在别处，启动前设置：export HERMES_HOME=/路径/到/.hermes"
  HERMES_DIR="$HOME/.hermes"
else
  echo "  ✓ 找到实例：$HERMES_DIR"
fi

# 4. 启动
export HERMES_HOME="$HERMES_DIR"
export CONSOLE_BACKUP_DIR="${CONSOLE_BACKUP_DIR:-$HOME/hermes-console-backups}"
export CONSOLE_GATEWAY_SERVICE="${CONSOLE_GATEWAY_SERVICE:-hermes-gateway}"

# 5. 开机自启（默认自动开启；显式 AUTOSTART=n / NO_AUTOSTART=1 跳过）
if [ "${AUTOSTART_REPLY:-${NO_AUTOSTART:-}}" = "n" ] || [ "${AUTOSTART_REPLY:-}" = "N" ] || [ "${NO_AUTOSTART:-0}" = "1" ]; then
  echo "==> 按设置跳过开机自启，以前台方式启动（Ctrl+C 停止）…"
  echo "    访问：http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '服务器IP'):$PORT"
  echo
  exec ./venv/bin/gunicorn -w 1 --threads 8 --timeout 700 -b "0.0.0.0:$PORT" app:app
fi
echo "==> 设置开机自启（systemd 用户级服务）…"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/hermes-console.service" <<UNIT
[Unit]
Description=Hermes Console - Web management UI
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PWD
Environment=HERMES_HOME=$HERMES_DIR
Environment=CONSOLE_BACKUP_DIR=$CONSOLE_BACKUP_DIR
Environment=CONSOLE_GATEWAY_SERVICE=$CONSOLE_GATEWAY_SERVICE
ExecStart=$PWD/venv/bin/gunicorn -w 1 --threads 8 --timeout 700 -b 0.0.0.0:$PORT app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now hermes-console.service
  # 保证用户服务在不登录时也常驻
  loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "Linger=yes" || sudo -n loginctl enable-linger "$USER" 2>/dev/null || echo "  ⚠ 未开启linger：服务器重启后需登录一次才拉起服务（sudo loginctl enable-linger $USER 可开启）"
echo "  ✓ 开机自启已设置：systemctl --user status hermes-console"
echo "    访问：http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '服务器IP'):$PORT"
echo "    首次打开网页即进入「创建管理员密码」页面——没有默认密码，谁先访问谁设置，"
echo "    请立即在浏览器中完成设置（公网环境尤其要第一时间设置！）"
echo
