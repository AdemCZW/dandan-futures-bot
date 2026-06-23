"""FastAPI 後端測試 — 全部用 source=synthetic（離線、不需金鑰、不連網）。"""
from fastapi.testclient import TestClient

from webapp.backend.main import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_strategies():
    r = client.get("/api/strategies")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert {"ema_cross", "zscore_revert", "zscore_ls"} <= names
    for s in r.json():
        assert "defaults" in s and "allow_short" in s


def test_backtest_synthetic_shape():
    r = client.post("/api/backtest", json={"strategy": "ema_cross", "source": "synthetic"})
    assert r.status_code == 200
    d = r.json()
    assert set(d["metrics"]) == {"total_return", "max_drawdown", "win_rate", "sharpe", "trades"}
    assert d["bars"] > 0
    assert isinstance(d["equity"], list) and len(d["equity"]) > 0
    assert "t" in d["equity"][0] and "equity" in d["equity"][0]
    assert isinstance(d["trades"], list)


def test_backtest_unknown_strategy_400():
    r = client.post("/api/backtest", json={"strategy": "nope", "source": "synthetic"})
    assert r.status_code == 400


def test_backtest_short_strategy_has_short_trades():
    r = client.post("/api/backtest", json={"strategy": "zscore_ls", "source": "synthetic"})
    assert r.status_code == 200
    dirs = {t["dir"] for t in r.json()["trades"]}
    assert -1 in dirs        # 多空策略應有空單交易


def test_optimize_synthetic_shape():
    r = client.post("/api/optimize", json={"strategy": "ema_cross", "source": "synthetic"})
    assert r.status_code == 200
    d = r.json()
    assert d["combos"] > 0
    hm = d["heatmap"]
    assert hm["xlabel"] and hm["ylabel"] and isinstance(hm["grid"], list) and len(hm["grid"]) > 0
    assert d["walkforward"]["summary"]["folds"] >= 1
    assert isinstance(d["top"], list) and len(d["top"]) > 0


def test_explain_returns_pipeline_and_decision_trace():
    r = client.post("/api/explain", json={"strategy": "ema_cross", "source": "synthetic"})
    assert r.status_code == 200
    d = r.json()
    assert len(d["pipeline"]) == 5 and d["pipeline"][0]["role"] == "市場分析師"
    assert d["decision_points"] >= 1 and len(d["steps"]) == d["decision_points"]
    s = d["steps"][0]
    assert set(["ts", "close", "ind", "target", "actions", "equity", "pos_before", "pos_after"]) <= set(s)
    acts = {a["act"] for a in s["actions"]}
    assert acts & {"entry", "entry_short", "exit_signal", "exit_sltp", "exit_final"}  # 決策點含進出場


def test_trades_endpoint_returns_list():
    r = client.get("/api/trades?limit=5")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_live_endpoint_returns_status():
    # 即時監控：即使沒有 bot 狀態檔 / 連不到行情也要安全回應（不丟例外）
    r = client.get("/api/live")
    assert r.status_code == 200
    d = r.json()
    assert "active" in d and "recent_trades" in d and isinstance(d["recent_trades"], list)
