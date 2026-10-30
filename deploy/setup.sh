#!/usr/bin/env bash
# 服务器首次安装（只需运行一次，重复运行也安全）。
#
#   bash deploy/setup.sh                       # 装虚拟环境、依赖、systemd 服务、每日备份
#   bash deploy/setup.sh --domain post.xxx.com # 同上，并装 Nginx 反向代理
#
# 可选环境变量：
#   PYTHON=python3.11      指定 Python（默认自动找 3.9 及以上）
#   PIP_INDEX_URL=...      pip 镜像（服务器访问不了 pypi.org 时用，如阿里云镜像）
#
# 注意：本脚本只 enable 服务，不会启动。数据库导入完成、确认 Mac 上的服务已停之后，
# 再手动 sudo systemctl start automatic-post —— 两边同时跑会重复发布。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="automatic-post"
RUN_USER="${SUDO_USER:-$(id -un)}"
DOMAIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

cd "$APP_DIR"

step "检查基础命令"
# root 身份下不需要 sudo；非 root 必须有 sudo 才能装 systemd 服务
for cmd in git curl systemctl; do
  command -v "$cmd" >/dev/null || die "缺少命令：$cmd"
done
if [[ "$(id -u)" == "0" ]]; then
  SUDO=""
  green "✓ git / curl / systemctl 都在（以 root 运行）"
else
  command -v sudo >/dev/null || die "缺少命令：sudo"
  SUDO="sudo"
  green "✓ git / curl / sudo / systemctl 都在"
fi

step "查找 Python 3.9+"
pick_python() {
  local c
  for c in ${PYTHON:-} python3.12 python3.11 python3.10 python3.9 python3; do
    [[ -n "$c" ]] && command -v "$c" >/dev/null || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PYTHON_BIN="$(pick_python)" || die "没有 Python 3.9 及以上。Ubuntu: sudo apt install python3 python3-venv；CentOS/Rocky: sudo dnf install python3.11"
"$PYTHON_BIN" -c 'import venv, sqlite3, ensurepip' 2>/dev/null \
  || die "$PYTHON_BIN 缺少 venv/ensurepip 模块。Ubuntu: sudo apt install python3-venv（或对应版本 python3.X-venv）"
green "✓ $($PYTHON_BIN --version)（$(command -v "$PYTHON_BIN")）"

step "创建虚拟环境并安装依赖"
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
green "✓ 依赖已安装"

step "检查 .env"
mkdir -p instance
if [[ ! -f .env ]]; then
  red "! 还没有 .env：请通过跳板机把 Mac 上导出的 .env 上传到 $APP_DIR/.env 后重新运行本脚本"
  ENV_OK=0
else
  chmod 600 .env
  ENV_OK=1
  host="$(grep -E '^AUTOMATIC_POST_HOST=' .env | tail -n1 | cut -d= -f2- || true)"
  if [[ -n "$host" && "$host" != "127.0.0.1" ]]; then
    die ".env 里 AUTOMATIC_POST_HOST=$host —— 服务没有登录功能，必须是 127.0.0.1，对外经 Nginx"
  fi
  for key in LLM_API_KEY MATERIAL_API_KEY DQD_OPEN_APPID DQD_OPEN_APPSECRET STARROCKS_HOST STARROCKS_PASSWORD; do
    grep -qE "^${key}=.+" .env || red "! .env 缺少 $key"
  done
  green "✓ .env 已就位（权限 600）"
fi
PORT="$(grep -E '^AUTOMATIC_POST_PORT=' .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
PORT="${PORT:-8890}"

step "运行测试（临时库，不碰线上数据、不发真实消息）"
bash ./deploy.sh test || die "测试未通过，请把输出发给开发排查"
green "✓ 测试通过"

step "安装 systemd 服务 ${SERVICE}（用户 ${RUN_USER}）"
sed -e "s#__USER__#${RUN_USER}#g" -e "s#__APP_DIR__#${APP_DIR}#g" deploy/automatic-post.service \
  | $SUDO tee "/etc/systemd/system/${SERVICE}.service" >/dev/null
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE" >/dev/null 2>&1
green "✓ 已安装并设为开机自启（尚未启动）"

step "安装每日备份（每天 04:30，保留 7 份）"
chmod +x deploy/backup.sh deploy.sh
echo "30 4 * * * ${RUN_USER} /bin/bash '${APP_DIR}/deploy/backup.sh' >> '${APP_DIR}/instance/backup.log' 2>&1" \
  | $SUDO tee "/etc/cron.d/${SERVICE}-backup" >/dev/null
$SUDO chmod 644 "/etc/cron.d/${SERVICE}-backup"
green "✓ /etc/cron.d/${SERVICE}-backup"

if [[ -n "$DOMAIN" ]]; then
  step "安装 Nginx 反向代理 $DOMAIN → 127.0.0.1:$PORT"
  if ! command -v nginx >/dev/null; then
    red "! 未安装 nginx。Ubuntu: sudo apt install nginx；CentOS/Rocky: sudo dnf install nginx。装好后重跑本脚本"
  else
    sed -e "s#__DOMAIN__#${DOMAIN}#g" -e "s#__PORT__#${PORT}#g" deploy/nginx-automatic-post.conf \
      | $SUDO tee "/etc/nginx/conf.d/${SERVICE}.conf" >/dev/null
    $SUDO nginx -t && $SUDO systemctl enable --now nginx >/dev/null 2>&1 && $SUDO systemctl reload nginx
    green "✓ /etc/nginx/conf.d/${SERVICE}.conf"
  fi
fi

step "完成"
if [[ -f instance/automatic_post.sqlite3 ]]; then
  echo "数据库已在 instance/。确认 Mac 上的服务已停止后启动："
else
  echo "下一步：上传数据库并运行 bash deploy/import_db.sh <文件>，确认 Mac 上的服务已停止后启动："
fi
echo "  sudo systemctl start $SERVICE && ./deploy.sh status"
[[ "$ENV_OK" == "1" ]] || red "（别忘了先上传 .env）"
