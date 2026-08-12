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

# launchd 的 PATH 極簡（約 /usr/bin:/bin:/usr/sbin:/sbin），找不到 homebrew 裝的
# claude。實際踩過：2026-07-24~27 排程連續多輪全部 exit 1，錯誤是
# FileNotFoundError: No such file or directory: 'claude'。
# 兩層保險：補 PATH，並直接把絕對路徑告訴 ClaudeCliClient。
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export AI_DESK_CLAUDE_BIN="/opt/homebrew/bin/claude"

cd "$PROJECT_ROOT" || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 致命：找不到專案目錄 $PROJECT_ROOT"
    exit 1
}

# ⚠️ 2026-08-12 隔離帳號層級自動記憶（防汙染，見 dandan-ai-desk.md）：
# claude CLI 每次啟動都會自動載入 ~/.claude/projects/.../memory/（帳號層級、
# 跨所有專案共用，不是 ai_desk 專屬），導致四角色辯論讀到「這套系統本身沒有
# edge」之類我對它下的評語，判斷不再是純粹從 briefing 推論。已實測
# --tools none / --system-prompt / --setting-sources / 換模型全部關不掉，
# 唯一有效的辦法是讓記憶資料夾在呼叫期間物理上不存在。
#
# trap 保證不管腳本怎麼結束（正常/中斷/崩潰）記憶都一定會搬回來；啟動時先
# 檢查有沒有上次沒搬回的殘留（例如電腦在還原前睡眠），有就先還原，絕不在
# 已經搬移的狀態上再搬一次。
CLAUDE_MEMORY_DIR="$HOME/.claude/projects/-Users-adem-----/memory"
CLAUDE_MEMORY_BAK="${CLAUDE_MEMORY_DIR}.hidden-by-aidesk"

restore_claude_memory() {
    if [ ! -d "$CLAUDE_MEMORY_BAK" ]; then
        return    # 沒有備份可還原（不是隱藏狀態，或已經還原過），什麼都不做
    fi
    if [ -d "$CLAUDE_MEMORY_DIR" ]; then
        # 2026-08-12 實測踩到：claude CLI 隱藏期間找不到記憶資料夾，會自己在
        # 原路徑重新建一個新的（可能是空的，也可能是它自己的初始化內容）。
        # 若這裡直接 mv 備份回去，目標已存在會變成「搬進資料夾裡巢狀一層」，
        # 不是取代——當場就這樣搞丟了一層。改成先把這個新建的搬到旁邊存查
        # （不刪除，可能有東西），再把真正的備份還原回正確位置。
        mv "$CLAUDE_MEMORY_DIR" "${CLAUDE_MEMORY_DIR}.cli-created-$(date +%s)"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ CLI 隱藏期間自建了新記憶資料夾，已移開存查"
    fi
    mv "$CLAUDE_MEMORY_BAK" "$CLAUDE_MEMORY_DIR"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 記憶資料夾已還原"
}

hide_claude_memory() {
    if [ -d "$CLAUDE_MEMORY_BAK" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ 發現上次殘留的記憶備份，先還原再繼續"
        restore_claude_memory
    fi
    if [ -d "$CLAUDE_MEMORY_DIR" ]; then
        mv "$CLAUDE_MEMORY_DIR" "$CLAUDE_MEMORY_BAK"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 記憶資料夾已暫時隱藏（辯論期間）"
    fi
}

trap restore_claude_memory EXIT INT TERM

# 全自動：風控放行的方向性提案會自動核准並在測試網掛限價單。
# 幣種白名單走 .env 的 AI_DESK_SYMBOLS（目前 ETHUSDT,BTCUSDT）。
export AI_DESK_AUTO_APPROVE=true

echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 排程觸發：開始本輪分析"

hide_claude_memory
for SYMBOL in BTCUSDT ETHUSDT; do
    echo "----------------------------------------------------------------"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === $SYMBOL 4h ==="
    "$PYTHON" run_ai_desk_once.py "$SYMBOL" 4h
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ $SYMBOL 這輪失敗（exit $STATUS），繼續下一個幣種"
    fi
done
restore_claude_memory

# 處理既有掛單：對帳、成交後補掛停損停利、逾時撤單。
# 與上面的分析分開跑，因為它負責的是「已經掛出去的單」的生命週期。
echo "----------------------------------------------------------------"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 掛單對帳/逾時處理 ==="
AI_DESK_EXEC_ENABLED=true "$PYTHON" run_ai_desk_execute.py

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本輪結束"
