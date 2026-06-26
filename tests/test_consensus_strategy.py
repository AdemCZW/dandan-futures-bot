"""TDD tests for ConsensusStrategy.

RED → GREEN order. All tests written before implementation.
"""
import numpy as np
import pandas as pd
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.quant_researcher import build_strategy


def make_df(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.012, n)
    close = 30_000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    idx  = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open":   np.r_[close[0], close[:-1]],
        "high":   high, "low": low, "close": close,
        "volume": rng.lognormal(3, 0.5, n),
    }, index=idx)


class TestConsensusStrategy:

    def test_build_strategy_returns_consensus_instance(self):
        from core.quant_researcher import ConsensusStrategy
        strat = build_strategy("consensus",
                               strategies=["rsi2_connors", "fib_channel"],
                               min_agree=2)
        assert isinstance(strat, ConsensusStrategy)

    def test_prepare_returns_dataframe_with_all_sub_strategy_columns(self):
        strat = build_strategy("consensus",
                               strategies=["rsi2_connors", "fib_channel"],
                               min_agree=2)
        df = make_df()
        prepared = strat.prepare(df)
        assert isinstance(prepared, pd.DataFrame)
        assert len(prepared) == len(df)
        # must include columns from both sub-strategies
        assert "rsi2" in prepared.columns        # rsi2_connors
        assert "fib_ch_pos" in prepared.columns  # fib_channel

    def test_signal_returns_int(self):
        strat = build_strategy("consensus",
                               strategies=["rsi2_connors", "fib_channel"],
                               min_agree=2)
        df = make_df()
        prepared = strat.prepare(df).dropna()
        row = prepared.iloc[-1]
        result = strat.signal(row, 0)
        assert isinstance(result, int)
        assert result in (-1, 0, 1)

    def test_signal_zero_when_sub_strategies_disagree(self):
        """If sub-strategies return different signals, consensus returns 0."""
        from core.quant_researcher import ConsensusStrategy

        class AlwaysLong:
            name = "always_long"
            def signal(self, row, pos): return 1
            def prepare(self, df): return df.copy()

        class AlwaysShort:
            name = "always_short"
            def signal(self, row, pos): return -1
            def prepare(self, df): return df.copy()

        strat = ConsensusStrategy([AlwaysLong(), AlwaysShort()], min_agree=2)
        df = make_df(50)
        prepared = strat.prepare(df)
        row = prepared.iloc[-1]
        assert strat.signal(row, 0) == 0

    def test_signal_long_when_majority_vote_long(self):
        """2 out of 3 vote long → signal 1."""
        from core.quant_researcher import ConsensusStrategy

        class AlwaysLong:
            name = "long"
            def signal(self, row, pos): return 1
            def prepare(self, df): return df.copy()

        class AlwaysFlat:
            name = "flat"
            def signal(self, row, pos): return 0
            def prepare(self, df): return df.copy()

        strat = ConsensusStrategy([AlwaysLong(), AlwaysLong(), AlwaysFlat()], min_agree=2)
        df = make_df(50)
        prepared = strat.prepare(df)
        row = prepared.iloc[-1]
        assert strat.signal(row, 0) == 1

    def test_signal_short_when_majority_vote_short(self):
        from core.quant_researcher import ConsensusStrategy

        class AlwaysShort:
            name = "short"
            def signal(self, row, pos): return -1
            def prepare(self, df): return df.copy()

        strat = ConsensusStrategy([AlwaysShort(), AlwaysShort()], min_agree=2)
        df = make_df(50)
        prepared = strat.prepare(df)
        row = prepared.iloc[-1]
        assert strat.signal(row, 0) == -1

    def test_three_sub_strategies_from_build(self):
        strat = build_strategy("consensus",
                               strategies=["rsi2_connors", "fib_channel", "smc_structure"],
                               min_agree=2)
        df = make_df(500)
        prepared = strat.prepare(df).dropna()
        assert len(prepared) > 50
        row = prepared.iloc[-1]
        result = strat.signal(row, 0)
        assert result in (-1, 0, 1)

    def test_min_agree_1_is_same_as_any_signal(self):
        """With min_agree=1, any single strategy agreement is enough."""
        from core.quant_researcher import ConsensusStrategy

        class AlwaysLong:
            name = "long"
            def signal(self, row, pos): return 1
            def prepare(self, df): return df.copy()

        class AlwaysFlat:
            name = "flat"
            def signal(self, row, pos): return 0
            def prepare(self, df): return df.copy()

        strat = ConsensusStrategy([AlwaysLong(), AlwaysFlat()], min_agree=1)
        df = make_df(50)
        prepared = strat.prepare(df)
        row = prepared.iloc[-1]
        assert strat.signal(row, 0) == 1


class TestConsensusRangeAdaptive:
    """min_agree_range：橫盤時降低共識門檻，讓橫盤子策略（rsi2_connors）能單獨達標。

    問題背景：CONSENSUS bot 完全不交易，因為 3 個子策略中只有 rsi2_connors
    regime_pref="range"，其他兩個趨勢策略在橫盤時回傳 0，永遠不足 min_agree=2。
    """

    def _make_strats_and_row(self):
        """回傳 (AlwaysLong, AlwaysFlat) 類別，以及有 regime 欄位的 row dict。"""
        from core.quant_researcher import ConsensusStrategy

        class AlwaysLong:
            name = "long"
            def signal(self, row, pos): return 1
            def prepare(self, df): return df.copy()

        class AlwaysFlat:
            name = "flat"
            def signal(self, row, pos): return 0
            def prepare(self, df): return df.copy()

        return AlwaysLong, AlwaysFlat, ConsensusStrategy

    def test_range_regime_uses_min_agree_range_threshold(self):
        """regime='range' 時用 min_agree_range=1，只需 1 票就能開倉。"""
        AlwaysLong, AlwaysFlat, ConsensusStrategy = self._make_strats_and_row()
        strat = ConsensusStrategy([AlwaysLong(), AlwaysFlat()],
                                   min_agree=2, min_agree_range=1)
        row = {"regime": "range"}
        assert strat.signal(row, 0) == 1, \
            "橫盤 1 票多、門檻=1 → 應進多"

    def test_trend_regime_still_uses_min_agree(self):
        """regime='trend' 時仍用 min_agree=2，1 票不夠。"""
        AlwaysLong, AlwaysFlat, ConsensusStrategy = self._make_strats_and_row()
        strat = ConsensusStrategy([AlwaysLong(), AlwaysFlat()],
                                   min_agree=2, min_agree_range=1)
        row = {"regime": "trend"}
        assert strat.signal(row, 0) == 0, \
            "趨勢盤 1 票多、門檻=2 → 不進場"

    def test_default_min_agree_range_equals_min_agree(self):
        """min_agree_range 未指定時預設等於 min_agree（向後相容）。"""
        AlwaysLong, AlwaysFlat, ConsensusStrategy = self._make_strats_and_row()
        strat = ConsensusStrategy([AlwaysLong(), AlwaysFlat()], min_agree=2)
        row = {"regime": "range"}
        assert strat.signal(row, 0) == 0, \
            "預設 min_agree_range=min_agree=2，1 票不夠"

    def test_zero_votes_in_range_no_trade(self):
        """橫盤無任何投票 → 不開倉。"""
        AlwaysLong, AlwaysFlat, ConsensusStrategy = self._make_strats_and_row()
        strat = ConsensusStrategy([AlwaysFlat(), AlwaysFlat()],
                                   min_agree=2, min_agree_range=1)
        row = {"regime": "range"}
        assert strat.signal(row, 0) == 0, "0 票無論門檻多低都不開倉"

    def test_short_in_range_with_low_threshold(self):
        """橫盤空單 1 票 + min_agree_range=1 → 開空。"""
        from core.quant_researcher import ConsensusStrategy

        class AlwaysShort:
            name = "short"
            def signal(self, row, pos): return -1
            def prepare(self, df): return df.copy()

        class AlwaysFlat:
            name = "flat"
            def signal(self, row, pos): return 0
            def prepare(self, df): return df.copy()

        strat = ConsensusStrategy([AlwaysShort(), AlwaysFlat()],
                                   min_agree=2, min_agree_range=1)
        row = {"regime": "range"}
        assert strat.signal(row, 0) == -1
