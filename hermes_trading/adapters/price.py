"""Price adapter — OHLCV candles across multiple timeframes."""
import asyncio
from typing import Dict

import numpy as np


def compute_ema(closes: np.ndarray, period: int) -> float:
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_sma(values: np.ndarray, period: int) -> float:
    return float(np.mean(values[-period:]))


def compute_indicator_set(df) -> Dict[str, float]:
    closes = df["close"]
    volumes = df["volume"]
    ema50 = compute_ema(closes, 50)
    ema200 = compute_ema(closes, 200)
    rsi = compute_rsi(closes, 14)
    vol_avg = compute_sma(volumes, 20)
    return {
        "close": float(closes[-1]),
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "volume": float(volumes[-1]),
        "vol_avg": vol_avg,
    }


async def fetch_ohlcv_multitimeframe(exchange, symbol: str, retries: int = 3) -> Dict[str, dict]:
    """Fetch 4H, 1H, 15M candles. Returns {timeframe: {close, ema50, ema200, rsi, volume, vol_avg}}."""
    timeframes = {
        "4h":  exchange.fetch_ohlcv(symbol, "4h", limit=250),
        "1h":  exchange.fetch_ohlcv(symbol, "1h", limit=200),
        "15m": exchange.fetch_ohlcv(symbol, "15m", limit=150),
    }

    result = {}
    for tf, candles in timeframes.items():
        closes = np.array([c[4] for c in candles])
        volumes = np.array([c[5] for c in candles])
        result[tf] = compute_indicator_set({"close": closes, "volume": volumes})

    return result


async def retry_with_backoff(coro, retries: int = 3, base_delay: float = 1.0):
    for i in range(retries):
        try:
            return await coro()
        except Exception as e:
            if i == retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** i))