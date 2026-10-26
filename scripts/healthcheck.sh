#!/bin/bash
# 轻量健康检查：每 3 分钟一次，8 秒超时，连续 3 次失败才重启；启动 8 分钟内不触发
set -u
STATE=/var/lib/astrbot-healthcheck
LOG=/var/log/astrbot-healthcheck.log
mkdir -p "$STATE"
FAILS="$STATE/fails"
STATUS="$STATE/status"
NOW=$(date +%s)
UPTIME=0
STAMP=$(date '+%F %T')

log() {
  echo "$STAMP $*" >> "$LOG"
}

write_status() {
  printf 'time=%s\nstate=%s\nprobe=%s\npip=%s\nuptime_s=%s\nfails=%s\n' \
    "$STAMP" "$1" "$2" "$3" "$UPTIME" "$4" > "$STATUS"
}

STARTED=$(docker inspect -f '{{.State.StartedAt}}' astrbot 2>/dev/null || echo "")
if [ -z "$STARTED" ]; then
  write_status "missing" "fail" "unknown" "0"
  log "容器不存在，跳过"
  exit 0
fi
START_PLAIN=${STARTED%%.*}
START_PLAIN=${START_PLAIN%Z}Z
START_EPOCH=$(date -d "$START_PLAIN" +%s 2>/dev/null || echo 0)
if [ "$START_EPOCH" -eq 0 ]; then
  write_status "unknown" "fail" "unknown" "0"
  exit 0
fi
UPTIME=$((NOW - START_EPOCH))

PIP="no"
if docker exec astrbot sh -c 'for p in /proc/[0-9]*; do [ "$(cat $p/comm 2>/dev/null)" = "pip" ] && exit 0; done; exit 1' 2>/dev/null; then
  PIP="yes"
fi

if [ "$UPTIME" -lt 480 ]; then
  echo 0 > "$FAILS"
  write_status "starting" "skip" "$PIP" "0"
  log "启动中 ${UPTIME}s，跳过（pip=$PIP）"
  exit 0
fi

if python3 - <<'PY'
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:6185/", timeout=8)
except Exception:
    raise SystemExit(1)
PY
then
  echo 0 > "$FAILS"
  write_status "healthy" "ok" "$PIP" "0"
  log "正常（pip=$PIP）"
  exit 0
fi

if [ "$PIP" = "yes" ]; then
  echo 0 > "$FAILS"
  write_status "installing" "fail" "$PIP" "0"
  log "面板未起：正在安装插件依赖（${UPTIME}s），不重启"
  exit 0
fi

COUNT=$(cat "$FAILS" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$FAILS"
write_status "unhealthy" "fail" "$PIP" "$COUNT"
log "面板无响应，第 ${COUNT}/3 次"
if [ "$COUNT" -ge 3 ]; then
  logger -t astrbot-health "面板连续 3 次无响应，重启 astrbot"
  log "连续 3 次失败，重启 astrbot"
  docker restart astrbot >/dev/null 2>&1
  echo 0 > "$FAILS"
  write_status "restarted" "fail" "$PIP" "0"
fi
