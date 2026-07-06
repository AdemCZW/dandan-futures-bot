"""資金費率特徵嚴格 ML 評估（2026-07-06）。

使用者要求探索新的 AI 學習方向：既有 ML 過濾層特徵全是價格/技術指標衍生
（嚴格重測 AUC 0.557，等於瞎猜），資金費率是完全不同的資訊來源（做多/
做空擁擠度），這支腳本驗證加入資金費率特徵後 AUC 有沒有實質提升。

方法（跟既有 ML 過濾層評估同一套嚴謹標準）：
  1. 對 8 幣的 smc_structure BOS 訊號做 Triple Barrier 標記（跟 run_train_filter.py
     同方法：atr_mult=2.0，vb=24 根）
  2. 分別用「baseline 特徵」與「baseline + 資金費率特徵」訓練
  3. 8 幣合併訓練（時間軸交錯）用 StratifiedKFold 5-fold 評 AUC（不能用
     PurgedKFold——那是給單一標的用的，多標的合併時間軸本來就交錯）
  4. 比較兩組 AUC，判斷資金費率是否有增量預測力

資料來源：
  - K 線：research/klines_cache/*_4h_1095.csv（3年，已有）
  - 資金費率：research/funding_cache/*.csv（本腳本抓取，幣安 fundingRate
    歷史 API 免金鑰、無時間窗限制——跟 open interest 歷史（僅約30天）不同，
    這是本次唯一可行的衍生品歷史資料）
"""
import sys, os, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score

from core.quant_researcher import build_strategy
from backtest.vbt_optimize import signals_from_prepared
from ml.triple_barrier import label_triple_barrier
from ml.ml_filter import extract_features, train_filter, FEATURE_COLS

CORE8 = ["SUIUSDT", "BTCUSDT", "ETHUSDT", "ARBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT"]
KLINE_CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
FUNDING_CACHE = os.path.join(os.path.dirname(__file__), "..", "funding_cache")
FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
os.makedirs(FUNDING_CACHE, exist_ok=True)


def load_klines(symbol, interval="4h", days=1095):
    return pd.read_csv(f"{KLINE_CACHE}/{symbol}_{interval}_{days}.csv", index_col=0, parse_dates=True)


def fetch_funding(symbol, days=1095):
    """抓資金費率歷史（免金鑰，每 8 小時一筆，無 open-interest-hist 那種時間窗限制）。"""
    cache = os.path.join(FUNDING_CACHE, f"{symbol}_funding_{days}.csv")
    if os.path.exists(cache):
        s = pd.read_csv(cache, index_col=0, parse_dates=True)["fundingRate"]
        return s
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    rows, cur = [], start_ms
    while cur < end_ms:
        url = f"{FAPI_FUNDING}?symbol={symbol}&startTime={cur}&endTime={end_ms}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  [warn] {symbol} funding: {e}"); break
        if not batch: break
        rows.extend(batch)
        cur = int(batch[-1]["fundingTime"]) + 1
        time.sleep(0.05)
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = df.drop_duplicates("fundingTime").set_index("fundingTime")
    df[["fundingRate"]].to_csv(cache)
    return df["fundingRate"]


def process_symbol(symbol, funding):
    df = load_klines(symbol)
    strat = build_strategy("smc_structure")
    prepared = strat.prepare(df).dropna()
    if len(prepared) < 100:
        return None
    le, _, se, _ = signals_from_prepared(prepared, "smc_structure")
    entries = le | se
    events = prepared.index[entries]
    if len(events) < 20:
        return None

    atr_series = prepared.get("atr")
    labels = label_triple_barrier(prepared["close"], events, pt=0.02, sl=0.02, vb=24,
                                  atr=atr_series, atr_mult=2.0)
    X_base = extract_features(prepared, events)
    X_fund = extract_features(prepared, events, funding=funding)
    return X_base, X_fund, labels


if __name__ == "__main__":
    print("=== 抓資金費率歷史（3年，8幣）===")
    funding_by_symbol = {}
    for s in CORE8:
        fr = fetch_funding(s)
        if not fr.empty:
            print(f"  {s:10s} {len(fr)} 筆結算，{fr.index[0]} ~ {fr.index[-1]}")
            funding_by_symbol[s] = fr

    all_base, all_fund, all_y = [], [], []
    print("\n=== 逐幣處理（smc_structure BOS 訊號 + Triple Barrier 標記）===")
    for s in CORE8:
        if s not in funding_by_symbol:
            continue
        result = process_symbol(s, funding_by_symbol[s])
        if result is None:
            print(f"  [跳過] {s} 訊號不足")
            continue
        X_base, X_fund, y = result
        print(f"  {s:10s} {len(y)} 筆訊號  +1={( y==1).sum()}  -1={(y==-1).sum()}  0={(y==0).sum()}")
        all_base.append(X_base)
        all_fund.append(X_fund)
        all_y.append(y)

    X_base_full = pd.concat(all_base, ignore_index=True)
    X_fund_full = pd.concat(all_fund, ignore_index=True)
    y_full = pd.concat(all_y, ignore_index=True)
    y_bin = (y_full == 1).astype(int)
    print(f"\n合計 {len(y_full)} 筆樣本  正樣本(+1)佔比 {y_bin.mean():.1%}")

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    print("\n=== AUC 比較（5-fold StratifiedKFold，多幣合併）===")
    for name, X in [("baseline（技術指標特徵，既有）", X_base_full),
                    ("baseline + 資金費率特徵（新）", X_fund_full)]:
        model = train_filter(X, y_full)
        aucs = []
        for train_idx, test_idx in cv.split(X.values, y_bin.values):
            m = train_filter(X.iloc[train_idx], y_full.iloc[train_idx])
            proba = m.predict_proba(X.iloc[test_idx].values)[:, 1]
            aucs.append(roc_auc_score(y_bin.iloc[test_idx], proba))
        print(f"  {name:<32} AUC = {np.mean(aucs):.4f} ± {np.std(aucs):.4f}  "
              f"（0.5=瞎猜，>0.55才算有意義的預測力）")

    print("\n=== 資金費率特徵重要度（用全樣本訓練的模型）===")
    model_fund = train_filter(X_fund_full, y_full)
    imp = dict(zip(X_fund_full.columns, model_fund.feature_importances_))
    for k, v in sorted(imp.items(), key=lambda x: -x[1])[:10]:
        marker = " ← 資金費率" if "funding" in k else ""
        print(f"  {k:<18} {v:.4f}{marker}")

    # ── 時間切分驗證（不shuffle）：這才是真正的樣本外檢驗 ──────────────────
    # shuffled StratifiedKFold 對「長記憶」特徵（funding_rate_ma/z 用90次結算
    # ≈30天滾動窗）會洩漏：訓練/測試樣本在時間上可能緊鄰，滾動窗高度自相關，
    # 造成「看似有預測力」但其實是同一段市場體制被切到兩邊的假象。
    print("\n" + "=" * 70)
    print("時間切分驗證（每幣前70%訓練、後30%測試，真正樣本外，不shuffle）")
    print("=" * 70)
    base_train, base_test, fund_train, fund_test, y_train_l, y_test_l = [], [], [], [], [], []
    for s in CORE8:
        if s not in funding_by_symbol:
            continue
        result = process_symbol(s, funding_by_symbol[s])
        if result is None:
            continue
        Xb, Xf, y = result
        cut = int(len(y) * 0.7)
        base_train.append(Xb.iloc[:cut]); base_test.append(Xb.iloc[cut:])
        fund_train.append(Xf.iloc[:cut]); fund_test.append(Xf.iloc[cut:])
        y_train_l.append(y.iloc[:cut]); y_test_l.append(y.iloc[cut:])

    Xb_tr, Xb_te = pd.concat(base_train, ignore_index=True), pd.concat(base_test, ignore_index=True)
    Xf_tr, Xf_te = pd.concat(fund_train, ignore_index=True), pd.concat(fund_test, ignore_index=True)
    y_tr, y_te = pd.concat(y_train_l, ignore_index=True), pd.concat(y_test_l, ignore_index=True)
    y_tr_bin, y_te_bin = (y_tr == 1).astype(int), (y_te == 1).astype(int)
    for name, Xtr, Xte in [("baseline", Xb_tr, Xb_te), ("baseline+資金費率", Xf_tr, Xf_te)]:
        m = train_filter(Xtr, y_tr)
        auc = roc_auc_score(y_te_bin, m.predict_proba(Xte.values)[:, 1])
        print(f"  {name:<20} AUC(樣本外，時間切分) = {auc:.4f}")
