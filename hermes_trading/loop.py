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
from .score import score_trades


console = Console()


class StatusWriter:
    """Writes status.json each cycle so dashboard API can read open positions."""

    def __init__(self, path: Path):
        self.path = path

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
            self.path.write_text(json.dumps({
                "open_positions": expanded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception:
            pass  # Non-critical


class TradingLoop:
    """Async loop: every 60s evaluate strategy, fire paper trades."""

    def __init__(self, asset: str, goal: dict):
        self.asset = asset
        self.goal = goal
        self.assets = goal.get("assets", [asset])
        self.strategy_path = Path(__file__).parent.parent / "state" / "strategy.yaml"
        self.trades_path = Path(__file__).parent.parent / "state" / "trades.jsonl"
        self.heartbeat_path = Path(__file__).parent.parent / "state" / "heartbeat.json"
        self.hypotheses_path = Path(__file__).parent.parent / "state" / "hypotheses.jsonl"
        self.status_path = Path(__file__).parent.parent / "state" / "status.json"
        self.consecutive_failures = 0
        self.max_failures = 5
        # Positions dict: {signal_asset: {entry_price, entry_time, side, indicators}}
        self._positions: dict[str, dict] = {}
        self.exchange = ccxt.kraken({"enableRateLimit": True})
        self._status_writer = StatusWriter(self.status_path)

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
        m15_cross = strategy["trigger_15m"]["rsi_cross"]

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

                should_exit = (
                    pnl_pct >= tp or
                    pnl_pct <= -sl or
                    (side == "long" and h1["rsi"] < strategy["early_exit_1h_rsi"]) or
                    (side == "short" and h1["rsi"] > (100 - strategy["early_exit_1h_rsi"]))
                )

                if should_exit:
                    await self.close_trade(asset, side, entry_price, h1["close"], pnl_pct, "exit", tf_data, strategy, pos)
                    del self._positions[asset]
                    closes_triggered += 1
                else:
                    # Track MFE/MAE for Exit Intelligence (pnl_pct is signed: positive = winning)
                    pos["_mfe"] = max(pos["_mfe"], pnl_pct)
                    pos["_mae"] = min(pos["_mae"], pnl_pct)
                    # Already in this asset — skip entry, keep monitoring
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
            vol_ratio = h1["volume"] / h1["vol_avg"]
            long_trigger = (
                direction in ("long", "both")
                and rsi_threshold < h1["rsi"] < 70          # oversold recovery zone
                and m15_cross < m15["rsi"]                  # 15M gaining momentum
            )
            short_trigger = (
                direction in ("short", "both")
                and 30 < h1["rsi"] < (100 - rsi_threshold)  # overbought reversal zone
                and m15["rsi"] < (100 - m15_cross)           # 15M losing momentum
            )

            # Expose signal values every cycle for log debugging
            q_scores = {a: sc for sc, a, _ in rated}
            sys.stdout.write(
                f"SIGNAL asset={asset} h1_RSI={h1['rsi']:.0f} rsi_req={rsi_threshold:.0f} "
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
            "_mfe": 0.0,   # Max Favorable Excursion (best % in our favor during trade)
            "_mae": 0.0,   # Max Adverse Excursion (worst % against us during trade)
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
        state_dir = Path(__file__).parent.parent / "state"
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