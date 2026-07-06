"""橫斷面排名特徵嚴格 ML 評估（2026-07-07）。

使用者選定的新 AI 方向：既有 ML 過濾層特徵全是「單幣看自己」（技術指標 AUC 0.557、
資金費率時間切分 0.4711、SMC 結構 0.551，三次獨立確認等於瞎猜）。橫斷面排名問的是
完全不同的維度——「這個幣此刻在 8 幣籃子裡是領漲還是落後」，對齊唯一被證實的 edge
（橫斷面動量）。本腳本驗證加入橫斷面特徵後 AUC 有沒有實質提升。

方法（跟資金費率評估同一套嚴謹標準）：
  1. 對 8 幣的 smc_structure BOS 訊號做 Triple Barrier 標記（atr_mult=2.0, vb=24）
  2. 分別用「baseline 特徵」與「baseline + 橫斷面特徵」訓練
  3. shuffled 5-fold AUC（會被長記憶特徵騙，僅供參考）
  4. **時間切分 70/30 AUC（真正樣本外，這才是決定性關卡——資金費率就是在這裡翻車）**
  5. 特徵重要度

資料：research/klines_cache/*_4h_1095.csv（3年，已有；橫斷面特徵零額外資料成本）。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import roc_auc_score

from core.quant_researcher import build_strategy
from backtest.vbt_optimize import signals_from_prepared
from ml.triple_barrier import label_triple_barrier
from ml.ml_filter import extract_features, cross_sectional_features, train_filter

CORE8 = ["SUIUSDT", "BTCUSDT", "ETHUSDT", "ARBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT"]
KLINE_CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")


def load_klines(symbol, interval="4h", days=1095):
    return pd.read_csv(f"{KLINE_CACHE}/{symbol}_{interval}_{days}.csv", index_col=0, parse_dates=True)


def build_panel():
    """8 幣收盤寬表（columns=幣, index=時間；outer join，未上市期間 NaN）。"""
    closes = {}
    for s in CORE8:
        try:
            closes[s] = load_klines(s)["close"]
        except FileNotFoundError:
            print(f"  [warn] {s} 無快取，跳過")
    return pd.DataFrame(closes).sort_index()


def process_symbol(symbol, panel):
    df = load_klines(symbol)
    strat = build_strategy("smc_structure")
    prepared = strat.prepare(df).dropna()
    if len(prepared) < 100:
        return None
    le, _, se, _ = signals_from_prepared(prepared, "smc_structure")
    events = prepared.index[le | se]
    if len(events) < 20:
        return None

    atr_series = prepared.get("atr")
    labels = label_triple_barrier(prepared["close"], events, pt=0.02, sl=0.02, vb=24,
                                  atr=atr_series, atr_mult=2.0)
    X_base = extract_features(prepared, events)
    cs = cross_sectional_features(panel, symbol, events)
    cs = cs.fillna(cs.median())
    cs.index = X_base.index                      # 對齊位置索引（extract_features 用 events 當 index）
    X_cs = pd.concat([X_base.reset_index(drop=True), cs.reset_index(drop=True)], axis=1)
    return X_base.reset_index(drop=True), X_cs, labels.reset_index(drop=True)


def auc_shuffled(X, y_bin, y_full, seed=42):
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    aucs = []
    for tr, te in cv.split(X.values, y_bin.values):
        m = train_filter(X.iloc[tr], y_full.iloc[tr])
        aucs.append(roc_auc_score(y_bin.iloc[te], m.predict_proba(X.iloc[te].values)[:, 1]))
    return np.mean(aucs), np.std(aucs)


if __name__ == "__main__":
    print("=== 建立 8 幣收盤 panel ===")
    panel = build_panel()
    print(f"  panel: {panel.shape[0]} 根 × {panel.shape[1]} 幣  {panel.index[0]} ~ {panel.index[-1]}")

    all_base, all_cs, all_y = [], [], []
    print("\n=== 逐幣處理（smc_structure BOS 訊號 + Triple Barrier）===")
    for s in CORE8:
        r = process_symbol(s, panel)
        if r is None:
            print(f"  [跳過] {s} 訊號不足")
            continue
        Xb, Xc, y = r
        print(f"  {s:10s} {len(y)} 筆訊號  +1={(y==1).sum()}  -1={(y==-1).sum()}  0={(y==0).sum()}")
        all_base.append(Xb); all_cs.append(Xc); all_y.append(y)

    Xb_full = pd.concat(all_base, ignore_index=True)
    Xc_full = pd.concat(all_cs, ignore_index=True)
    y_full = pd.concat(all_y, ignore_index=True)
    y_bin = (y_full == 1).astype(int)
    print(f"\n合計 {len(y_full)} 筆樣本  正樣本(+1)佔比 {y_bin.mean():.1%}")

    print("\n=== shuffled 5-fold AUC（僅供參考，長記憶特徵會被騙）===")
    for name, X in [("baseline（單幣技術指標，既有）", Xb_full),
                    ("baseline + 橫斷面排名（新）", Xc_full)]:
        mu, sd = auc_shuffled(X, y_bin, y_full)
        print(f"  {name:<30} AUC = {mu:.4f} ± {sd:.4f}")

    print("\n=== 橫斷面特徵重要度（全樣本模型）===")
    m_cs = train_filter(Xc_full, y_full)
    for k, v in sorted(zip(Xc_full.columns, m_cs.feature_importances_), key=lambda x: -x[1])[:12]:
        marker = " ← 橫斷面" if k.startswith("cs_") else ""
        print(f"  {k:<18} {v:.4f}{marker}")

    # ── 時間切分 70/30（決定性關卡）─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("時間切分驗證（每幣前70%訓練、後30%測試，真正樣本外，不shuffle）")
    print("這是決定性關卡：資金費率就是 shuffled 看似有效、時間切分後翻車")
    print("=" * 70)
    b_tr, b_te, c_tr, c_te, y_tr_l, y_te_l = [], [], [], [], [], []
    for s in CORE8:
        r = process_symbol(s, panel)
        if r is None:
            continue
        Xb, Xc, y = r
        cut = int(len(y) * 0.7)
        b_tr.append(Xb.iloc[:cut]); b_te.append(Xb.iloc[cut:])
        c_tr.append(Xc.iloc[:cut]); c_te.append(Xc.iloc[cut:])
        y_tr_l.append(y.iloc[:cut]); y_te_l.append(y.iloc[cut:])

    Xb_tr, Xb_te = pd.concat(b_tr, ignore_index=True), pd.concat(b_te, ignore_index=True)
    Xc_tr, Xc_te = pd.concat(c_tr, ignore_index=True), pd.concat(c_te, ignore_index=True)
    y_tr, y_te = pd.concat(y_tr_l, ignore_index=True), pd.concat(y_te_l, ignore_index=True)
    y_te_bin = (y_te == 1).astype(int)
    print()
    for name, Xtr, Xte in [("baseline", Xb_tr, Xb_te), ("baseline+橫斷面", Xc_tr, Xc_te)]:
        m = train_filter(Xtr, y_tr)
        auc = roc_auc_score(y_te_bin, m.predict_proba(Xte.values)[:, 1])
        verdict = "✅ 有增量預測力" if auc > 0.55 else ("≈瞎猜" if auc >= 0.5 else "❌ 比瞎猜還差")
        print(f"  {name:<16} AUC(樣本外，時間切分) = {auc:.4f}  {verdict}")
    print("\n判定：橫斷面版時間切分 AUC 若未實質 >0.55 且 > baseline，則跟前三種特徵一樣無 edge。")
