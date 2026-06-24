"""run_paper.load_champion — 從 learning_oos_best.json 載入 walk-forward 驗證過的冠軍配置。

paper bot 前進驗證時讀這個檔，自動採用「樣本外最佳」策略/時框；
檔案不存在或損毀 → 退回 fallback（of_momentum / 4h，OOS 最穩健者）。
"""
import json

from run_paper import load_champion


def test_load_champion_from_valid_file(tmp_path):
    p = tmp_path / "best.json"
    p.write_text(json.dumps({
        "symbol": "BTCUSDT", "tf": "4h", "strategy": "of_momentum",
        "oos_expectancy": 8.04, "oos_profit_factor": 1.31, "oos_win_rate": 0.357, "folds": 5,
    }))
    champ = load_champion(str(p))
    assert champ["strategy"] == "of_momentum"
    assert champ["interval"] == "4h"            # tf → interval
    assert champ["symbol"] == "BTCUSDT"
    assert champ["params"] == {}                # 預設參數 WF → 無覆寫
    assert champ["source"] == "learning_oos_best"


def test_load_champion_missing_file_uses_fallback(tmp_path):
    champ = load_champion(str(tmp_path / "nope.json"),
                          fallback_strategy="of_momentum", fallback_interval="4h")
    assert champ["strategy"] == "of_momentum"
    assert champ["interval"] == "4h"
    assert champ["source"] == "fallback"


def test_load_champion_corrupt_file_uses_fallback(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    champ = load_champion(str(p), fallback_strategy="rsi2_connors", fallback_interval="4h")
    assert champ["strategy"] == "rsi2_connors"
    assert champ["source"] == "fallback"


def test_load_champion_carries_params_when_present(tmp_path):
    p = tmp_path / "best.json"
    p.write_text(json.dumps({"strategy": "supertrend", "tf": "1h",
                             "params": {"period": 7, "multiplier": 2.0}}))
    champ = load_champion(str(p))
    assert champ["params"] == {"period": 7, "multiplier": 2.0}
