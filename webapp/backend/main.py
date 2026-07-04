"""FastAPI 後端入口。

啟動（從專案根目錄）：
    .venv/bin/uvicorn webapp.backend.main:app --reload --port 8000

所有運算邏輯在 service.py；這裡只負責路由、請求驗證、CORS、錯誤碼。
"""
from __future__ import annotations
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webapp.backend import service

app = FastAPI(title="丹丹交易團隊 API", version="1.0")

# 本機開發：放行所有來源（前端 vite 預設跑在 :5173）。正式部署請收斂白名單。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BacktestReq(BaseModel):
    strategy: str = "ema_cross"
    symbol: str = "BTCUSDT"
    interval: str = "5m"
    params: dict | None = None
    source: str = "synthetic"        # synthetic（離線）| testnet（公開行情）
    limit: int = 1000


class ExplainReq(BaseModel):
    strategy: str = "ema_cross"
    symbol: str = "BTCUSDT"
    interval: str = "5m"
    params: dict | None = None
    source: str = "synthetic"
    limit: int = 1000
    only_decisions: bool = True


class OptimizeReq(BaseModel):
    strategy: str = "ema_cross"
    source: str = "synthetic"
    objective: str = "sharpe"
    train: int = 2000
    test: int = 500
    limit: int = 1000


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/strategies")
def strategies():
    return service.list_strategies()


@app.post("/api/backtest")
def backtest(req: BacktestReq):
    try:
        return service.run_backtest_api(req.strategy, req.symbol, req.interval,
                                        req.params, req.source, req.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/explain")
def explain(req: ExplainReq):
    try:
        return service.run_explain_api(req.strategy, req.symbol, req.interval,
                                       req.params, req.source, req.limit, req.only_decisions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/optimize")
def optimize(req: OptimizeReq):
    try:
        return service.run_optimize_api(req.strategy, req.source, req.objective,
                                        req.train, req.test, req.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/trades")
def trades(limit: int = 50, mode: str | None = None):
    return service.read_trades(limit=limit, mode=mode)


# 2026-07-05 清理：移除 /api/live2/3/4、/api/live-all、/api/liveN/close 遺留端點——
# 它們代理 RAILWAY_BOT_URL_2/3/4 指向的「舊分離 bot 服務」，該些服務已關閉合併進
# 單一 BOTS_CONFIG 容器（/bots + /{id}/live + /api/close/{bot_id} 為現行路徑）。
@app.get("/api/live")
def live():
    return service.live_status()


@app.post("/api/live/close")
def live_close():
    """向後相容：代理合併容器根路由的平倉（root → 第一台 bot）。"""
    return service.close_position(service._RAILWAY_BOT_URL, service._CLOSE_TOKEN)


@app.post("/api/close/{bot_id}")
def generic_close(bot_id: str):
    """通用平倉代理（N 台籃子）：/api/close/b5 → <bot根URL>/b5/close。

    bot_id 白名單 ^b[1-9][0-9]?$（id 即路由段，擋穿越/怪字元）。
    根 URL 沿用 RAILWAY_BOT_URL（合併容器的根，向後相容端點也掛同一處）。
    """
    import re
    if not re.fullmatch(r"b[1-9][0-9]?", bot_id):
        raise HTTPException(status_code=404, detail="unknown bot id")
    base = service._RAILWAY_BOT_URL
    if not base:
        return {"ok": False, "msg": "未設 RAILWAY_BOT_URL"}
    return service.close_position(f"{base}/{bot_id}", service._CLOSE_TOKEN)


@app.get("/api/klines")
def klines(symbol: str = "BTCUSDT", interval: str = "4h",
           limit: int = 200, source: str = "testnet"):
    try:
        return service.klines_data(symbol, interval, limit, source)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/ma6")
def ma6(symbol: str = "BTCUSDT", interval: str = "4h", limit: int = 300, source: str = "testnet"):
    """六線密集/發散圖表資料（雙均線系統版面）。"""
    try:
        return service.ma6_overlay_data(symbol, interval, limit, source)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/trade-markers")
def trade_markers(symbol: str = "BTCUSDT", bucket_hours: int = 6, limit: int = 5000):
    """機器人下單點：全部紀錄，每 bucket_hours 小時聚合一個點，依 bot 分色並標明 mode。"""
    try:
        return service.trade_markers(symbol=symbol, bucket_hours=bucket_hours, limit=limit)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/price")
def price(symbol: str = "BTCUSDT"):
    return service.mark_price(symbol)


# ── 靜態前端 ──────────────────────────────────────────────────────────────────
# 正式部署時把打包好的 React（webapp/frontend/dist）掛在 "/"，前端同源呼叫 /api，
# 不需 CORS。本機開發（前端跑 vite :5173、無 dist）則自動略過此掛載。
# 掛在所有 /api 路由「之後」，確保 API 優先匹配。
_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
