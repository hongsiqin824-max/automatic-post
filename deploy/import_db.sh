#!/usr/bin/env bash
# 【在服务器上运行】导入 Mac 导出的数据库快照。
#
#   bash deploy/import_db.sh automatic_post.sqlite3.gz
#
# 服务必须是停止状态；已有数据库会先改名保留，不会被覆盖。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="automatic-post"
DB="$APP_DIR/instance/automatic_post.sqlite3"
SRC="${1:-}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

[[ -n "$SRC" && -f "$SRC" ]] || die "用法: bash deploy/import_db.sh <automatic_post.sqlite3.gz>"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  die "服务正在运行，请先 sudo systemctl stop $SERVICE"
fi

if [[ -f "$SRC.sha256" ]]; then
  echo "校验文件完整性…"
  (cd "$(dirname "$SRC")" && sha256sum -c "$(basename "$SRC").sha256") || die "校验失败，文件可能没传完整，请重新上传"
else
  red "! 没有找到 $SRC.sha256，跳过完整性校验"
fi

mkdir -p "$APP_DIR/instance"
tmp="$DB.importing"
echo "解压到 $tmp …"
gunzip -c "$SRC" > "$tmp"

PY="$APP_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
"$PY" - "$tmp" <<'EOF' || { rm -f "$tmp"; exit 1; }
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
result = conn.execute("PRAGMA quick_check").fetchone()[0]
if result != "ok":
    sys.exit(f"数据库校验失败: {result}")
rows = conn.execute("SELECT status, count(*) FROM articles GROUP BY status ORDER BY 2 DESC").fetchall()
latest = conn.execute("SELECT max(created_at) FROM articles").fetchone()[0]
print("数据库校验 ok；最新稿件时间", latest)
for status, count in rows:
    print(f"  {status:<22}{count}")
EOF

if [[ -f "$DB" ]]; then
  keep="$DB.before-import-$(date +%Y%m%d-%H%M%S)"
  mv "$DB" "$keep"
  echo "原数据库已保留为 $keep"
fi
rm -f "$DB-journal" "$DB-wal" "$DB-shm"
mv "$tmp" "$DB"
green "✓ 导入完成：$DB（$(du -h "$DB" | cut -f1)）"
echo "确认 Mac 上的服务已停止后启动：sudo systemctl start $SERVICE && ./deploy.sh status"
