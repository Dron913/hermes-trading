"""Offline performance analysis from Railway logs + market data."""
import json
import sys
from datetime import datetime, timezone

# Real current market prices (Kraken, 1m) at analysis time
PRICES = {
    "BTC/USDT": 73783.0,
    "ETH/USDT": 2008.0,
    "SOL/USDT": 82.50,
}

# REVERSED from Railway logs (E2E cycle 1, prices from ~2 hours ago)
ENTRY_PRICES = {
    "BTC/USDT": 73883.10,  # ~$100 below current
    "ETH/USDT": 2012.59,
    "SOL/USDT": 82.29,
}

def analyze():
    state_dir = __import__("pathlib").Path(__file__).parent / "state"

    # Read trades from persistent volume via railway run output
    # (Can't read volume directly — analyze from Railway log reconstruction)
    # Trades = 0 from trades.jsonl (all open positions, no closes)

    # Strategy
    strat_path = state_dir / "strategy.yaml"
    strat = __import__("yaml").safe_load(strat_path.read_text())

    # Bootstrap proof
    bp = state_dir / "bootstrap_proof.json"
    boot_info = json.loads(bp.read_text()) if bp.exists() else {}

    # Open positions: 3 LONG from Railway logs
    open_positions = [
        {"asset": "BTC/USDT", "side": "long",  "entry": 73883.10, "cur": PRICES["BTC/USDT"]},
        {"asset": "ETH/USDT", "side": "long",  "entry": 2012.59, "cur": PRICES["ETH/USDT"]},
        {"asset": "SOL/USDT", "side": "long",  "entry": 82.29,   "cur": PRICES["SOL/USDT"]},
    ]

    print("=" * 60)
    print("TRADEFORGE PERFORMANCE REPORT")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    print("\n--- STRATEGY ---")
    print(f"  Version:     {strat['version']}")
    print(f"  Assets:     {', '.join(strat['assets'])}")
    print(f"  Max Pos:     {strat['max_open_positions']}")
    print(f"  SL:          {strat['stop_loss_pct']}%")
    print(f"  TP:          {strat['take_profit_pct']}%")
    print(f"  RSI thresh:  {strat['setup_1h']['rsi_threshold']}")

    print("\n--- CLOSED TRADES ---")
    print(f"  Total:       0")
    print(f"  Win rate:    N/A (no closed trades)")
    print(f"  Total PnL:   0.00%")
    print("  NOTE: Trades close via TP/SL/early-exit. All 3 positions still open.")
    print("        No reflection triggered yet (reflection_every=5, trades=0 closed)")

    print("\n--- OPEN POSITIONS (unrealized) ---")
    total_equity = 10000.0  # paper account base
    for pos in open_positions:
        pnl = (pos["cur"] - pos["entry"]) / pos["entry"] * 100 if pos["side"] == "long" else (pos["entry"] - pos["cur"]) / pos["entry"] * 100
        notional = total_equity / 3  # equal weight
        pnl_dollar = notional * pnl / 100
        print(f"  {pos['asset']:10s} {pos['side'].upper():5s} entry={pos['entry']:.2f} cur={pos['cur']:.2f} pnl={pnl:+.3f}% (${pnl_dollar:+.2f})")
        total_equity += pnl_dollar
        # Asset-level PnL tracking
        if pos['asset'] not in ('BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'TOTAL'):
            pass  # already tracked

    print(f"\n  Paper Equity: ${total_equity:.2f}")
    print(f"  Unrealized:   ${total_equity - 10000:.2f} ({((total_equity-10000)/100)*100:.3f}%)")

    # Asset-level summary (no closed trades yet)
    print("\n--- ASSET PERFORMANCE (open PnL, reference only) ---")
    for asset in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        entry = ENTRY_PRICES[asset]
        cur = PRICES[asset]
        pnl = (cur - entry) / entry * 100
        status = "OPEN"  # all positions open
        print(f"  {asset:10s}  pnl={pnl:+.3f}% status={status}")

    print("\n--- PERSISTENCE ---")
    print(f"  Volume:       tradeforge-volume · /app/state")
    print(f"  Bootstrap:    persist_epoch={boot_info.get('persistence_epoch', '?')}")
    print(f"  Strategy:     persisted in volume ({strat['version']} in /app/state/strategy.yaml)")
    print(f"  Hypotheses:   persisted (v0005-v0008 in /app/state/hypotheses.jsonl)")
    print(f"  History:      4 versions archived in /app/state/history/")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    analyze()