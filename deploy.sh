#!/usr/bin/env bash
# Automatic Post 服务器一键部署。
#
#   ./deploy.sh              拉取 Gitee 最新 main → 装依赖 → 跑测试 → 重启 → 健康检查
#   ./deploy.sh rollback     回退到上一次部署前的版本（也可指定 commit：./deploy.sh rollback abc1234）
#   ./deploy.sh status       服务状态、当前版本、健康检查
#   ./deploy.sh logs [N]     最近 N 行服务日志（默认 100）
#
# 安全约定：
#   - 只做 fast-forward；服务器上有人改了代码或分叉时直接停下，不会硬覆盖。
#   - 测试不过不重启，线上继续跑旧版本；重启后健康检查不过自动回退。
#   - 跳过测试：SKIP_TESTS=1 ./deploy.sh（只在紧急修复时用）。
set -euo pipefail

# git 拉代码会改写本文件。bash 边读边执行，原地改写可能读串，所以总是从一份临时
# 副本执行；拉完代码后再用新版本的 deploy.sh 执行后半程（见 cmd_deploy）。
if [[ -z "${DEPLOY_APP_DIR:-}" ]]; then
  DEPLOY_APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  self_copy="$(mktemp "${TMPDIR:-/tmp}/automatic-post-deploy.XXXXXX")"
  cp "${BASH_SOURCE[0]}" "$self_copy"
  DEPLOY_APP_DIR="$DEPLOY_APP_DIR" exec bash "$self_copy" "$@"
fi
# 已在副本里运行：bash 持有文件句柄，删掉副本不影响继续执行
[[ "${BASH_SOURCE[0]}" == "$DEPLOY_APP_DIR/deploy.sh" ]] || rm -f "${BASH_SOURCE[0]}"

APP_DIR="$DEPLOY_APP_DIR"
SERVICE="${SERVICE:-automatic-post}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
HISTORY="$APP_DIR/instance/deploy_history.log"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
SYSTEMCTL="${SYSTEMCTL:-sudo systemctl}"
JOURNALCTL="${JOURNALCTL:-sudo journalctl}"

cd "$APP_DIR"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

env_value() {
  # 从 .env 读一个变量（不 source，避免执行任意内容）
  local key="$1" default="$2" line
  line="$(grep -E "^${key}=" .env 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  echo "${line:-$default}"
}

health_url() {
  echo "http://127.0.0.1:$(env_value AUTOMATIC_POST_PORT 8890)/health"
}

main_pid() {
  $SYSTEMCTL show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0
}

wait_healthy() {
  # old_pid 非空时，还要求服务进程已换新：防止新版本没起来、旧进程仍在应答而误判成功
  local old_pid="${1:-}" url deadline pid
  url="$(health_url)"
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    pid="$(main_pid)"
    if [[ -n "$old_pid" && ( -z "$pid" || "$pid" == "0" || "$pid" == "$old_pid" ) ]]; then
      sleep 2
      continue
    fi
    if curl -fsS -m 5 "$url" 2>/dev/null | grep -q '"ok": *true'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

install_requirements_if_needed() {
  local from="$1" to="$2"
  if [[ ! -x "$PY" ]]; then
    die "缺少虚拟环境 $VENV，请先运行 deploy/setup.sh"
  fi
  if [[ "$from" == "$to" ]] || ! git diff --quiet "$from" "$to" -- requirements.txt; then
    step "安装依赖（requirements.txt 有变化）"
    "$PY" -m pip install -q -r requirements.txt
  fi
}

run_tests() {
  if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
    red "! 已跳过测试（SKIP_TESTS=1）"
    return 0
  fi
  step "运行测试"
  if [[ -n "${TEST_CMD:-}" ]]; then
    bash -c "$TEST_CMD"
    return
  fi
  local tmp status=0
  tmp="$(mktemp -d)"
  # 测试用临时库，并清空模型/飞书/余额等外部凭据：任何测试都不可能碰到线上库或真实发消息。
  # 开放平台凭据保留：临时库里没有授权 token，发不出稿；部分页面测试依赖它判断「已配置」。
  env AUTOMATIC_POST_DB="$tmp/test.sqlite3" \
    LLM_API_KEY= LLM_API_KEY2= MATERIAL_API_KEY= \
    FEISHU_REPORT_WEBHOOK_URL= SOURCE_REPORT_WEBHOOK_URL= PUBLISH_ALERT_WEBHOOK_URL= \
    BALANCE_ALERT_WEBHOOK_URL= AIGW_BALANCE_ALERT_WEBHOOK_URL= \
    TOKENHUB_ACCESS_TOKEN= AIGW_ACCESS_TOKEN= FEISHU_APP_SECRET= TOKEN_SERVICE_URL= \
    "$PY" -m pytest tests/ -q -p no:cacheprovider || status=$?
  rm -rf "$tmp"
  return "$status"
}

restart_and_check() {
  local old_pid
  old_pid="$(main_pid)"
  [[ "$old_pid" != "0" ]] || old_pid=""
  step "重启服务 $SERVICE"
  $SYSTEMCTL restart "$SERVICE"
  step "健康检查 $(health_url)（最多 ${HEALTH_TIMEOUT}s）"
  wait_healthy "$old_pid"
}

record() {
  mkdir -p "$(dirname "$HISTORY")"
  printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$1" "$2" "$3" >> "$HISTORY"
}

show_tail() {
  step "最近日志"
  $JOURNALCTL -u "$SERVICE" -n 15 --no-pager 2>/dev/null || true
}

switch_to() {
  # 把工作区切到指定 commit（工作区已确认干净）
  git reset -q --hard "$1"
}

cmd_deploy() {
  [[ -f .env ]] || die "缺少 .env，请先上传服务器配置"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no
    die "服务器上的代码有改动，已停止。代码只在本地改、推到 Gitee 后再部署。"
  fi

  local prev new
  prev="$(git rev-parse HEAD)"
  step "拉取 $REMOTE/$BRANCH"
  git fetch -q "$REMOTE" "$BRANCH"
  new="$(git rev-parse "$REMOTE/$BRANCH")"

  if [[ "$prev" == "$new" && "${FORCE:-0}" != "1" ]]; then
    green "✓ 已是最新版本 $(git log -1 --format='%h %s')，无需部署（FORCE=1 可强制重启）"
    return 0
  fi
  if [[ "$prev" != "$new" ]]; then
    git merge -q --ff-only "$REMOTE/$BRANCH" \
      || die "无法快进合并（服务器分支与 Gitee 分叉），已停止，代码未改动"
    echo "本次更新："
    git log --format='  %h %s' "$prev..$new"
    # 后半程（装依赖/测试/重启/回退）交给刚拉下来的新版本 deploy.sh
    exec env -u DEPLOY_APP_DIR bash "$APP_DIR/deploy.sh" _after_pull "$prev" "$new"
  fi
  cmd_after_pull "$prev" "$new"
}

cmd_after_pull() {
  local prev="$1" new="$2"
  install_requirements_if_needed "$prev" "$new"

  if ! run_tests; then
    red "✗ 测试未通过，代码回退到 $(git rev-parse --short "$prev")，服务未重启，线上仍是旧版本"
    switch_to "$prev"
    install_requirements_if_needed "$new" "$prev"
    record "test_failed" "$prev" "$new"
    exit 1
  fi

  if restart_and_check; then
    record "deployed" "$prev" "$new"
    green "✓ 部署成功：$(git log -1 --format='%h %s')"
    show_tail
    return 0
  fi

  red "✗ 新版本健康检查失败，自动回退到 $(git rev-parse --short "$prev")"
  show_tail
  switch_to "$prev"
  install_requirements_if_needed "$new" "$prev"
  if restart_and_check; then
    record "auto_rolled_back" "$prev" "$new"
    red "已回退，线上恢复旧版本。请看上面的日志定位问题。"
  else
    record "auto_rollback_failed" "$prev" "$new"
    red "回退后仍不健康，请立即运行 ./deploy.sh logs 排查"
  fi
  exit 1
}

cmd_rollback() {
  local target="${1:-}" current
  current="$(git rev-parse HEAD)"
  if [[ -z "$target" ]]; then
    # 最近一次成功部署之前的版本
    target="$(awk -F'\t' '$2=="deployed"{t=$3} END{print t}' "$HISTORY" 2>/dev/null || true)"
    [[ -n "$target" ]] || die "没有可回退的部署记录，请指定 commit：./deploy.sh rollback <commit>"
  fi
  target="$(git rev-parse --verify "$target^{commit}")" || die "找不到 commit $target"
  [[ "$target" != "$current" ]] || die "当前已经是 $(git rev-parse --short "$target")"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    die "服务器上的代码有改动，已停止"
  fi
  step "回退 $(git rev-parse --short "$current") → $(git log -1 --format='%h %s' "$target")"
  switch_to "$target"
  install_requirements_if_needed "$current" "$target"
  if restart_and_check; then
    record "rolled_back" "$current" "$target"
    green "✓ 回退成功。下次 ./deploy.sh 会重新拉取 Gitee 最新代码。"
    show_tail
  else
    show_tail
    die "回退后健康检查失败，请运行 ./deploy.sh logs 排查"
  fi
}

cmd_status() {
  step "版本"
  git log -1 --format='%h %s (%cd)' --date=format:'%F %T'
  git fetch -q "$REMOTE" "$BRANCH" 2>/dev/null \
    && echo "Gitee 上比当前新的提交：$(git rev-list --count "HEAD..$REMOTE/$BRANCH")"
  step "服务"
  $SYSTEMCTL --no-pager status "$SERVICE" | head -n 5 || true
  step "健康检查"
  curl -fsS -m 5 "$(health_url)" && echo || red "健康检查失败"
  if [[ -f "$HISTORY" ]]; then
    step "最近部署记录"
    tail -n 5 "$HISTORY"
  fi
}

case "${1:-deploy}" in
  deploy) cmd_deploy ;;
  _after_pull) cmd_after_pull "$2" "$3" ;;
  rollback) cmd_rollback "${2:-}" ;;
  status) cmd_status ;;
  test) run_tests ;;
  logs) $JOURNALCTL -u "$SERVICE" -n "${2:-100}" --no-pager ;;
  *) echo "用法: ./deploy.sh [deploy|rollback [commit]|status|test|logs [N]]"; exit 2 ;;
esac
