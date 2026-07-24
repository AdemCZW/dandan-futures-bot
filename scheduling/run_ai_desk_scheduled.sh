#!/bin/bash
# ai_desk 排程包裝腳本 — 由 launchd 每 4 小時（對齊 4h K 棒收盤）呼叫一次。
#
# 為什麼需要這層包裝：launchd 執行時的環境極簡（PATH 幾乎是空的、沒有 shell
# profile、工作目錄不是專案根目錄），所以不能直接叫 python，也不能依賴相對路徑。
#
# 刻意不用 `set -e`：BTC 那輪失敗（例如 claude CLI 逾時或憑證過期）時，
# 仍要繼續跑 ETH，不該讓一次失敗把整輪都跳過。

PROJECT_ROOT="/Users/adem/量化機器"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

cd "$PROJECT_ROOT" || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 致命：找不到專案目錄 $PROJECT_ROOT"
    exit 1
}

# 全自動：風控放行的方向性提案會自動核准並在測試網掛限價單。
# 幣種白名單走 .env 的 AI_DESK_SYMBOLS（目前 ETHUSDT,BTCUSDT）。
export AI_DESK_AUTO_APPROVE=true

echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 排程觸發：開始本輪分析"

for SYMBOL in BTCUSDT ETHUSDT; do
    echo "----------------------------------------------------------------"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === $SYMBOL 4h ==="
    "$PYTHON" run_ai_desk_once.py "$SYMBOL" 4h
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $SYMBOL 這輪失敗（exit $STATUS），繼續下一個幣種"
    fi
done

# 處理既有掛單：對帳、成交後補掛停損停利、逾時撤單。
# 與上面的分析分開跑，因為它負責的是「已經掛出去的單」的生命週期。
echo "----------------------------------------------------------------"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 掛單對帳/逾時處理 ==="
AI_DESK_EXEC_ENABLED=true "$PYTHON" run_ai_desk_execute.py

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本輪結束"
