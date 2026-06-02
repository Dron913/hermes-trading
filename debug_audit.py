import ccxt, time, sys
sys.path.insert(0, r'C:\Users\DELL\hermes-trading')
from hermes_trading.adapters.price import compute_ema, compute_rsi, compute_sma
from datetime import datetime, timezone, timedelta

ex = ccxt.kraken()
end = datetime.now(timezone.utc)

# Fetch without 'since' to get max candles from Kraken (no truncation)
raw4h = ex.fetch_ohlcv('BTC/USD', '4h', limit=350)
raw1h = ex.fetch_ohlcv('BTC/USD', '1h', limit=720)
raw15m = ex.fetch_ohlcv('BTC/USD', '15m', limit=720)
print(f"Candles: 4H={len(raw4h)}, 1H={len(raw1h)}, 15M={len(raw15m)}")

first_dt = datetime.fromtimestamp(raw4h[0][0]/1000, tz=timezone.utc)
last_dt = datetime.fromtimestamp(raw4h[-1][0]/1000, tz=timezone.utc)
print(f"4H range: {first_dt} -> {last_dt}")
print(f"4H span: {(last_dt - first_dt).total_seconds() / 86400:.1f} days")

quality_scores = []
trades = []

# Warmup: need 213+ 4h candles for EMA200
start_idx = max(215, len(raw4h) - 180)
print(f"Loop: indices {start_idx} to {len(raw4h)-1} ({len(raw4h)-start_idx} samples)")

for i in range(start_idx, len(raw4h)):
    ts4h = raw4h[i][0]
    dt = datetime.fromtimestamp(ts4h/1000, tz=timezone.utc)

    closes4h = [c[4] for c in raw4h[max(0, i-350):i+1]]
    h4_close = closes4h[-1]
    h4_ema50 = compute_ema(closes4h, 50)
    h4_ema200 = compute_ema(closes4h, 200)
    h4_rsi = compute_rsi(closes4h, 14)

    h1_candles = [c for c in raw1h if c[0] <= ts4h]
    h1_closes = [c[4] for c in h1_candles[-200:]]
    h1_volumes = [c[5] for c in h1_candles[-200:]]
    h1_close = h1_closes[-1] if h1_closes else 0
    h1_ema50 = compute_ema(h1_closes, 50) if len(h1_closes) >= 50 else h1_close
    h1_rsi = compute_rsi(h1_closes, 14)
    h1_vol = h1_volumes[-1] if h1_volumes else 0
    h1_vol_avg = compute_sma(h1_volumes, 20) if len(h1_volumes) >= 20 else 0

    m15_candles = [c for c in raw15m if c[0] <= ts4h]
    m15_closes = [c[4] for c in m15_candles[-150:]]
    m15_close = m15_closes[-1] if m15_closes else 0
    m15_ema50 = compute_ema(m15_closes, 50) if len(m15_closes) >= 50 else m15_close
    m15_rsi = compute_rsi(m15_closes, 14)

    trend_long = h4_ema50 > h4_ema200
    trend_short = h4_ema50 < h4_ema200
    vol_ok = h1_vol > h1_vol_avg if h1_vol_avg > 0 else False

    # Quality score
    qs = 0.0
    long_cond = trend_long and h1_rsi > 45 and h1_close > h1_ema50 and vol_ok
    short_cond = trend_short and h1_rsi < 55 and h1_close < h1_ema50 and vol_ok
    if long_cond: qs += 50
    if short_cond: qs += 50
    qs += min(h1_rsi * 0.2, 10)
    qs += min((h1_vol / h1_vol_avg if h1_vol_avg else 0) * 5, 20)
    rsi_dist = abs(50 - h1_rsi)
    qs += rsi_dist * 0.3 if 40 < h1_rsi < 60 else 0
    qs = min(qs, 100)

    quality_scores.append({
        "dt": dt, "score": qs,
        "trend": "bull" if trend_long else ("bear" if trend_short else "chop"),
        "rsi_4h": h4_rsi, "rsi_1h": h1_rsi, "rsi_15m": m15_rsi,
        "h4_ema50": h4_ema50, "h4_ema200": h4_ema200,
        "vol_ratio": h1_vol / h1_vol_avg if h1_vol_avg else 0,
        "long_cond": long_cond, "short_cond": short_cond,
    })

    # Trade simulation (keep it simple)
    if not trend_long and not trend_short:
        continue
    direction = "long" if (trend_long and h1_rsi > 45 and h1_close > h1_ema50 and vol_ok) else \
               "short" if (trend_short and h1_rsi < 55 and h1_close < h1_ema50 and vol_ok) else None
    if not direction:
        continue

    entry_price = h1_close
    tp_pct, sl_pct = 4.0, 2.0

    for j in range(i+1, min(i+200, len(raw4h))):
        f_ts = raw4h[j][0]
        f_h1 = [c for c in raw1h if ts4h < c[0] <= f_ts]
        if not f_h1:
            continue
        f_close = f_h1[-1][4]
        pnl = (f_close - entry_price) / entry_price * 100
        if direction == "short":
            pnl = -pnl

        reason = None
        if pnl >= tp_pct: reason = "TP"
        elif pnl <= -sl_pct: reason = "SL"

        if reason:
            duration = (datetime.fromtimestamp(f_ts/1000, tz=timezone.utc) - dt).total_seconds() / 3600
            trades.append({"dt": dt, "direction": direction, "pnl": pnl, "reason": reason, "duration_h": duration})
            break

print(f"\nQuality scores: {len(quality_scores)}")
if quality_scores:
    scores = [q["score"] for q in quality_scores]
    regimes = [q["trend"] for q in quality_scores]
    print(f"Score: {min(scores):.0f}–{max(scores):.0f} (avg {sum(scores)/len(scores):.1f})")
    print(f"Regime: bull={regimes.count('bull')} bear={regimes.count('bear')} chop={regimes.count('chop')}")
    print(f"Trade signals: {len(trades)}")
    print(f"\n--- FILTER BREAKDOWN (BTC/USD on Kraken) ---")
    bull_count = sum(1 for q in quality_scores if q["trend"] == "bull")
    bear_count = sum(1 for q in quality_scores if q["trend"] == "bear")
    chop_count = sum(1 for q in quality_scores if q["trend"] == "chop")
    past_trend = bull_count + bear_count
    past_rsi = sum(1 for q in quality_scores if q["trend"] != "chop" and q["rsi_1h"] > 45 and q["rsi_1h"] < 55)
    past_vol = sum(1 for q in quality_scores if q["trend"] != "chop" and q["vol_ratio"] >= 1.0)
    past_price = sum(1 for q in quality_scores if q["trend"] != "chop" and q["rsi_1h"] > 45 and q["vol_ratio"] >= 1.0)
    signal = sum(1 for q in quality_scores if q["score"] >= 50)
    print(f"  Total samples: {len(quality_scores)}")
    print(f"  In CHOP (no trend):     {chop_count} ({chop_count/len(quality_scores)*100:.0f}%)")
    print(f"  Trend OK:               {past_trend} ({past_trend/len(quality_scores)*100:.0f}%)")
    if past_trend:
        print(f"    - Trend OK + 1H RSI 45-55:     {past_rsi} ({past_rsi/past_trend*100:.0f}% of trending)")
        print(f"    - Trend OK + volume spike:    {past_vol} ({past_vol/past_trend*100:.0f}% of trending)")
        print(f"    - FULL LONG signal (score>=50): {signal}")
else:
    print("ERROR: no quality scores!")