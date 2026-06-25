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
