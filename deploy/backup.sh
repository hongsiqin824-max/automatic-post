#!/usr/bin/env bash
# 每日备份线上 SQLite：用 SQLite 在线备份接口拷一份一致的快照（服务运行中也安全），
# 压缩后放 instance/backup/，保留最近 KEEP 份。由 setup.sh 装进 /etc/cron.d。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${AUTOMATIC_POST_DB:-$APP_DIR/instance/automatic_post.sqlite3}"
BACKUP_DIR="$APP_DIR/instance/backup"
KEEP="${KEEP:-7}"
PY="$APP_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

[[ -f "$DB" ]] || { echo "数据库不存在：$DB" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/automatic_post-$(date +%Y%m%d-%H%M%S).sqlite3"

"$PY" - "$DB" "$target" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=60)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst, pages=4096)
src.close()
dst.close()
EOF
gzip -f "$target"

# 只保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/automatic_post-*.sqlite3.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date '+%F %T') 备份完成 ${target}.gz ($(du -h "${target}.gz" | cut -f1))"
