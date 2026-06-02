"""Deterministic reflection (fallback) + Hermes-aware reflection."""
import json
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml


HISTORY_DIR = Path(__file__).parent.parent / "state" / "history"
HYPOTHESIS_PROMPT_VERSION = "v25trades"

# ============================================================
# Hermes Knowledge System — 30-domain structured storage
# ============================================================

KNOWLEDGE_DOMAINS = [
    "RSI_indicator",
    "MACD_indicator",
    "Bollinger_Bands",
    "EMA_SMA_crossovers",
    "Volume_analysis",
    "Risk_management",
    "Market_structure",
    "Sentiment_Analysis",
    "Order_flow",
    "Portfolio_construction",
    "Strategy_development",
    "Backtesting_methodology",
    "Behavioral_finance",
    "Macro_economics",
    "Onchain_metrics",
    "DeFi_protocols",
    "Stablecoin_flows",
    "Exchange_flows",
    "Asset_correlations",
    "Time_based_strategies",
    "Exit_strategy",
    "Entry_signal_quality",
    "Market_regime",
    "Volatility_analysis",
    "Liquidity_analysis",
    "News_event_impact",
    "Social_signals",
    "Regulatory_developments",
    "Project_fundamentals",
    "Market_microstructure",
]


class KnowledgeStorage:
    """Manages knowledge.jsonl — structured storage for 30-domain insights."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "knowledge.jsonl"
        self.domains = {d: {"count": 0, "total_impact": 0.0, "entries": []} for d in KNOWLEDGE_DOMAINS}
        # Track which domains contributed to each trade
        self.trade_domain_map: dict[str, List[str]] = {}
        # Track exit_reason -> domain associations
        self.exit_domain_map: dict[str, List[str]] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                domain = entry.get("domain")
                if domain in self.domains:
                    self.domains[domain]["count"] += 1
                    self.domains[domain]["total_impact"] += entry.get("impact_score", 0.0)
                    self.domains[domain]["entries"].append(entry)
            except json.JSONDecodeError:
                pass

    def _append(self, entry: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def add(self, domain: str, category: str, insight: str,
            evidence: Optional[dict] = None, impact_score: float = 0.5,
            trade_id: Optional[str] = None, mfe_pct: float = 0.0,
            mae_pct: float = 0.0, exit_reason: Optional[str] = None,
            asset: Optional[str] = None, side: Optional[str] = None,
            pnl_pct: float = 0.0):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "domain": domain,
            "category": category,
            "insight": insight,
            "evidence": evidence or {},
            "impact_score": round(impact_score, 3),
            "trade_id": trade_id,
            "mfe_pct": round(mfe_pct, 4),
            "mae_pct": round(mae_pct, 4),
            "exit_reason": exit_reason,
            "asset": asset,
            "side": side,
            "pnl_pct": round(pnl_pct, 4),
            "session_trades": self._total_entries(),
        }
        self._append(entry)
        if domain in self.domains:
            self.domains[domain]["count"] += 1
            self.domains[domain]["total_impact"] += impact_score
            self.domains[domain]["entries"].append(entry)
        if trade_id:
            self.trade_domain_map[trade_id] = self.trade_domain_map.get(trade_id, []) + [domain]
        if exit_reason:
            self.exit_domain_map[exit_reason] = self.exit_domain_map.get(exit_reason, []) + [domain]

    def _total_entries(self) -> int:
        return sum(d["count"] for d in self.domains.values())

    def get_relevant(self, trade: dict, domain_hints: Optional[List[str]] = None,
                     limit: int = 5) -> List[dict]:
        """Return knowledge entries relevant to the given trade."""
        results = []
        asset = trade.get("asset", "")
        domain_hints = domain_hints or []
        for domain in KNOWLEDGE_DOMAINS:
            entries = self.domains.get(domain, {}).get("entries", [])
            # Weight: recent entries + entries matching asset + entries with high impact
            for e in entries[-20:]:  # last 20 per domain
                score = 0.0
                if e.get("asset") and asset.startswith(e["asset"].replace("/USDT", "")):
                    score += 2.0
                if domain in domain_hints:
                    score += 1.5
                score += (e.get("impact_score") or 0.5) * 0.5
                if score > 0.5:
                    results.append((score, e))
        results.sort(key=lambda x: -x[0])
        return [e for _, e in results[:limit]]

    def get_all_entries(self, limit: int = 100) -> List[dict]:
        """Return all recent knowledge entries across all domains."""
        all_entries = []
        for domain in KNOWLEDGE_DOMAINS:
            entries = self.domains.get(domain, {}).get("entries", [])
            all_entries.extend(entries[-min(5, len(entries)):])
        return sorted(all_entries, key=lambda e: e.get("timestamp", ""), reverse=True)[:limit]

    def get_summary(self) -> dict:
        """Return domain coverage summary."""
        return {
            domain: {
                "count": d["count"],
                "avg_impact": round(d["total_impact"] / d["count"], 3) if d["count"] > 0 else 0.0,
                "latest": d["entries"][-1]["timestamp"] if d["entries"] else None,
            }
            for domain, d in self.domains.items()
        }

    def get_domain_diversity(self) -> int:
        """Count how many domains have at least one insight."""
        return sum(1 for d in self.domains.values() if d["count"] > 0)


def derive_knowledge_from_trade(trade: dict, storage: KnowledgeStorage) -> List[str]:
    """Analyze a closed trade and store knowledge in relevant domains."""
    domain_insights = []
    pnl = trade.get("pnl_pct", 0.0)
    mfe = trade.get("mfe_pct", 0.0)
    mae = trade.get("mae_pct", 0.0)
    exit_reason = trade.get("exit_reason", "unknown")
    asset = trade.get("asset", "")
    side = trade.get("side", "")
    ind = trade.get("indicators", {}).get("entry", {})
    is_win = pnl >= 0

    # 1. Entry signal quality (RSI)
    entry_rsi = ind.get("rsi_1h", 50)
    if entry_rsi < 30:
        storage.add(
            "RSI_indicator", "oversold_entry",
            f"Oversold entry (RSI_1h={entry_rsi:.1f}) on {asset}: "
            f"{'profitable' if is_win else 'loss'}. P&L={pnl:+.2f}%",
            evidence={"rsi": entry_rsi, "asset": asset}, impact_score=0.6 if is_win else 0.3,
            trade_id=asset, mfe_pct=mfe, mae_pct=mae, exit_reason=exit_reason,
            asset=asset, side=side, pnl_pct=pnl
        )
        domain_insights.append("RSI_indicator")

    # 2. Exit strategy analysis
    exit_quality = "optimal"
    if mfe > 0 and abs(mae) > abs(mfe) * 0.5:
        exit_quality = "early_exit"
    if mfe > abs(pnl) * 2:
        exit_quality = "significant_slippage"
    quality_msg = f"Exit quality: {exit_quality}. MFE={mfe:+.2f}%, MAE={mae:+.2f}%, P&L={pnl:+.2f}%"
    storage.add(
        "Exit_strategy", f"exit_quality_{exit_quality}",
        quality_msg,
        evidence={"mfe": mfe, "mae": mae, "pnl": pnl},
        impact_score=0.7, trade_id=asset, mfe_pct=mfe, mae_pct=mae,
        exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
    )
    domain_insights.append("Exit_strategy")

    # 3. MFE/MAE relationship -> indicates ceiling/floor capture
    if mfe != 0 and mae != 0:
        mfe_mae_ratio = abs(mfe) / max(abs(mae), 0.0001)
        if mfe_mae_ratio > 3:
            storage.add(
                "Risk_management", "high_mfe_mae_ratio",
                f"{asset} captured {mfe:+.2f}% of favorable range vs only {mae:+.2f}% adverse. "
                f"Ratio={mfe_mae_ratio:.1f}x — {'good exit discipline' if is_win else 'missed ceiling'}",
                evidence={"mfe": mfe, "mae": mae, "ratio": mfe_mae_ratio},
                impact_score=0.5, trade_id=asset, mfe_pct=mfe, mae_pct=mae,
                exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
            )
            domain_insights.append("Risk_management")

    # 4. Volume signal quality
    vol_ratio = ind.get("volume_ratio", 1.0)
    if vol_ratio:
        if vol_ratio < 0.5:
            storage.add(
                "Volume_analysis", "low_volume_entry",
                f"Entry on low volume (ratio={vol_ratio:.2f}). {'Won' if is_win else 'Lost'}. "
                f"Quality score: {ind.get('quality_score', '?')}",
                evidence={"volume_ratio": vol_ratio, "quality_score": ind.get("quality_score")},
                impact_score=0.4, trade_id=asset, mfe_pct=mfe, mae_pct=mae,
                exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
            )
            domain_insights.append("Volume_analysis")

    # 5. EMA trend alignment
    ema50 = ind.get("ema50_1h", 0)
    price = trade.get("entry_price", 0)
    if ema50 and price:
        above_ema = price > ema50
        correct_align = (above_ema and side == "long") or (not above_ema and side == "short")
        storage.add(
            "EMA_SMA_crossovers", "ema_alignment",
            f"{asset} entry {'aligned with' if correct_align else 'against'} EMA (EMA50={ema50:.2f}, price={price:.2f}). "
            f"{'Correct' if correct_align else 'Wrong'} direction. Result: {pnl:+.2f}%",
            evidence={"ema50": ema50, "price": price, "side": side, "correct": correct_align},
            impact_score=0.5, trade_id=asset, mfe_pct=mfe, mae_pct=mae,
            exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
        )
        domain_insights.append("EMA_SMA_crossovers")

    # 6. Market regime (time-of-day awareness)
    entry_time = trade.get("timestamp", "")
    if entry_time:
        try:
            hour = int(entry_time[11:13]) if len(entry_time) > 11 else 12
            if 2 <= hour < 6:
                storage.add(
                    "Market_regime", "low_liquidity_session",
                    f"{asset} entry during low-liquidity session (hour={hour} UTC). "
                    f"Result: {pnl:+.2f}%. {'Caution' if not is_win else 'Overnight holding worked'}",
                    evidence={"hour_utc": hour},
                    impact_score=0.3, trade_id=asset, mfe_pct=mfe, mae_pct=mae,
                    exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
                )
                domain_insights.append("Market_regime")
        except (ValueError, IndexError):
            pass

    # 7. Exit reason mapping
    exit_insights = {
        "stop_loss": "Stop loss triggered — consider stop placement or volatility adjustment",
        "take_profit": "Take profit hit at target",
        "early_exit_1h_rsi": "RSI crossed 50 — trend reversal signal triggered early exit",
        "signal_exit": "Signal no longer confirmed — quality deteriorated",
    }
    if exit_reason in exit_insights:
        storage.add(
            "Exit_strategy", f"exit_reason_{exit_reason}",
            f"{asset} exited via {exit_reason}: {exit_insights.get(exit_reason, '')} "
            f"P&L={pnl:+.2f}%, MFE={mfe:+.2f}%",
            evidence={"reason": exit_reason, "mfe": mfe, "mae": mae, "pnl": pnl},
            impact_score=0.6 if is_win else 0.4,
            trade_id=asset, mfe_pct=mfe, mae_pct=mae,
            exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
        )
        if "Exit_strategy" not in domain_insights:
            domain_insights.append("Exit_strategy")

    # 8. Entry signal quality score
    qs = ind.get("quality_score", 50)
    storage.add(
        "Entry_signal_quality", f"quality_{int(qs / 10) * 10}s",
        f"{asset} entry quality score: {qs:.1f}/100. {'High quality setup' if qs >= 65 else 'Mediocre setup' if qs >= 50 else 'Poor setup'}. "
        f"P&L={pnl:+.2f}%",
        evidence={"quality_score": qs, "rsi_1h": entry_rsi, "volume_ratio": vol_ratio},
        impact_score=0.6 if is_win else 0.4,
        trade_id=asset, mfe_pct=mfe, mae_pct=mae,
        exit_reason=exit_reason, asset=asset, side=side, pnl_pct=pnl
    )
    domain_insights.append("Entry_signal_quality")

    return domain_insights


def build_knowledge_context(storage: KnowledgeStorage, recent_trades: List[dict]) -> str:
    """Build a context string from relevant knowledge for the Hermes prompt."""
    if not storage.get_domain_diversity():
        return ""

    # Determine primary domains for this trade set
    domain_counts = {}
    for trade in recent_trades:
        domains = KNOWLEDGE_DOMAINS  # Consider all domains broadly first
        asset = trade.get("asset", "")
        for d in domains:
            domain_counts[d] = domain_counts.get(d, 0)

    # Get all recent knowledge
    entries = storage.get_all_entries(limit=30)
    if not entries:
        return ""

    lines = []
    for e in entries:
        lines.append(
            f"  [{e['domain']}] {e['insight'][:120]}"
            f" (ts={e['timestamp'][5:16]} impact={e['impact_score']:.1f})"
        )

    return (
        "\n\n== PRIOR KNOWLEDGE (learned from previous trades, relevant domains) ==\n"
        + "\n".join(lines)
        + f"\n\nDomains covered so far: {storage.get_domain_diversity()}/{len(KNOWLEDGE_DOMAINS)}"
    )


def bump_version(current: str) -> str:
    n = int(current.lstrip("v0").lstrip("0") or "1")
    return f"v{n+1:04d}"


def run_fallback_reflection(trades: List[dict], strategy_path: Path, trades_path: Path, hypotheses_path: Path) -> dict:
    """Deterministic fallback: used until Hermes is installed."""
    strategy = yaml.safe_load(strategy_path.read_text())
    prev_version = strategy["version"]
    new_version = bump_version(prev_version)

    realized = sum(t["pnl_pct"] for t in trades)
    target = 5.0  # hardcoded from goal; fallback is dumb
    max_dd = _max_drawdown_fast(trades)

    # Build hypothesis
    if realized < target:
        change = {"variable": "entry.rsi_threshold", "direction": "loosen", "amount": 2}
        reason = f"Realised return {realized:+.1f}% < target {target:+.1f}%. Loosening RSI threshold to allow more entries."
        strategy["entry"]["threshold"] = strategy["entry"].get("threshold", 30) - 2
        strategy["setup_1h"]["rsi_threshold"] = strategy["setup_1h"].get("rsi_threshold", 45) - 2
    elif max_dd > 8.0:
        change = {"variable": "stop_loss_pct", "direction": "tighten", "amount": 0.2}
        reason = f"Max drawdown {max_dd:.1f}% exceeded 8%. Tightening stop loss by 0.2%."
        strategy["stop_loss_pct"] = round(strategy["stop_loss_pct"] - 0.2, 2)
    else:
        change = {"variable": "position_size_r", "direction": "increase", "amount": 0.05}
        reason = f"Strategy performing well. Increasing position size by 5%."
        strategy["position_size_r"] = round(strategy.get("position_size_r", 0.5) + 0.05, 2)

    strategy["version"] = new_version

    # Save history
    HISTORY_DIR.mkdir(exist_ok=True)
    (HISTORY_DIR / f"{new_version}.yaml").write_text(yaml.dump(strategy))
    (HISTORY_DIR / f"{prev_version}.yaml").write_text(strategy_path.read_text())

    # Append hypothesis
    hypothesis = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "fallback",
        "version": new_version,
        "change": change,
        "reason": reason,
        "trades_analyzed": len(trades),
        "realized_return": round(realized, 4),
        "max_drawdown": round(max_dd, 4),
    }
    hypotheses_path.open("a").write(json.dumps(hypothesis) + "\n")

    # Save strategy
    strategy_path.write_text(yaml.dump(strategy))

    print(f"[CYAN]Fallback reflection: {prev_version} -> {new_version} | {change['variable']} {change['direction']} {change['amount']}[/CYAN]")
    return hypothesis


def _max_drawdown_fast(trades: List[dict]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t["pnl_pct"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_trade_stats(trades: List[dict]) -> dict:
    """Compute aggregated performance statistics from a list of closed trades."""
    if not trades:
        return {}
    wins = [t for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pct", 0) <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t.get("pnl_pct", 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    asset_pnl: dict = {}
    for t in trades:
        a = t.get("asset", "?")
        asset_pnl.setdefault(a, []).append(t.get("pnl_pct", 0))
    reason_counts: dict = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        reason_counts[r] = reason_counts.get(r, 0) + 1
    total_duration = sum(t.get("duration_sec", 0) for t in trades)
    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "loss_rate": round(len(losses) / len(trades) * 100, 1) if trades else 0,
        "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 4) if losses else 0.0,
        "profit_factor": (
            round(abs(sum(t["pnl_pct"] for t in wins) /
                   (sum(t["pnl_pct"] for t in losses) or -0.0001)), 2)
        ),
        "total_pnl": round(sum(t.get("pnl_pct", 0) for t in trades), 4),
        "max_drawdown": round(max_dd, 4),
        "avg_duration_hours": round(total_duration / len(trades) / 3600, 1),
        "asset_performance": {a: round(sum(v), 2) for a, v in asset_pnl.items()},
        "exit_reason_breakdown": reason_counts,
    }


def _build_trade_listing(trades: List[dict], limit: int = 10) -> str:
    """Human-readable listing of individual trades, newest last."""
    recent = trades[-limit:]
    lines = []
    for t in recent:
        dur_h = t.get("duration_sec", 0) // 3600
        dur_m = (t.get("duration_sec", 0) % 3600) // 60
        dur_str = f"{dur_h}h{dur_m}m" if dur_h > 0 else f"{dur_m}m"
        entry_rsi = t.get("indicators", {}).get("entry", {}).get("rsi_1h", "?")
        lines.append(
            f"  {t.get('asset', '?'):10s} {t.get('side', '?'):5s} "
            f"pnl={t.get('pnl_pct', 0):+.2f}% dur={dur_str} exit={t.get('exit_reason', '?'):5s} "
            f"entry_RSI={entry_rsi} strat={t.get('strategy_version', '?')}"
        )
    return "\n".join(lines)


async def run_hermes_reflection(trades: List[dict], strategy_path: Path, trades_path: Path, hypotheses_path: Path) -> dict:
    """Hermes mode: calls the `hermes` CLI with trade data, parses the hypothesis."""
    import subprocess
    strategy = yaml.safe_load(strategy_path.read_text())
    prev_version = strategy["version"]
    state_dir = Path(__file__).parent.parent / "state"

    # Load knowledge storage and derive insights from trades
    storage = KnowledgeStorage(state_dir)
    for trade in trades:
        derive_knowledge_from_trade(trade, storage)

    prompt = build_hermes_prompt(trades, strategy, storage, include_knowledge=True)
    result = subprocess.run(
        ["hermes", "-z", prompt],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    hypothesis = parse_hermes_output(output)
    new_version = apply_hypothesis(hypothesis, strategy_path, hypotheses_path, _hermes_context(), len(trades), trades)
    print(f"[CYAN]Hermes reflection [{prev_version} -> {new_version}]: "
          f"{hypothesis['variable']} | {hypothesis['direction']} {hypothesis['amount']} | "
          f"reason: {hypothesis['reason'][:80]}...[/CYAN]")
    print(f"[CYAN]Knowledge: {storage.get_domain_diversity()}/{len(KNOWLEDGE_DOMAINS)} domains covered[/CYAN]")

    # Read back the saved hypothesis to return it
    if hypotheses_path.exists():
        lines = [l.strip() for l in hypotheses_path.read_text().strip().split("\n") if l.strip()]
        if lines:
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                pass
    return {**hypothesis, "version": new_version, "mode": "hermes", "trades_analyzed": len(trades),
            "timestamp": datetime.now(timezone.utc).isoformat()}


def build_hermes_prompt(trades: List[dict], strategy: dict, storage: Optional[KnowledgeStorage] = None,
                        include_knowledge: bool = True) -> str:
    recent = trades[-25:]
    stats = _compute_trade_stats(recent)
    trade_listing = _build_trade_listing(recent, limit=10)

    # Load previous hypotheses for context (if available on disk)
    hypotheses_note = ""
    state_dir = Path(__file__).parent.parent / "state"
    hyp_path = state_dir / "hypotheses.jsonl"
    if hyp_path.exists():
        hyp_lines = [l.strip() for l in hyp_path.read_text().strip().split("\n") if l.strip()]
        last_3 = []
        for line in hyp_lines[-3:]:
            try:
                h = json.loads(line)
                last_3.append(
                    f"  v{h['version']} | {h.get('variable','?')} | "
                    f"{h.get('direction','?')} {h.get('amount','?')} | {h.get('reason','?')[:60]}"
                )
            except Exception:
                pass
        if last_3:
            hypotheses_note = "\n\nRecent reflection history (last 3):\n" + "\n".join(last_3)

    knowledge_note = ""
    if include_knowledge and storage:
        knowledge_note = build_knowledge_context(storage, recent)

    return f"""Trading Strategy Advisor — analyze closed trades and propose one improvement.

OUTPUT FORMAT (JSON only, no additional text):
{{"variable": "strategy.path", "direction": "loosen|tighten|increase|decrease", "amount": float, "reason": "..."}}

== CURRENT STRATEGY ==
{yaml.dump(strategy)}

== CLOSED TRADES (newest last, last 10 shown) ==
{trade_listing}
Total trades in analysis window: {len(recent)}

== PERFORMANCE STATISTICS ==
  Total trades analyzed:   {stats.get('total_trades', 0)}
  Win rate:               {stats.get('win_rate', 0)}%
  Loss rate:              {stats.get('loss_rate', 0)}%
  Average winner:         {stats.get('avg_win', 0):+.4f}%
  Average loser:          {stats.get('avg_loss', 0):+.4f}%
  Profit factor:          {stats.get('profit_factor', 0)}
  Total PnL:              {stats.get('total_pnl', 0):+.4f}%
  Max drawdown:           {stats.get('max_drawdown', 0):.4f}%
  Avg trade duration:     {stats.get('avg_duration_hours', 0):.1f} hours
  Asset PnL breakdown:    {stats.get('asset_performance', {})}
  Exit reason breakdown:  {stats.get('exit_reason_breakdown', {})}

== KNOWLEDGE DOMAINS ==
Covered domains: {storage.get_domain_diversity()}/{len(KNOWLEDGE_DOMAINS)} ({', '.join(d for d in KNOWLEDGE_DOMAINS if storage.domains.get(d, {}).get('count', 0) > 0) or 'none yet'}){knowledge_note}
{hypotheses_note}

== YOUR TASK ==
Analyze the trades above, compute performance, and propose exactly ONE parameter change
to improve the strategy. Consider: Are losers too large vs winners? Are exits too early
or too late? Is the win rate sustainable? Look for systematic weaknesses. Also use the
PRIOR KNOWLEDGE section above — if similar patterns were observed before, reference them.

Choose the variable, direction, and amount that has the highest expected improvement.
Output only the JSON object."""


def parse_hermes_output(output: str) -> dict:
    import re, json as _json
    # Try to extract first valid JSON object from output (handles arrays too)
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', output, re.DOTALL)
    if not m:
        # Fallback: try raw match
        m = re.search(r'\{.*\}', output, re.DOTALL)
    if m:
        try:
            data = _json.loads(m.group())
            # If array, take first element
            if isinstance(data, list) and len(data) > 0:
                data = data[0]
            return {
                "variable": data["variable"],
                "direction": data["direction"],
                "amount": float(data["amount"]),
                "reason": data["reason"]
            }
        except (_json.JSONDecodeError, KeyError, ValueError):
            pass
    return {"variable": "entry.rsi_threshold", "direction": "loosen", "amount": 2,
            "reason": "Hermes parse failed — default fallback: loosen RSI threshold."}


def _hermes_context() -> dict:
    import os
    return {
        "mode": "hermes",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def apply_hypothesis(hypothesis: dict, strategy_path: Path, hypotheses_path: Path,
                    ctx: dict, total_trades: int = 0, trades: List[dict] = None) -> str:
    strategy = yaml.safe_load(strategy_path.read_text())
    new_version = bump_version(strategy["version"])

    # Apply change to strategy dict
    var = hypothesis["variable"]
    val = hypothesis["amount"]
    direct = hypothesis["direction"]

    parts = var.split(".")
    d = strategy
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    key = parts[-1]

    if direct == "loosen":
        d[key] = round(d.get(key, 0) - val, 2)
    elif direct == "tighten":
        d[key] = round(d.get(key, 0) + val, 2)
    elif direct == "increase":
        d[key] = round(d.get(key, 0) + val, 2)
    elif direct == "decrease":
        d[key] = round(d.get(key, 0) - val, 2)

    strategy["version"] = new_version

    HISTORY_DIR.mkdir(exist_ok=True)
    (HISTORY_DIR / f"{new_version}.yaml").write_text(yaml.dump(strategy))
    strategy_path.write_text(yaml.dump(strategy))

    record = {**ctx, **hypothesis, "version": new_version,
              "trades_analyzed": total_trades,
              "stats": _compute_trade_stats(trades) if trades else {}}
    hypotheses_path.open("a").write(json.dumps(record) + "\n")
    sys.stdout.write(
        f"[PERSIST] version={new_version} hypothesis={hypothesis['variable']} "
        f"written to: {hypotheses_path} ({hypotheses_path.stat().st_size} bytes)\n"
    )
    sys.stdout.flush()
    return new_version