#!/bin/bash
# run_sync_daemon.sh — arXiv 每日元数据同步守护进程
#
# 用法:
#   bash run_sync_daemon.sh              # 后台启动 (默认)
#   bash run_sync_daemon.sh --fg          # 前台运行 (调试用)
#   bash run_sync_daemon.sh --once        # 只跑一次, cron 用
#
# 日志目录: logs/sync_daemon/
#   每次运行写入 run_YYYYMMDD_HHMMSS.log，保留最近 10 个。

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs/sync_daemon"
mkdir -p "$LOG_DIR"

PID_FILE="$LOG_DIR/daemon.pid"
MAX_LOG_FILES=10

rotate_logs() {
    local pattern="$LOG_DIR/run_*.log"
    local count
    count=$(ls $pattern 2>/dev/null | wc -l)
    if [ "$count" -ge "$MAX_LOG_FILES" ]; then
        ls -t $pattern | tail -n +$((MAX_LOG_FILES + 1)) | xargs -r rm -f
    fi
}

# ── 如果请求前台 (--fg), 跳过 fork, 直接执行 ──
if [ "$1" = "--fg" ]; then
    shift
else
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[$(date)] daemon already running (PID $OLD_PID), exiting"
            exit 0
        fi
    fi
    echo "[$(date)] starting daemon in background... (logs: $LOG_DIR/)"
    nohup bash "$0" --fg "$@" >> "$LOG_DIR/daemon.log" 2>&1 &
    disown
    echo "[$(date)] daemon started (PID $!)"
    exit 0
fi

# ── 以下在前台执行 (--fg 或被 fork 调用) ──

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] daemon already running (PID $OLD_PID), exiting"
        exit 0
    fi
fi
echo $$ > "$PID_FILE"
trap "rm -f $PID_FILE" EXIT

ONCE=false
[ "$1" = "--once" ] && ONCE=true

LOOP_SLEEP=$((6 * 3600))
RUN=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$RUN] $*"
}

log "daemon started (pid=$$, once=$ONCE, loop=${LOOP_SLEEP}s)"

while true; do
    RUN=$((RUN + 1))
    rotate_logs
    RUN_LOG="$LOG_DIR/run_$(date '+%Y%m%d_%H%M%S').log"
    log "running sync → $RUN_LOG"
    export PYTHONDONTWRITEBYTECODE=1
    cd "$ROOT" && uv run compass service sync > "$RUN_LOG" 2>&1
    log "sync finished (rc=$?)"
    $ONCE && log "once mode: exiting" && break
    log "sleeping ${LOOP_SLEEP}s until next cycle"
    sleep "$LOOP_SLEEP"
done
