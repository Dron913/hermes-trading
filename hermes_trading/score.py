"""Score a list of trades against the goal. Returns float in [-1, +1]."""
import json
import math
from pathlib import Path
from typing import List

import yaml


def compute_sharpe(trades: List[dict]) -> float:
    if len(trades) < 2:
        return 0.0
    pnls = [t["pnl_pct"] / 100 for t in trades]
    mean = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
    if std == 0:
        return 0.0
    # Annualize (assume ~60 trades/30d)
    return (mean / std) * math.sqrt(365) if std else 0.0


def max_drawdown(trades: List[dict]) -> float:
    if not trades:
        return 0.0
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


def score_trades(trades: List[dict]) -> float:
    if not trades:
        return 0.0

    goal_path = Path(__file__).parent.parent / "state" / "goal.yaml"
    goal = yaml.safe_load(goal_path.read_text())

    realized_return = sum(t["pnl_pct"] for t in trades)
    target_return = goal["target_return_30d"] * 100
    max_dd = max_drawdown(trades)

    realized_sharpe = compute_sharpe(trades)

    # Component scores [-1, +1]
    return_component = max(-1, min(1, realized_return / target_return)) if target_return else 0
    dd_component = max(-1, min(1, 1 - (max_dd / (goal["max_drawdown"] * 100)))) if max_dd else 1
    sharpe_component = max(-1, min(1, realized_sharpe / goal["min_sharpe"])) if realized_sharpe else 0

    # Composite
    composite = (return_component * 0.4) + (dd_component * 0.4) + (sharpe_component * 0.2)

    if composite < goal["failure_below"]:
        return -1.0
    return max(-1.0, min(1.0, composite))