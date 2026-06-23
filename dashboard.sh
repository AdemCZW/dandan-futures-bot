#!/usr/bin/env bash
# 一鍵啟停「丹丹儀表板」三件套：FastAPI 後端 + paper bot + Vite 前端。
# 用 setsid 完全脫離終端，關掉終端也不會被收掉；日誌寫到 .run/*.log。
#
# 用法：
#   ./dashboard.sh start     # 全部啟動（http://localhost:5173）
#   ./dashboard.sh stop      # 全部停止
#   ./dashboard.sh status    # 看狀態
#   ./dashboard.sh restart
#
# 可用環境變數覆寫：INTERVAL(1m) POLL(15) STRATEGY(ema_cross)
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"
RUN=".run"; mkdir -p "$RUN"
INTERVAL="${INTERVAL:-1m}"; POLL="${POLL:-15}"; STRATEGY="${STRATEGY:-ema_cross}"

_spawn() {  # _spawn <name> <cmd...>
  local name="$1"; shift
  nohup "$@" > "$RUN/$name.log" 2>&1 < /dev/null &
  local pid=$!
  echo $pid > "$RUN/$name.pid"
  echo "  啟動 $name (PID $pid) → $RUN/$name.log"
}

_alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

start() {
  echo "啟動中…"
  _alive backend  || _spawn backend  "$PY" -m uvicorn webapp.backend.main:app --port 8000 --log-level warning
  _alive paperbot || _spawn paperbot "$PY" -u run_paper.py --interval "$INTERVAL" --poll "$POLL" --strategy "$STRATEGY"
  _alive frontend || _spawn frontend npm --prefix webapp/frontend run dev -- --port 5173 --strictPort
  sleep 3
  status
  echo "→ 打開 http://localhost:5173"
}

stop() {
  echo "停止中…"
  for name in frontend paperbot backend; do
    if [ -f "$RUN/$name.pid" ]; then
      pid="$(cat "$RUN/$name.pid")"
      pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null
      echo "  停止 $name (PID $pid)"
      rm -f "$RUN/$name.pid"
    fi
  done
  # 保險：清掉殘留
  pkill -f "uvicorn webapp.backend" 2>/dev/null
  pkill -f "run_paper.py" 2>/dev/null
  pkill -f "vite.*5173" 2>/dev/null
}

status() {
  for name in backend paperbot frontend; do
    if _alive "$name"; then echo "  [運行中] $name (PID $(cat "$RUN/$name.pid"))"
    else echo "  [已停止] $name"; fi
  done
  curl -s -o /dev/null -w "  後端 :8000 → HTTP %{http_code}\n" --max-time 3 http://localhost:8000/api/health 2>/dev/null || echo "  後端 :8000 → 無回應"
  curl -s -o /dev/null -w "  前端 :5173 → HTTP %{http_code}\n" --max-time 3 http://localhost:5173/ 2>/dev/null || echo "  前端 :5173 → 無回應"
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *) echo "用法：$0 {start|stop|restart|status}"; exit 1 ;;
esac
