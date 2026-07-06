#!/bin/bash
# 每日 forward 追蹤器的排程 wrapper（launchd 呼叫）。
# 用真實主網 4h K 線重跑 smc/4h/rr3 籃子，累積乾淨的樣本外 forward 紀錄。
# 結果壓成單行 append 到 forward_log.jsonl（每行一天，供之後對照/趨勢）。
set -u
REPO="/Users/adem/量化機器"
PY="$REPO/.venv/bin/python"
LOG="$REPO/research/live_audit/forward_log.jsonl"

cd "$REPO" || exit 1
# 擷取 FORWARD_JSON 之後的機器可讀段，用 venv python 壓成單行 jsonl（含 run_at 時間戳）
"$PY" research/scratchpad/daily_forward_tracker.py --json --new-days 1 2>/dev/null \
  | awk '/^FORWARD_JSON$/{flag=1;next} flag' \
  | "$PY" -c 'import sys,json,datetime
raw=sys.stdin.read().strip()
if not raw: sys.exit(0)
rec={"run_at":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),"report":json.loads(raw)}
open(sys.argv[1],"a").write(json.dumps(rec,ensure_ascii=False)+"\n")' "$LOG"
