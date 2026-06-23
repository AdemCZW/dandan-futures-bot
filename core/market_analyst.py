"""市場分析師 /market-analyst — 即時行情、歷史 K 線、異常檢測。

只負責「拿資料」，不做任何交易決策。
"""
import pandas as pd
from binance.client import Client


def make_client(api_key: str = "", api_secret: str = "", testnet: bool = True) -> Client:
    """建立幣安 client。testnet=True 會自動指向 testnet.binance.vision。

    拿公開行情（K 線、價格）其實不需要金鑰，但下單需要。
    """
    return Client(api_key, api_secret, testnet=testnet)


def fetch_klines(client: Client, symbol: str, interval: str, limit: int = 500,
                 futures: bool = False) -> pd.DataFrame:
    """抓最近 N 根 K 線，回傳乾淨的 DataFrame（欄位皆為 float）。

    futures=True 改抓【合約】K 線（futures_klines），供 run_live_futures 使用；
    欄位格式與現貨相同。
    """
    raw = (client.futures_klines(symbol=symbol, interval=interval, limit=limit)
           if futures else client.get_klines(symbol=symbol, interval=interval, limit=limit))
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("open_time")
    return df[["open", "high", "low", "close", "volume"]]


def detect_anomaly(df: pd.DataFrame, lookback: int = 50, z_threshold: float = 4.0) -> bool:
    """簡易異常偵測：最新一根成交量是否相對近期均值嚴重爆量。

    回傳 True 代表偵測到異常（暴量），上層可選擇暫停下單避免被插針。
    """
    if len(df) < lookback:
        return False
    vol = df["volume"].tail(lookback)
    mean, std = vol.iloc[:-1].mean(), vol.iloc[:-1].std()
    if std == 0:
        return False
    z = (vol.iloc[-1] - mean) / std
    return bool(z > z_threshold)


def fetch_historical_klines(client: Client, symbol: str, interval: str,
                            start_str: str, end_str: str | None = None) -> pd.DataFrame:
    """抓「長歷史」K 線。python-binance 會自動分批翻頁，突破單次 1000 根上限。

    start_str 接受 python-binance 格式，例如 "6 months ago UTC"、"2025-06-01"。
    walk-forward 一定要用這個（資料越長越能驗證泛化）。
    """
    raw = client.get_historical_klines(symbol, interval, start_str, end_str)
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.set_index("open_time")[["open", "high", "low", "close", "volume"]]


def save_klines(df: pd.DataFrame, path: str) -> None:
    """快取到 CSV，避免最佳化時反覆打 API。"""
    df.to_csv(path)


def load_klines(path: str) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)
