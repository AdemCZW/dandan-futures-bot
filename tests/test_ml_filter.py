"""TDD tests for ml/ package.

RED → GREEN → REFACTOR order.
All tests written BEFORE any implementation.

Covers:
  ml.triple_barrier  — Triple Barrier labeling
  ml.purged_kfold    — PurgedKFold cross-validator
  ml.ml_filter       — XGBoost feature extraction + training + inference
"""
import math
import os
import tempfile
import numpy as np
import pandas as pd
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ────────────────────────────────────────────────────────────────

def make_close(n: int = 500, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.01, n)
    prices = 100 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.Series(prices, index=idx, name="close")


def make_ohlcv(n: int = 500, seed: int = 0) -> pd.DataFrame:
    close = make_close(n, seed)
    rng = np.random.default_rng(seed + 1)
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    return pd.DataFrame({
        "open": np.r_[close.iloc[0], close.iloc[:-1].values],
        "high": high, "low": low, "close": close,
        "volume": rng.lognormal(3, 0.5, n),
    }, index=close.index)


# ═══════════════════════════════════════════════════════════════════════════
# Part 1 — Triple Barrier
# ═══════════════════════════════════════════════════════════════════════════

class TestTripleBarrier:
    """ml.triple_barrier.label_triple_barrier must correctly assign +1/-1/0."""

    def test_returns_series_with_same_index_as_events(self):
        from ml.triple_barrier import label_triple_barrier
        close = make_close(200)
        events = close.index[[10, 50, 100]]
        labels = label_triple_barrier(close, events, pt=0.05, sl=0.05, vb=20)
        assert isinstance(labels, pd.Series)
        assert list(labels.index) == list(events)

    def test_label_plus1_when_price_rises_sharply(self):
        """Synthetic series: immediate +10% jump → profit barrier hit → +1."""
        from ml.triple_barrier import label_triple_barrier
        idx = pd.date_range("2024-01-01", periods=50, freq="1h")
        # flat then big jump
        prices = [100.0] * 5 + [110.0] * 45
        close = pd.Series(prices, index=idx)
        events = pd.DatetimeIndex([idx[3]])
        labels = label_triple_barrier(close, events, pt=0.05, sl=0.05, vb=30)
        assert labels.iloc[0] == 1

    def test_label_minus1_when_price_drops_sharply(self):
        """Synthetic series: immediate -10% drop → stop loss hit → -1."""
        from ml.triple_barrier import label_triple_barrier
        idx = pd.date_range("2024-01-01", periods=50, freq="1h")
        prices = [100.0] * 5 + [88.0] * 45
        close = pd.Series(prices, index=idx)
        events = pd.DatetimeIndex([idx[3]])
        labels = label_triple_barrier(close, events, pt=0.05, sl=0.05, vb=30)
        assert labels.iloc[0] == -1

    def test_label_zero_when_price_flat_until_vertical_barrier(self):
        """Flat price → no horizontal barrier hit → vertical barrier → 0."""
        from ml.triple_barrier import label_triple_barrier
        idx = pd.date_range("2024-01-01", periods=50, freq="1h")
        close = pd.Series(100.0, index=idx)
        events = pd.DatetimeIndex([idx[0]])
        labels = label_triple_barrier(close, events, pt=0.05, sl=0.05, vb=10)
        assert labels.iloc[0] == 0

    def test_labels_only_contain_valid_values(self):
        from ml.triple_barrier import label_triple_barrier
        close = make_close(300)
        events = close.index[::20][:10]
        labels = label_triple_barrier(close, events, pt=0.03, sl=0.03, vb=15)
        assert set(labels.values).issubset({-1, 0, 1})

    def test_multiple_events_are_labeled_independently(self):
        from ml.triple_barrier import label_triple_barrier
        close = make_close(400)
        events = close.index[[10, 100, 200, 300]]
        labels = label_triple_barrier(close, events, pt=0.04, sl=0.04, vb=20)
        assert len(labels) == 4
        assert not labels.isna().any()


# ═══════════════════════════════════════════════════════════════════════════
# Part 2 — PurgedKFold
# ═══════════════════════════════════════════════════════════════════════════

class TestPurgedKFold:
    """ml.purged_kfold.PurgedKFold: time-aware CV that purges overlapping labels."""

    def _make_X_t1(self, n: int = 100, hold: int = 5):
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        X = pd.DataFrame({"feat": np.arange(n)}, index=idx)
        t1 = idx.shift(hold)[:n]      # label spans [i, i+hold]
        return X, pd.Series(t1, index=idx)

    def test_returns_correct_number_of_splits(self):
        from ml.purged_kfold import PurgedKFold
        X, t1 = self._make_X_t1()
        cv = PurgedKFold(n_splits=5, t1=t1)
        splits = list(cv.split(X))
        assert len(splits) == 5

    def test_train_and_test_indices_dont_overlap(self):
        from ml.purged_kfold import PurgedKFold
        X, t1 = self._make_X_t1()
        cv = PurgedKFold(n_splits=5, t1=t1)
        for train, test in cv.split(X):
            assert len(set(train) & set(test)) == 0

    def test_test_indices_cover_all_samples_across_splits(self):
        from ml.purged_kfold import PurgedKFold
        X, t1 = self._make_X_t1(n=100)
        cv = PurgedKFold(n_splits=5, t1=t1)
        seen = set()
        for _, test in cv.split(X):
            seen.update(test)
        assert len(seen) == len(X)

    def test_purged_samples_not_in_train_when_label_overlaps_test(self):
        """A training sample whose label-end falls inside the test window is purged."""
        from ml.purged_kfold import PurgedKFold
        n = 50
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        X = pd.DataFrame({"f": range(n)}, index=idx)
        t1 = pd.Series(idx.shift(10)[:n], index=idx)   # 10-bar labels
        cv = PurgedKFold(n_splits=5, t1=t1)
        for train, test in cv.split(X):
            test_start = X.index[test[0]]
            for tr_i in train:
                label_end = t1.iloc[tr_i]
                # no training sample's label should bleed into test window
                assert label_end <= test_start or X.index[tr_i] > X.index[test[-1]], (
                    f"Label end {label_end} bleeds into test starting {test_start}"
                )

    def test_embargo_shrinks_train_size(self):
        """With embargo > 0 fewer training samples than without."""
        from ml.purged_kfold import PurgedKFold
        X, t1 = self._make_X_t1(n=200, hold=3)
        cv_no_emb = PurgedKFold(n_splits=5, t1=t1, pct_embargo=0.0)
        cv_emb    = PurgedKFold(n_splits=5, t1=t1, pct_embargo=0.05)
        train_no  = sum(len(tr) for tr, _ in cv_no_emb.split(X))
        train_emb = sum(len(tr) for tr, _ in cv_emb.split(X))
        assert train_emb <= train_no


# ═══════════════════════════════════════════════════════════════════════════
# Part 3 — ML Filter (XGBoost)
# ═══════════════════════════════════════════════════════════════════════════

class TestMlFilter:
    """ml.ml_filter: feature extraction + XGBoost training + inference + I/O."""

    def _make_prepared(self, n: int = 300) -> pd.DataFrame:
        df = make_ohlcv(n)
        rng = np.random.default_rng(99)
        df["atr"]        = df["close"] * rng.uniform(0.005, 0.02, n)
        df["adx"]        = rng.uniform(10, 50, n)
        df["rsi"]        = rng.uniform(20, 80, n)
        df["er"]         = rng.uniform(0, 1, n)
        df["choppiness"] = rng.uniform(38, 62, n)
        df["regime"]     = rng.choice(["trend", "range"], n)
        return df

    def test_extract_features_returns_dataframe(self):
        from ml.ml_filter import extract_features
        prepared = self._make_prepared()
        events   = prepared.index[::30][:5]
        feats    = extract_features(prepared, events)
        assert isinstance(feats, pd.DataFrame)
        assert len(feats) == len(events)

    def test_extract_features_has_expected_columns(self):
        from ml.ml_filter import extract_features, FEATURE_COLS
        prepared = self._make_prepared()
        events   = prepared.index[::30][:5]
        feats    = extract_features(prepared, events)
        for col in FEATURE_COLS:
            assert col in feats.columns, f"Missing feature column: {col}"

    def test_train_filter_returns_fitted_model(self):
        from ml.ml_filter import extract_features, train_filter
        prepared = self._make_prepared(300)
        events   = prepared.index[::20][:10]
        X = extract_features(prepared, events)
        y = pd.Series([1, -1, 1, 0, 1, -1, 0, 1, -1, 1], index=events)
        model = train_filter(X, y)
        assert model is not None
        assert hasattr(model, "predict_proba")

    def test_predict_proba_returns_float_in_0_1(self):
        from ml.ml_filter import extract_features, train_filter, signal_proba
        prepared = self._make_prepared(300)
        events   = prepared.index[::20][:12]
        X = extract_features(prepared, events)
        y = pd.Series([1, -1, 1, 0, 1, -1, 0, 1, -1, 1, -1, 1], index=events)
        model = train_filter(X, y)
        row   = X.iloc[[-1]]
        p     = signal_proba(model, row)
        assert isinstance(p, float)
        assert 0.0 <= p <= 1.0

    def test_save_and_load_roundtrip(self):
        from ml.ml_filter import extract_features, train_filter, save_filter, load_filter
        prepared = self._make_prepared(300)
        events   = prepared.index[::20][:12]
        X = extract_features(prepared, events)
        y = pd.Series([1, -1, 1, 0, 1, -1, 0, 1, -1, 1, -1, 1], index=events)
        model = train_filter(X, y)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "filter.pkl")
            save_filter(model, path)
            loaded = load_filter(path)
        row = X.iloc[[-1]]
        assert hasattr(loaded, "predict_proba")
        # same prediction after roundtrip
        assert loaded.predict_proba(row)[0, 1] == model.predict_proba(row)[0, 1]

    def test_model_improves_over_random_on_synthetic_data(self):
        """Model trained on separable data should beat 50% accuracy."""
        from ml.ml_filter import extract_features, train_filter
        from sklearn.model_selection import cross_val_score
        n = 400
        prepared = self._make_prepared(n)
        events   = prepared.index[10::5][:60]
        X = extract_features(prepared, events)
        # make labels correlated with RSI: high RSI → +1, low RSI → -1
        y_vals = np.where(X["rsi"] > 50, 1, -1)
        y = pd.Series(y_vals, index=events)
        model = train_filter(X, y)
        scores = cross_val_score(model, X, (y == 1).astype(int), cv=3)
        assert scores.mean() > 0.5, f"Model accuracy {scores.mean():.2f} not > 0.5"


# ═══════════════════════════════════════════════════════════════════════════
# Part 4 — ML 過濾層升級（2026-07-04 撿起 #55）：修 look-ahead 洩漏 + 類別不平衡
# ═══════════════════════════════════════════════════════════════════════════

class TestVolZNoLookahead:
    """vol_z 舊版用「整段資料」的 mean/std，等於偷看未來（訓練時洩漏，實盤推論時
    的分佈跟訓練時不同）。修復：只能用「當下時間點為止」的滾動視窗。"""

    def _make_prepared(self, n=300, seed=1):
        df = make_ohlcv(n, seed)
        rng = np.random.default_rng(99)
        df["atr"] = df["close"] * 0.01
        df["adx"] = 20.0
        df["rsi"] = 50.0
        df["er"] = 0.5
        df["choppiness"] = 50.0
        return df

    def test_early_event_vol_z_unaffected_by_future_volume_spike(self):
        """把資料尾端（events 之後）的成交量改成極端值，早期 event 的 vol_z 不該變。"""
        from ml.ml_filter import extract_features
        prepared_a = self._make_prepared(300)
        prepared_b = prepared_a.copy()
        prepared_b.iloc[250:] = prepared_b.iloc[250:].assign(
            volume=prepared_b["volume"].iloc[250:] * 1000)  # 尾端爆量
        early_event = prepared_a.index[[20]]                # 遠在爆量之前
        feats_a = extract_features(prepared_a, early_event)
        feats_b = extract_features(prepared_b, early_event)
        assert feats_a["vol_z"].iloc[0] == pytest.approx(feats_b["vol_z"].iloc[0], abs=1e-6), (
            "早期事件的 vol_z 不該被「未來才發生」的爆量影響——現在會變，證明有 look-ahead 洩漏"
        )

    def test_vol_z_still_computed_when_no_lookahead_possible(self):
        """功能面：vol_z 依然能正常算出數值（不是整欄變 0 或 NaN）。"""
        from ml.ml_filter import extract_features
        prepared = self._make_prepared(300)
        events = prepared.index[100:120]
        feats = extract_features(prepared, events)
        assert feats["vol_z"].notna().all()
        assert feats["vol_z"].std() > 0   # 不是全部塌縮成同一個值


class TestScalePosWeight:
    """train_filter 對稀少的 +1（贏單）類別加權，避免模型學到「永遠猜 0/-1」的偷懶解。"""

    def _make_prepared(self, n=300, seed=1):
        df = make_ohlcv(n, seed)
        df["atr"] = df["close"] * 0.01
        df["adx"] = 20.0
        df["rsi"] = 50.0
        df["er"] = 0.5
        df["choppiness"] = 50.0
        return df

    def test_scale_pos_weight_matches_imbalance_ratio(self):
        from ml.ml_filter import extract_features, train_filter
        prepared = self._make_prepared(300)
        events = prepared.index[::5][:40]
        X = extract_features(prepared, events)
        # 高度不平衡：只有 4 筆 +1，其餘 36 筆非 +1
        y = pd.Series([1]*4 + [0]*36, index=events)
        model = train_filter(X, y)
        expected_ratio = 36 / 4
        assert model.get_params()["scale_pos_weight"] == pytest.approx(expected_ratio)

    def test_scale_pos_weight_defaults_to_one_when_no_positives(self):
        """全部都不是 +1（沒有正樣本）→ 退回 1.0，不除以零。"""
        from ml.ml_filter import extract_features, train_filter
        prepared = self._make_prepared(300)
        events = prepared.index[::5][:20]
        X = extract_features(prepared, events)
        y = pd.Series([0]*20, index=events)
        model = train_filter(X, y)
        assert model.get_params()["scale_pos_weight"] == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════
# SMC 結構特徵（2026-07-05）：先前嚴格重測證實泛用波動度特徵無預測力（AUC 0.557），
# 根因處方是「換結構特徵」。新增三個：fvg_size_atr（FVG 缺口大小/ATR）、
# bos_dist_atr（BOS 突破距離/ATR）、bos_body_atr（突破棒實體/ATR）。
# 非 SMC 策略缺這些欄位 → NaN → fillna(median) 降級，與 fib_score 等同一慣例。
# ═══════════════════════════════════════════════════════════════════════════

class TestSmcStructuralFeatures:
    def _smc_prepared(self, n=120):
        """帶 swing/atr/bos 欄位的 prepared（模擬 smc_structure.prepare() 輸出）。"""
        rng = np.random.default_rng(3)
        closes = 100 + np.cumsum(rng.normal(0.1, 1.0, n))
        idx = pd.date_range("2024-01-01", periods=n, freq="4h")
        df = pd.DataFrame({
            "open": closes - 0.2, "high": closes + 1.0, "low": closes - 1.0,
            "close": closes, "volume": np.full(n, 100.0)}, index=idx)
        df["atr"] = 2.0
        df["adx"] = 25.0; df["rsi"] = 55.0; df["er"] = 0.5; df["choppiness"] = 45.0
        df["swing_high"] = closes - 3.0          # close 高於 swing_high 3.0 → bos_dist = 1.5×ATR
        df["swing_low"] = closes - 10.0
        return df

    def test_feature_cols_include_structural(self):
        from ml.ml_filter import FEATURE_COLS
        for col in ("fvg_size_atr", "bos_dist_atr", "bos_body_atr"):
            assert col in FEATURE_COLS, f"FEATURE_COLS 缺 {col}"

    def test_structural_values_computed_when_columns_present(self):
        from ml.ml_filter import extract_features
        prepared = self._smc_prepared()
        events = prepared.index[50:55]
        X = extract_features(prepared, events)
        # bos_dist = (close − swing_high)/atr = 3.0/2.0 = 1.5
        assert X["bos_dist_atr"].iloc[0] == pytest.approx(1.5)
        # bos_body = |close − open|/atr = 0.2/2.0 = 0.1
        assert X["bos_body_atr"].iloc[0] == pytest.approx(0.1)

    def test_fvg_size_positive_when_gap_exists(self):
        from ml.ml_filter import extract_features
        prepared = self._smc_prepared()
        # 人工造一個看漲 FVG：event 根的 low 高於 i-2 根的 high → 缺口 = low − high[i-2]
        i = 60
        prepared.iloc[i - 2, prepared.columns.get_loc("high")] = prepared["low"].iloc[i] - 4.0
        X = extract_features(prepared, prepared.index[[i]])
        assert X["fvg_size_atr"].iloc[0] == pytest.approx(4.0 / 2.0)   # 缺口 4.0 / ATR 2.0

    def test_non_smc_prepared_degrades_gracefully(self):
        """無 swing/atr 欄位的策略（如 fib_ema）→ 結構特徵 NaN→median 降級，不炸。"""
        from ml.ml_filter import extract_features
        rng = np.random.default_rng(5)
        n = 60
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        idx = pd.date_range("2024-01-01", periods=n, freq="4h")
        prepared = pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                                 "close": closes, "volume": np.full(n, 10.0)}, index=idx)
        X = extract_features(prepared, prepared.index[10:15])
        assert len(X) == 5                        # 不拋例外、行數正確


# ═══════════════════════════════════════════════════════════════════════════
# 資金費率特徵（2026-07-06，使用者要求探索衍生品專屬資料——非價格衍生特徵，
# 跟先前證實無預測力的技術指標特徵（AUC 0.557）是不同的資訊來源家族）。
# ═══════════════════════════════════════════════════════════════════════════

class TestFundingFeatures:
    def _funding_series(self, n=40, start="2024-01-01"):
        """模擬幣安 fundingRate 歷史：每 8 小時一筆，index=fundingTime。"""
        idx = pd.date_range(start, periods=n, freq="8h")
        rng = np.random.default_rng(1)
        return pd.Series(rng.normal(0.0001, 0.0003, n), index=idx, name="fundingRate")

    def test_returns_expected_columns(self):
        from ml.ml_filter import funding_features
        funding = self._funding_series()
        events = pd.DatetimeIndex(["2024-01-03 04:00", "2024-01-05 12:00"])
        out = funding_features(events, funding)
        for col in ("funding_rate", "funding_rate_ma", "funding_rate_z"):
            assert col in out.columns

    def test_uses_only_settlement_at_or_before_event_causal(self):
        """事件時間點的特徵只能反映「已經結算」的費率，不能偷看之後才結算的值。"""
        from ml.ml_filter import funding_features
        idx = pd.to_datetime(["2024-01-01 00:00", "2024-01-01 08:00", "2024-01-01 16:00"])
        funding = pd.Series([0.0001, 0.0001, 0.0001], index=idx)   # 前兩筆平穩
        # 事件夾在第2筆(08:00)結算後、第3筆(16:00)結算前
        event = pd.DatetimeIndex(["2024-01-01 12:00"])
        out_before = funding_features(event, funding)
        # 把「事件之後才結算」的第3筆改成極端值，事件當下的特徵不該變
        funding2 = funding.copy()
        funding2.iloc[2] = 5.0                       # 之後才結算的暴衝
        out_after = funding_features(event, funding2)
        assert out_before["funding_rate"].iloc[0] == out_after["funding_rate"].iloc[0]
        assert out_before["funding_rate"].iloc[0] == pytest.approx(0.0001)

    def test_event_before_first_settlement_is_nan(self):
        from ml.ml_filter import funding_features
        funding = self._funding_series(start="2024-06-01")
        event = pd.DatetimeIndex(["2024-01-01 00:00"])   # 早於資金費率歷史起點
        out = funding_features(event, funding)
        assert out["funding_rate"].isna().all()

    def test_funding_rate_z_reflects_extremity(self):
        """費率明顯偏離近期均值時，z 分數應有明顯量級（正負號跟偏離方向一致）。"""
        from ml.ml_filter import funding_features
        idx = pd.date_range("2024-01-01", periods=30, freq="8h")
        rates = np.full(30, 0.0001)
        rates[-1] = 0.01           # 最後一筆是異常正極值
        funding = pd.Series(rates, index=idx)
        event = pd.DatetimeIndex([idx[-1] + pd.Timedelta(hours=1)])
        out = funding_features(event, funding, z_window=30)
        assert out["funding_rate_z"].iloc[0] > 2.0   # 明顯正偏離


class TestExtractFeaturesWithFunding:
    def test_extract_features_accepts_optional_funding(self):
        """extract_features 加 funding 選填參數：有給時特徵欄位包含資金費率衍生特徵。"""
        from ml.ml_filter import extract_features, FEATURE_COLS
        rng = np.random.default_rng(2)
        n = 60
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        idx = pd.date_range("2024-01-01", periods=n, freq="4h")
        prepared = pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                                 "close": closes, "volume": np.full(n, 10.0)}, index=idx)
        funding_idx = pd.date_range("2023-12-01", periods=200, freq="8h")
        funding = pd.Series(rng.normal(0.0001, 0.0002, 200), index=funding_idx)
        events = prepared.index[10:15]
        X = extract_features(prepared, events, funding=funding)
        for col in ("funding_rate", "funding_rate_ma", "funding_rate_z"):
            assert col in X.columns

    def test_extract_features_without_funding_backward_compatible(self):
        """不傳 funding（現有全部呼叫點）→ 行為逐位元不變，沒有資金費率欄位。"""
        from ml.ml_filter import extract_features
        rng = np.random.default_rng(2)
        n = 60
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        idx = pd.date_range("2024-01-01", periods=n, freq="4h")
        prepared = pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                                 "close": closes, "volume": np.full(n, 10.0)}, index=idx)
        X = extract_features(prepared, prepared.index[10:15])
        assert "funding_rate" not in X.columns
