#!/usr/bin/env bash
# 【在 Mac 上运行】迁移当天用：停掉 Mac 上的服务，导出一致的数据库快照和服务器用的 .env。
#
#   bash deploy/export_from_mac.sh [输出目录]
#   默认输出到项目上一级的 server-migration/（不在仓库里，不会被提交）
#
# 产物：
#   automatic_post.sqlite3.gz         数据库快照（上传到服务器）
#   automatic_post.sqlite3.gz.sha256  校验文件（一起上传，导入时自动校验）
#   env                               服务器用的 .env（上传后改名为 .env；已补 StarRocks 账号）
#
# 撤销（迁移失败、要让 Mac 继续跑）：
#   mv ~/Library/LaunchAgents/disabled/com.automatic-post.web.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.automatic-post.web.plist
#   注意：如果服务器已经跑过并发布了稿件，Mac 上的库就旧了，不能直接撤回，先联系开发。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$(dirname "$APP_DIR")/server-migration}"
LABEL="com.automatic-post.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DISABLED_DIR="$HOME/Library/LaunchAgents/disabled"
DB="$APP_DIR/instance/automatic_post.sqlite3"
SKILL_ENV="$HOME/.trae-cn/skills/dqd-bigdata-sr/.env"
PY=/usr/bin/python3

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "这个脚本只在 Mac 上运行"
[[ -f "$DB" ]] || die "找不到数据库 $DB"

red "这会停掉 Mac 上的 Automatic Post 服务（并取消开机自启），之后由服务器接管。"
read -r -p "确认迁移请输入 YES：" answer
[[ "$answer" == "YES" ]] || die "已取消"

step "停止 Mac 上的服务"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ -f "$PLIST" ]]; then
  mkdir -p "$DISABLED_DIR"
  mv "$PLIST" "$DISABLED_DIR/"
  echo "launchd 配置已移到 $DISABLED_DIR/（不会再自动启动）"
fi
for _ in $(seq 1 30); do
  pgrep -f "run.py web" >/dev/null || break
  sleep 1
done
if pgrep -f "run.py web" >/dev/null; then
  pgrep -fl "run.py web"
  die "仍有 run.py web 进程在跑（可能是手动启动的），请先结束它再重跑本脚本"
fi
green "✓ Mac 上已没有服务进程"

mkdir -p "$OUT"
snapshot="$OUT/automatic_post.sqlite3"
rm -f "$snapshot" "$snapshot.gz" "$snapshot.gz.sha256"

step "导出数据库快照（$(du -h "$DB" | cut -f1)）"
"$PY" - "$DB" "$snapshot" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=60)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst, pages=8192)
src.close()
result = dst.execute("PRAGMA quick_check").fetchone()[0]
count = dst.execute("SELECT count(*) FROM articles").fetchone()[0]
dst.close()
if result != "ok":
    sys.exit(f"快照校验失败: {result}")
print(f"快照校验 ok，articles {count} 行")
EOF

step "压缩并生成校验值"
gzip -f "$snapshot"
(cd "$OUT" && shasum -a 256 automatic_post.sqlite3.gz > automatic_post.sqlite3.gz.sha256)
green "✓ $snapshot.gz（$(du -h "$snapshot.gz" | cut -f1)）"

step "生成服务器用的 env"
cp "$APP_DIR/.env" "$OUT/env"
if [[ -f "$SKILL_ENV" ]]; then
  added=0
  while IFS= read -r line; do
    key="${line%%=*}"
    [[ "$key" =~ ^STARROCKS_[A-Z_]+$ ]] || continue
    grep -qE "^${key}=" "$OUT/env" && continue
    [[ $added == 1 ]] || printf '\n# StarRocks（Mac 上原本从 dqd-bigdata-sr 技能读取，服务器上没有这个文件）\n' >> "$OUT/env"
    echo "$line" >> "$OUT/env"
    added=1
  done < "$SKILL_ENV"
fi
chmod 600 "$OUT/env"
grep -q '^STARROCKS_HOST=.' "$OUT/env" && green "✓ 已补 StarRocks 配置" || red "! env 里没有 StarRocks 配置，联赛点击数据同步会失败"

step "完成，接下来"
cat <<EOF
1. 通过跳板机把下面 3 个文件上传到服务器的 ~/automatic-post/ 目录：
     $snapshot.gz
     $snapshot.gz.sha256
     $OUT/env
2. 在服务器上：
     cd ~/automatic-post && mv env .env && chmod 600 .env
     bash deploy/import_db.sh automatic_post.sqlite3.gz
     sudo systemctl start automatic-post && ./deploy.sh status
3. 上传完成后可删除本地的 ${OUT}（env 里有密钥）。
EOF
