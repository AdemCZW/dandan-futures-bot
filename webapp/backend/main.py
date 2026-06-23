"""FastAPI 後端入口。

啟動（從專案根目錄）：
    .venv/bin/uvicorn webapp.backend.main:app --reload --port 8000

所有運算邏輯在 service.py；這裡只負責路由、請求驗證、CORS、錯誤碼。
"""
from __future__ import annotations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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


@app.get("/api/live")
def live():
    return service.live_status()


@app.get("/api/live2")
def live2():
    """第二台 bot（短線 donchian 對照實驗）的即時狀態。未設定 RAILWAY_BOT_URL_2 → active=False。"""
    if not service._RAILWAY_BOT_URL_2:
        return {"active": False, "configured": False}
    out = service.live_status(railway_url=service._RAILWAY_BOT_URL_2)
    out["configured"] = True
    return out


@app.get("/api/hl-leaderboard")
def hl_leaderboard(top_n: int = 30):
    try:
        return service.hyperliquid_leaderboard(top_n=top_n)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/whales")
def whales(symbol: str = "BTCUSDT", period: str = "5m", limit: int = 30):
    try:
        return service.whale_data(symbol=symbol, period=period, limit=limit)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/klines")
def klines(symbol: str = "BTCUSDT", interval: str = "4h",
           limit: int = 200, source: str = "testnet"):
    try:
        return service.klines_data(symbol, interval, limit, source)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/price")
def price(symbol: str = "BTCUSDT"):
    return service.mark_price(symbol)


@app.get("/api/copytraders")
def copytraders(limit: int = 20):
    try:
        return service.binance_copytrading(limit=limit)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/copytrader-positions")
def copytrader_positions(uid: str = ""):
    try:
        return service.binance_copytrader_positions(uid=uid)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/large-trades")
def large_trades(symbol: str = "BTCUSDT", min_usdt: float = 500_000, limit: int = 100):
    try:
        return service.binance_large_trades(symbol=symbol, min_usdt=min_usdt, limit=limit)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
