# 丹丹交易團隊 — 合約測試網 bot 的雲端映像（Railway worker）
# ⚠️ 全程 Binance testnet、虛擬資金、不碰真錢。金鑰由 Railway 環境變數注入，不打包進映像。
FROM python:3.12-slim

WORKDIR /app

# 先裝依賴（最小清單，不含 matplotlib/web；利用 layer cache 加速）
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

# 複製程式碼（.dockerignore 已排除 .env / .venv / node_modules / tests 等）
COPY . .

# 預設啟動合約測試網 bot；實際指令以 railway.json 的 startCommand 為準（可在面板覆寫）
CMD ["python", "-u", "run_live_futures.py", \
     "--strategy", "fib_retracement", "--interval", "1m", \
     "--poll", "15", "--budget", "100", "--leverage", "10"]
