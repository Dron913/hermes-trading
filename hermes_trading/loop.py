"""24/7 reliability loop — pulls data, evaluates strategy, paper trades, logs."""
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt
import yaml
from rich.console import Console
from rich.table import Table

from .adapters.price import fetch_ohlcv_multitimeframe
from .adapters.onchain import fetch_onchain
from .adapters.macro import fetch_macro
from .adapters.alpaca_broker import AlpacaBroker
from .score import score_trades


# Use STATE_DIR env var so loop.py writes to the same volume as run.py reads from.
# Railway mounts its persistent volume at /app/state — use that as default.
# When STATE_DIR is set (e.g., in local dev), use that path; otherwise prefer /app/state
# over the relative-to-__file__ path so all Railway components write to the shared volume.
_env = os.getenv("STATE_DIR", "")
if _env:
    _STATE_DIR = Path(_env)
else:
    # Default to /app/state (Railway persistent volume) so loop.py writes to the same
    # location that run.py /worker/file/ reads from. Fall back to /app/hermes_trading/state
    # only if the container is running without a persistent volume mount.
    _STATE_DIR = Path("/app/state")

console = Console()

# Expose for StatusWorker._pa_path and other components that need the same root
def get_state_dir() -> Path:
    return _STATE_DIR


class StatusWriter:
    """Writes status.json each cycle so dashboard API can read open positions + paper account."""

    # Fixed constants for paper account computation
    MAX_POSITIONS = 4
    MAX_POSITION_PCT = 0.25  # max 25% per position

    def __init__(self, path: Path):
        self.path = path
        self._pa_path = path.parent / "paper_account.yaml"
        self._starting_balance = self._load_starting_balance()

    def _load_starting_balance(self) -> float:
        """Load starting balance from paper_account.yaml. Create with default $100k if missing."""
        try:
            if self._pa_path.exists():
                data = yaml.safe_load(self._pa_path.read_text())
                return float(data.get("starting_balance", 100_000.0))
            # First boot: set default starting balance
            default = {"starting_balance": 100_000.0}
            self._pa_path.write_text(yaml.dump(default))
            return 100_000.0
        except Exception:
            return 100_000.0

    def _compute_realized_pnl(self) -> float:
        """Sum realized P&L (as %) across all closed trades, convert to USD."""
        trades_path = self.path.parent / "trades.jsonl"
        if not trades_path.exists():
            return 0.0
        total_pct = 0.0
        try:
            for line in trades_path.read_text().strip().split("\n"):
                if line.strip():
                    t = json.loads(line)
                    total_pct += float(t.get("pnl_pct", 0.0))
        except Exception:
            pass
        return round(total_pct * self._starting_balance / 100, 2)

    def write(self, positions: dict[str, dict], strategy: dict,
              tf_data_map: dict | None = None) -> None:
        """positions: {asset: {entry_price, entry_time, side, indicators}}."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            expanded = []
            for asset, pos in positions.items():
                entry_price = pos["entry_price"]
                side = pos["side"]
                unrealized = 0.0
                cur_price = "?"
                if tf_data_map and asset in tf_data_map:
                    cur = tf_data_map[asset]["1h"]["close"]
                    pnl_pct = (cur - entry_price) / entry_price * 100
                    if side == "short":
                        pnl_pct = -pnl_pct
                    unrealized = round(pnl_pct, 4)
                    cur_price = str(cur)
                expanded.append({
                    "asset": asset,
                    "side": side,
                    "entry_price": entry_price,
                    "entry_time": pos.get("entry_time", ""),
                    "unrealized_pnl": unrealized,
                    "cur_price": cur_price,
                    "sl": strategy.get("stop_loss_pct", "?"),
                    "tp": strategy.get("take_profit_pct", "?"),
                })

            # Paper account metrics
            realized_pnl = self._compute_realized_pnl()
            unrealized_pnl_usd = sum(
                p.get("unrealized_pnl", 0.0) * self._starting_balance / 100
                for p in expanded
            ) if expanded else 0.0
            current_balance = round(self._starting_balance + realized_pnl, 2)
            capital_per_pos = self._starting_balance * self.MAX_POSITION_PCT
            deployed = len(expanded) * capital_per_pos
            available = round(current_balance - deployed + unrealized_pnl_usd, 2)

            status = {
                "open_positions": expanded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "paper_account": {
                    "starting_balance": self._starting_balance,
                    "current_balance": current_balance,
                    "realized_pnl_usd": realized_pnl,
                    "unrealized_pnl_usd": round(unrealized_pnl_usd, 2),
                    "available_capital": available,
                    "deployed_capital": deployed,
                    "capital_utilization_pct": round(deployed / self._starting_balance * 100, 1),
                },
            }
            self.path.write_text(json.dumps(status, indent=2))
        except Exception:
            pass  # Non-critical


class TradingLoop:
    """Async loop: every 60s evaluate strategy, fire paper trades."""

    def __init__(self, asset: str, goal: dict):
        self.asset = asset
        self.goal = goal
        self.assets = goal.get("assets", [asset])
        sd = _STATE_DIR  # use env-aligned state directory
        self.strategy_path = sd / "strategy.yaml"
        self.trades_path = sd / "trades.jsonl"
        self.heartbeat_path = sd / "heartbeat.json"
        self.hypotheses_path = sd / "hypotheses.jsonl"
        self.status_path = sd / "status.json"
        self.consecutive_failures = 0
        self.max_failures = 5
        # Positions dict: {signal_asset: {entry_price, entry_time, side, indicators}}
        self._positions: dict[str, dict] = {}
        # Alpaca broker — initialized if credentials are set in env
        self._broker: Optional[AlpacaBroker] = None
        _key = os.getenv("ALPACA_API_KEY")
        _secret = os.getenv("ALPACA_API_SECRET")
        if _key and _secret:
            self._broker = AlpacaBroker(_key, _secret)
            console.print(f"[bold cyan]Alpaca broker initialized (paper trading)[/bold cyan]")
            # Cancel any stale pending orders from previous runs so they don't
            # consume buying power and cause wash-trade rejections
            try:
                cancelled = self._broker._client.delete("/v2/orders")
                if cancelled.status_code == 200:
                    n = len(cancelled.json())
                    console.print(f"[bold cyan]  Cancelled {n} stale Alpaca orders[/bold cyan]")
            except Exception as e:
                console.print(f"[yellow]  Could not cancel stale orders: {e}[/yellow]")
        else:
            console.print("[yellow]ALPACA_API_KEY/SECRET not set — running in simulation mode[/yellow]")
        # Alpaca: sync equity BEFORE StatusWriter init so the yaml has correct balance
        if self._broker:
            try:
                alpaca_equity = self._broker.get_equity()
                yaml_path = sd / "paper_account.yaml"  # /app/state/paper_account.yaml
                yaml.safe_dump({"starting_balance": alpaca_equity}, yaml_path.open("w"))
                self._starting_balance = alpaca_equity  # override before StatusWriter reads it
                console.print(f"[bold cyan]  Alpaca equity synced: ${alpaca_equity:,.2f}[/bold cyan]")
            except Exception as e:
                console.print(f"[yellow]  Could not sync Alpaca equity: {e}[/yellow]")
        self.exchange = ccxt.kraken({"enableRateLimit": True})
        self._status_writer = StatusWriter(self.status_path)  # now reads correct yaml
        # Exit Intelligence — initialized lazily at first trade close
        self._ei_analyzer = None
        self._ei_root = sd
        # Drawdown protection — tracks peak balance and pauses entries when -15% DD hit
        self._peak_balance_pct = 0.0  # 0 = uninitialized (set on first balance calculation)
        self._dd_pause_trades = False

    @property
    def _position_open(self) -> bool:
        return len(self._positions) > 0

    def load_strategy(self) -> dict:
        return yaml.safe_load(self.strategy_path.read_text())

    def quality_score(self, asset: str, tf_data: dict) -> float:
        """Score BTC/ETH/SOL setups. Higher = better trade candidate."""
        h4 = tf_data.get("4h", {})
        h1 = tf_data.get("1h", {})
        m15 = tf_data.get("15m", {})

        if not h4 or not h1 or not m15:
            return 0.0

        # Removed 4H EMA trend hard-gate — BTC/ETH stay flat for days; this blocked all scoring
        rsi_ok_long  = 30 < h1["rsi"] < 70
        rsi_ok_short = 30 < h1["rsi"] < 70
        price_ok_long  = h1["close"] > h1["ema50"]
        price_ok_short = h1["close"] < h1["ema50"]
        vol_ok = h1["volume"] > h1["vol_avg"]

        s = 0.0
        if rsi_ok_long and price_ok_long:
            s += 50
        if rsi_ok_short and price_ok_short:
            s += 50
        s += min(h1["rsi"] * 0.2, 10)
        s += min(h1["volume"] / h1["vol_avg"] * 5, 20)
        s += abs(50 - h1["rsi"]) * 0.3 if 40 < h1["rsi"] < 60 else 0
        return min(s, 100)

    async def evaluate_and_trade(self):
        """Fetch data, rank assets, check exits + fire entries (multi-position)."""
        strategy = self.load_strategy()
        max_open = strategy.get("max_open_positions", 4)
        starting_balance = self._status_writer._starting_balance

        # --- Alpaca: sync open positions at cycle start ---
        # Captures fills that happened via bracket TP/SL auto-exit, writes records
        if self._broker:
            try:
                alpaca_positions = self._broker.get_positions()
                still_open = set(alpaca_positions.keys())

                # Any _positions entry that vanished from Alpaca was closed externally
                closed_offline = [a for a in list(self._positions) if a not in still_open]
                for asset in closed_offline:
                    pos = self._positions[asset]
                    sys.stdout.write(
                        f"DETECTED CLOSE: {asset} closed via Alpaca TP/SL "
                        f"(entry={pos['entry_price']:.4f})\n"
                    )
                    sys.stdout.flush()
                    # Use last tracked MFE as estimate (bracket hit while we tracked it)
                    est_pnl = pos.get("_mfe", 0.0) * 0.5  # conservative: assume ~50% of peak was left
                    await self.close_trade(
                        asset, pos["side"], pos["entry_price"],
                        pos["entry_price"], est_pnl,
                        "bracket_auto_close", {}, strategy, pos,
                    )
                    del self._positions[asset]

                # Alpaca positions not yet in local mem → new fills; add them
                for asset, pos in alpaca_positions.items():
                    if asset not in self._positions:
                        console.print(
                            f"[bold green]SYNC Alpaca fill: {pos['side'].upper()} {asset} "
                            f"@ {pos['entry_price']:.4f}[/bold green]"
                        )
                        self._positions[asset] = {
                            "entry_price": pos["entry_price"],
                            "entry_time": datetime.now(timezone.utc).isoformat(),
                            "side": pos["side"],
                            "_mfe": 0.0,
                            "_mae": 0.0,
                            "indicators": {},
                            "_alpaca_filled": True,
                        }
                    else:
                        self._positions[asset]["_mfe"] = max(
                            self._positions[asset].get("_mfe", 0.0),
                            pos.get("unrealized_pl_pct", 0.0),
                        )
            except Exception as e:
                console.print(f"[yellow]Alpaca position sync failed: {e}[/yellow]")

        # --- Drawdown circuit breaker ---
        DD_THRESHOLD_PCT = 15.0  # pause if >15% below peak
        realized_pnl = sum(
            float(t.get("pnl_pct", 0.0)) for t in self._read_trades()
        )
        current_balance_pct = realized_pnl
        if self._peak_balance_pct == 0.0:
            self._peak_balance_pct = current_balance_pct  # initialize on first run
        elif current_balance_pct > self._peak_balance_pct:
            self._peak_balance_pct = current_balance_pct  # new peak

        dd_from_peak = self._peak_balance_pct - current_balance_pct
        if dd_from_peak >= DD_THRESHOLD_PCT and not self._dd_pause_trades:
            self._dd_pause_trades = True
            console.print(f"[bold red]DRAWDOWN TRIGGERED: {dd_from_peak:.1f}% below peak — pausing new entries[/bold red]")
        elif dd_from_peak < DD_THRESHOLD_PCT * 0.5 and self._dd_pause_trades:
            self._dd_pause_trades = False
            console.print(f"[bold green]Drawdown recovered ({dd_from_peak:.1f}% below peak) — resuming entries[/bold green]")

        # --- Fetch all asset data ---
        rated = []
        for asset in self.assets:
            retries = 3
            for attempt in range(retries):
                try:
                    tf_data = await fetch_ohlcv_multitimeframe(self.exchange, asset)
                    q = self.quality_score(asset, tf_data)
                    rated.append((q, asset, tf_data))
                    break
                except Exception as e:
                    delay = 2 ** attempt
                    console.print(f"[yellow]Retry {attempt+1}/{retries} for {asset}: {e}[/yellow]")
                    await asyncio.sleep(delay)
            else:
                console.print(f"[red]Skipping {asset} after 3 failures[/red]")

        if not rated:
            self.consecutive_failures += 1
            console.print(f"[red]All assets failed. Consecutive failures: {self.consecutive_failures}[/red]")
            if self.consecutive_failures >= self.max_failures:
                console.print("[bold red]Circuit breaker: max failures reached. Sleeping 5 min.[/bold red]")
                await asyncio.sleep(300)
            return

        self.consecutive_failures = 0
        rated.sort(reverse=True)

        # --- Per-asset exit + entry evaluation ---
        tp = strategy["take_profit_pct"]
        sl = strategy["stop_loss_pct"]
        direction = strategy["entry"].get("direction", "both")
        rsi_threshold = strategy["setup_1h"]["rsi_threshold"]
        # Default rsi_cross=55 means 15M RSI must cross ±5 pts vs the 50 center.
        # Previous default of 50 required M15_RSI<50 for shorts — impossible when
        # 1H RSI is 55-72 (overbought market) and M15 RSI stays 52-73.
        # Current market: 1H RSI=55-72 (overbought range), need shallower pullbacks.
        # 55 threshold: longs need M15_RSI > 55 (often met), shorts need M15_RSI < 45
        # (achievable when market pulls back from 60-73 down to 40-45).
        m15_cross = strategy["trigger_15m"].get("rsi_cross", 55)

        entries_to_open: list[tuple] = []   # (asset, tf_data) for new entries
        closes_triggered: int = 0

        for q, asset, tf_data in rated:
            h4 = tf_data["4h"]
            h1 = tf_data["1h"]
            m15 = tf_data["15m"]

            # --- Exit check: position on this asset? ---
            if asset in self._positions:
                pos = self._positions[asset]
                entry_price = pos["entry_price"]
                side = pos["side"]

                pnl_pct = (h1["close"] - entry_price) / entry_price * 100
                if side == "short":
                    pnl_pct = -pnl_pct

                should_exit_tp = pnl_pct >= tp
                should_exit_sl = pnl_pct <= -sl
                should_exit_rsi = (side == "long" and h1["rsi"] < strategy["early_exit_1h_rsi"]) or \
                                 (side == "short" and h1["rsi"] > (100 - strategy["early_exit_1h_rsi"]))
                # Trailing stop: if we were profitable (MFE > 0.5%) but gave back 70% of gains, exit
                # Replaces the old "exit" fallback which had no justification
                should_exit_trailing = (
                    pos["_mfe"] > 0.5 and
                    pnl_pct < (pos["_mfe"] * 0.3)  # only 30% of peak profit remaining
                )

                # Track MFE/MAE on every evaluation tick (including final tick before close)
                pos["_mfe"] = max(pos["_mfe"], pnl_pct)
                pos["_mae"] = min(pos["_mae"], pnl_pct)

                # Determine specific exit reason (priority: TP > SL > trailing > RSI)
                if should_exit_tp:
                    exit_reason_str = "take_profit"
                elif should_exit_sl:
                    exit_reason_str = "stop_loss"
                elif should_exit_trailing:
                    exit_reason_str = "trailing_stop"
                elif should_exit_rsi:
                    exit_reason_str = "early_exit_1h_rsi"
                else:
                    # This branch should never be reached if should_exit is computed correctly.
                    # Defensive: log and skip the close to prevent unjustified exits.
                    sys.stdout.write(
                        f"[WARN] No exit signal for {asset} but should_exit=True — "
                        f"skipping close (mfe={pos['_mfe']:+.2f}% pnl={pnl_pct:+.2f}%)\n"
                    )
                    sys.stdout.flush()
                    continue

                should_exit = should_exit_tp or should_exit_sl or should_exit_trailing or should_exit_rsi

                if should_exit:
                    await self.close_trade(asset, side, entry_price, h1["close"], pnl_pct, exit_reason_str, tf_data, strategy, pos)
                    del self._positions[asset]
                    closes_triggered += 1
                else:
                    # No exit — continue tracking
                    q_scores = {a: sc for sc, a, _ in rated}
                    sys.stdout.write(
                        f"TRACK {asset} {side.upper()} entry={entry_price:.2f} "
                        f"cur={h1['close']:.2f} pnl={pnl_pct:+.2f}% "
                        f"mfe={pos['_mfe']:+.2f}% mae={pos['_mae']:+.2f}% "
                        f"h1_RSI={h1['rsi']:.0f} 4H_close={h4['close']:.2f}\n"
                    )
                    sys.stdout.flush()
                continue

            # --- Entry check: room for more positions? ---
            if len(self._positions) >= max_open:
                continue

            # Entry signals: dual RSI — h1 RSI + m15 RSI cross
            # 4H trend filter only gates shorts (avoid catching falling knives).
            # Removed from longs so we can accumulate in choppy/dip markets.
            # When 4H is bullish AND RSI setup fires → high-quality long.
            # When 4H is bearish AND RSI setup fires → short.
            h4_trend_long_ok  = h4["close"] > h4["ema50"]
            h4_trend_short_ok = h4["close"] < h4["ema50"]
            vol_ratio = h1["volume"] / h1["vol_avg"]

            long_trigger = (
                self._dd_pause_trades is False
                and direction in ("long", "both")
                # No 4H filter on longs — ETH/SOL/XRP can bounce even when BTC leads down
                and rsi_threshold < h1["rsi"] < 70            # oversold recovery zone
                and m15_cross < m15["rsi"]                    # 15M RSI above threshold
            )
            short_trigger = (
                self._dd_pause_trades is False
                and direction in ("short", "both")
                and h4_trend_short_ok                         # 4H must be in downtrend
                and 30 < h1["rsi"] < (100 - rsi_threshold)   # overbought reversal zone
                and m15["rsi"] < (100 - m15_cross)           # 15M losing momentum below threshold
            )

            # Expose signal values every cycle for log debugging
            q_scores = {a: sc for sc, a, _ in rated}
            sys.stdout.write(
                f"SIGNAL dir={direction} h4trend={('short_ok' if h4_trend_short_ok else 'long_ok' if h4_trend_long_ok else 'NEUTRAL')} "
                f"asset={asset} h1_RSI={h1['rsi']:.0f} rsi_req={rsi_threshold:.0f} "
                f"m15_RSI={m15['rsi']:.0f} m15_req={m15_cross:.0f} "
                f"VS={vol_ratio:.2f} long={long_trigger} short={short_trigger} "
                f"qual={q:.1f} open_pos={len(self._positions)}/{max_open}\n"
            )
            sys.stdout.flush()

            if long_trigger:
                entries_to_open.append((asset, tf_data, "long"))
            elif short_trigger:
                entries_to_open.append((asset, tf_data, "short"))

        # --- Open queued entries (priority = quality score order) ---
        for asset, tf_data, side in entries_to_open:
            if len(self._positions) >= max_open:
                break
            h1 = tf_data["1h"]
            await self.open_trade(asset, side, h1["close"], tf_data, q_scores)

        # --- Exit Intelligence: Phase 2 advisor + Phase 3 shadow (after entries open) ---
        if self._ei_analyzer is not None:
            try:
                from .exit_intelligence_store import ExitIntelligenceStore
                store = ExitIntelligenceStore(self._ei_root)
                state = store.get_phase_state()
                phase = state.get("current_phase", 1)
                trades = self._read_trades()
                asset_stats = store.get_asset_stats()

                if phase >= 2 and self._positions:
                    recs = self._ei_analyzer.evaluate_open_positions(
                        self._positions, trades, asset_stats
                    )
                    if recs:
                        console.print(
                            f"[cyan]Exit Intel Phase {phase}: "
                            f"{len(recs)} recommendation(s) generated[/cyan]"
                        )

                if phase >= 3 and self._positions:
                    shadows = self._ei_analyzer.evaluate_shadow_exits(
                        self._positions, trades, asset_stats
                    )
                    if shadows:
                        console.print(
                            f"[magenta]Exit Intel Shadow: "
                            f"{len(shadows)} shadow exit(s) tracked[/magenta]"
                        )
            except Exception as ei_err:
                console.print(f"[yellow]Exit Intel advisor/shadow error: {ei_err}[/yellow]")

        # --- E2E checkpoint ---
        sv = strategy.get("version", "unknown")
        tcount = len(self._read_trades())
        pbytes = self.trades_path.stat().st_size if self.trades_path.exists() else 0
        positions = list(self._positions.keys())
        sys.stdout.write(
            f"E2E strat_ver={sv} trades={tcount} tbytes={pbytes} "
            f"open_positions={len(self._positions)}/{max_open} "
            f"assets={positions}\n"
        )
        sys.stdout.flush()

        # Write open positions to status.json for dashboard API
        tf_map = {asset: tf_data for q, asset, tf_data in rated}
        self._status_writer.write(self._positions, strategy, tf_map)

    async def open_trade(self, asset: str, side: str, price: float, tf_data: dict, q_scores: dict):
        self._positions[asset] = {
            "entry_price": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "side": side,
            "_mfe": 0.0,
            "_mae": 0.0,
            "_alpaca_filled": False,
            "indicators": {
                "signal_asset": asset,
                "rsi_4h": tf_data["4h"]["rsi"],
                "rsi_1h": tf_data["1h"]["rsi"],
                "rsi_15m": tf_data["15m"]["rsi"],
                "ema50_4h": tf_data["4h"]["ema50"],
                "ema200_4h": tf_data["4h"]["ema200"],
                "ema50_1h": tf_data["1h"]["ema50"],
                "volume_ratio": tf_data["1h"]["volume"] / tf_data["1h"]["vol_avg"],
                "quality_score": q_scores.get(asset, 0),
                "entry_price": price,
            },
        }

        # --- Alpaca: submit ONLY for longs (Alpaca paper rejects crypto shorts)
        if self._broker and side == "long":
            _strategy = self.load_strategy()
            equity = self._broker.get_equity()
            risk_pct = _strategy.get("risk_per_trade_pct", 1.0) / 100.0
            sl_pct = _strategy.get("stop_loss_pct", 0.3) / 100.0
            tp_pct = _strategy.get("take_profit_pct", 1.8) / 100.0
            order_id, filled_price = self._broker.submit_entry(
                symbol=asset, side=side, equity=equity,
                risk_pct=risk_pct, stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct, entry_price=price,
            )
            if order_id:
                self._positions[asset]["_alpaca_order_id"] = order_id
                self._positions[asset]["_alpaca_filled"] = True
                if filled_price and filled_price != price:
                    self._positions[asset]["entry_price"] = filled_price
                    console.print(
                        f"[bold cyan]  Alpaca: {order_id} filled @ ${filled_price:.4f}[/bold cyan]"
                    )
                else:
                    console.print(
                        f"[bold cyan]  Alpaca: {order_id} "
                        f"(equity=${equity:,.0f}, risk={risk_pct*100:.1f}%)[/bold cyan]"
                    )
            else:
                console.print(f"[bold yellow]  Alpaca long FAILED — stored locally[/bold yellow]")
        elif self._broker and side == "short":
            console.print(
                f"[bold yellow]  Alpaca crypto shorts not supported — simulation mode[/bold yellow]"
            )
        console.print(
            f"[bold green]OPEN {side.upper()} {price:.4f} {asset}[/bold green] "
            f"(score={q_scores.get(asset, 0):.1f}, pos={len(self._positions)}/4)"
        )

    async def close_trade(
        self,
        asset: str,
        side: str,
        entry_price: float,
        close_price: float,
        pnl_pct: float,
        reason: str,
        tf_data: dict,
        strategy: dict,
        pos: dict,
    ):
        # --- Alpaca: close only positions that were filled in Alpaca ---
        if self._broker and pos.get("_alpaca_filled"):
            close_side = "sell" if side == "long" else "buy"
            self._broker.cancel_open_orders(asset)
            result = self._broker.submit_market_order(asset, qty=None, side=close_side)
            if result:
                console.print(f"[bold cyan]  Alpaca close: {result}[/bold cyan]")
            else:
                sys.stdout.write(f"[WARN] Alpaca close order failed (may already be closed)\n")
                sys.stdout.flush()

        # --- Exit Intelligence: MFE/MAE tracking ---
        mfe_pct = pos.get("_mfe", 0.0)
        mae_pct = pos.get("_mae", 0.0)
        # profit_if_held: how much more profit we could have captured (unused ceiling)
        potential_profit_if_held = round(mfe_pct - pnl_pct, 4) if mfe_pct > pnl_pct else 0.0

        entry_ind = pos.get("indicators", {})
        trade = {
            "asset": asset,
            "side": side,
            "entry_price": entry_price,
            "exit_price": close_price,
            "pnl_pct": round(pnl_pct, 4),
            "mfe_pct": round(mfe_pct, 4),        # Max Favorable Excursion
            "mae_pct": round(mae_pct, 4),        # Max Adverse Excursion
            "profit_if_held_pct": potential_profit_if_held,  # missed ceiling vs actual
            "profit_if_held_abs": round(potential_profit_if_held, 2),
            "duration_sec": int(
                time.time()
                - datetime.fromisoformat(pos.get("entry_time", datetime.now(timezone.utc).isoformat())).timestamp()
            ),
            "exit_reason": reason,
            "indicators": {
                "entry": entry_ind,
                "exit": {
                    "rsi_4h": tf_data["4h"]["rsi"],
                    "rsi_1h": tf_data["1h"]["rsi"],
                    "rsi_15m": tf_data["15m"]["rsi"],
                    "close_4h": tf_data["4h"]["close"],
                    "close_1h": tf_data["1h"]["close"],
                },
            },
            "strategy_version": strategy["version"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(trade) + "\n"
        with self.trades_path.open("a") as f:
            f.write(line)

        # --- Exit Intelligence: score and analyze this trade ---
        try:
            if self._ei_analyzer is None:
                from .exit_intelligence import ExitIntelligenceAnalyzer
                self._ei_analyzer = ExitIntelligenceAnalyzer(self._ei_root)
            await self._ei_analyzer.on_trade_closed(trade)
        except Exception as ei_err:
            console.print(f"[yellow]Exit Intelligence error: {ei_err}[/yellow]")

        console.print(
            f"[green]CLOSE {side.upper()} {close_price:.4f} {asset} "
            f"pnl={pnl_pct:+.2f}% reason={reason}[/green] "
            f"(pos={len(self._positions)}/4)"
        )

        # Fire reflection when counter hits every N closed trades
        # OVERFITTING SAFEGUARD: never modify strategy with fewer than 4 closed trades
        # (reflection_every=2 would otherwise fire after just 2 trades — too few to be meaningful)
        trades = self._read_trades()
        MIN_TRADES_FOR_MODIFICATION = 4
        threshold_reached = len(trades) > 0 and len(trades) % self.goal["reflection_every"] == 0
        enough_data = len(trades) >= MIN_TRADES_FOR_MODIFICATION
        if threshold_reached:
            console.print(f"[bold cyan]Reflection triggered: {len(trades)} trades closed ({'sufficient data' if enough_data else 'TOO FEW — skipping modification'})[/bold cyan]")
            if enough_data:
                if os.getenv("HERMES_REFLECTION_MODE", "").lower() == "true":
                    hermes_ok = shutil.which("hermes") is not None
                    if hermes_ok:
                        from .reflect import run_hermes_reflection
                        # Retry logic: try Hermes up to 2 times before fallback
                        hermes_success = False
                        for attempt in range(2):
                            try:
                                hyp = await run_hermes_reflection(trades, self.strategy_path, self.trades_path, self.hypotheses_path)
                                # Check if Hermes actually produced a real response (not the fallback message)
                                if hyp.get("reason") and "Hermes parse failed" not in hyp.get("reason", ""):
                                    hermes_success = True
                                    console.print(f"[green]Hermes reflection applied (attempt {attempt + 1})[/green]")
                                    break
                                else:
                                    console.print(f"[yellow]Hermes parse failed, attempt {attempt + 1}/2 — retrying...[/yellow]")
                                    if attempt == 1:
                                        console.print(f"[yellow]Hermes failed {2} consecutive times — using fallback[/yellow]")
                            except Exception as hermes_err:
                                console.print(f"[red]Hermes error on attempt {attempt + 1}: {hermes_err}[/red]")
                                if attempt == 1:
                                    console.print(f"[red]Hermes exhausted all retries — using fallback[/red]")
                        if not hermes_success:
                            self._run_fallback_safe(trades)
                    else:
                        self._run_fallback_safe(trades)
                        console.print(f"[yellow]Fallback reflection applied (hermes CLI not in container)[/yellow]")
                else:
                    self._run_fallback_safe(trades)
                    console.print(f"[yellow]Fallback reflection applied (HERMES_REFLECTION_MODE not true)[/yellow]")
            else:
                console.print(f"[yellow]Skipping reflection — only {len(trades)} trades, need {MIN_TRADES_FOR_MODIFICATION}+ for reliable analysis[/yellow]")

    def _run_fallback_safe(self, trades: list):
        """Run fallback reflection with spam guard and corrupted value recovery."""
        from .reflect import run_fallback_reflection, _compute_trade_stats

        # Strategy health check — detect corrupted values
        try:
            import yaml as _yaml
            strat = _yaml.safe_load(self.strategy_path.read_text())
            rsi = strat.get("entry", {}).get("rsi_threshold", 30)
            if rsi < 0 or rsi > 80:
                console.print(f"[bold red]StrategyCorruption: rsi_threshold={rsi} is invalid — "
                              f"resetting to safe value 30[/bold red]")
                d = strat.setdefault("entry", {})
                d["rsi_threshold"] = 30
                d["threshold"] = 30
                strat.setdefault("setup_1h", {})["rsi_threshold"] = 30
                self.strategy_path.write_text(_yaml.dump(strat))
        except Exception as e:
            console.print(f"[red]Strategy health check error: {e}[/red]")

        # Spam guard: track consecutive fallbacks to detect repeated failures
        hyp_path = self.hypotheses_path
        consecutive_fallback = 0
        if hyp_path.exists():
            try:
                lines = [l.strip() for l in hyp_path.read_text().strip().split("\n") if l.strip()]
                last_entries = lines[-5:] if len(lines) >= 5 else lines
                # Check if last 3 were all fallbacks with the same change
                same_vars = {}
                for l in last_entries:
                    try:
                        h = json.loads(l)
                        if h.get("mode") == "fallback" or (h.get("reason") or "").includes("Hermes parse failed"):
                            v = h.get("variable", "unknown")
                            same_vars[v] = same_vars.get(v, 0) + 1
                    except json.JSONDecodeError:
                        pass
                # If same variable changed 3+ times in a row, halt fallback spam
                for var, count in same_vars.items():
                    if count >= 3:
                        console.print(f"[bold red]SpamGuard: {var} modified {count}x in a row — "
                                      f"skipping fallback, waiting for Hermes[/bold red]")
                        return
            except Exception:
                pass

        from .reflect import run_fallback_reflection
        hyp = run_fallback_reflection(trades, self.strategy_path, self.trades_path, self.hypotheses_path)
        console.print(f"[yellow]Fallback reflection applied[/yellow]")

    def _read_trades(self) -> list:
        if not self.trades_path.exists():
            return []
        try:
            content = self.trades_path.read_text().strip()
            if not content:
                return []
        except Exception:
            return []
        trades = []
        for i, line in enumerate(content.split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                console.print(f"[yellow]WARN: Skipping malformed trade line {i+1}: {line[:60]}[/yellow]")
        return trades

    def write_heartbeat(self):
        hb = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": self.asset,
            "mode": "paper",
            "position_open": self._position_open,
        }
        self.heartbeat_path.write_text(json.dumps(hb, indent=2))

    async def run(self):
        import json as _json

        strategy = self.load_strategy()
        trades = self._read_trades()

        # Enumerate persisted state from volume — proves persistence survived restart
        state_dir = _STATE_DIR
        self._ei_root = state_dir

        # --- Exit Intelligence: backfill historical trades ---
        try:
            from .exit_intelligence import backfill_historical_trades
            from .exit_intelligence_store import ExitIntelligenceStore
            backfilled = await backfill_historical_trades(state_dir, trades)
            sys.stdout.write(f"[EXIT INTEL] Backfill complete: {backfilled} historical trades processed\n")
        except Exception as ei_err:
            sys.stdout.write(f"[EXIT INTEL] Backfill skipped: {ei_err}\n")
        sys.stdout.flush()

        try:
            state_files = sorted([
                str(p.relative_to(state_dir)) for p in state_dir.rglob("*")
                if p.is_file() and ".venv" not in str(p)
            ])
            bp_data = {}
            bp = state_dir / "bootstrap_proof.json"
            if bp.exists():
                bp_data = _json.loads(bp.read_text())
        except Exception:
            state_files = []
            bp_data = {}

        sys.stdout.write(
            f"INIT strat_ver={strategy['version']} trades={len(trades)} "
            f"max_pos={strategy.get('max_open_positions',4)} "
            f"rsi_th={strategy['setup_1h']['rsi_threshold']} "
            f"stop_loss={strategy['stop_loss_pct']} tp={strategy['take_profit_pct']} "
            f"persist_epoch={bp_data.get('persistence_epoch','?')} "
            f"volume_files={len(state_files)}\n"
        )
        sys.stdout.flush()
        console.print(f"[bold green]Worker running. Ctrl+C to stop.[/bold green]")
        iteration = 0
        while True:
            iteration += 1
            t0 = time.time()
            try:
                await self.evaluate_and_trade()
            except Exception as e:
                console.print(f"[red]Error in loop iteration {iteration}: {e}[/red]")
            self.write_heartbeat()
            elapsed = time.time() - t0
            sleep_for = max(0, 60 - elapsed)
            await asyncio.sleep(sleep_for)