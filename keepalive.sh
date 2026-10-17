#!/bin/bash
# Automatic Post keepalive — ensures run.py web is always running.
set -euo pipefail

PROJECT_DIR="/Users/demo/Desktop/automatic/automatic post"
PID_PATTERN="run.py web"

# Check if already running
if pgrep -f "$PID_PATTERN" > /dev/null 2>&1; then
    exit 0
fi

# Start it
cd "$PROJECT_DIR"
nohup /usr/bin/python3 run.py web >> instance/web-stdout.log 2>> instance/web-stderr.log &
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Started run.py web (pid=$!)" >> instance/web-keepalive.log