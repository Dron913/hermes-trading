"""Exit Intelligence persistence layer — manages all exit_intelligence state files."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


TRACKED_ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]


class ExitIntelligenceStore:
    """Manages exit_intelligence.jsonl, phase_state.json, asset_exit_stats.json,
    recommendations.jsonl, shadow_trades.jsonl, proposals.jsonl."""

    def __init__(self, root: Path):
        self.root = root
        self.ei_path = root / "exit_intelligence.jsonl"
        self.phase_path = root / "phase_state.json"
        self.asset_stats_path = root / "asset_exit_stats.json"
        self.recs_path = root / "recommendations.jsonl"
        self.shadows_path = root / "shadow_trades.jsonl"
        self.proposals_path = root / "proposals.jsonl"

        self._ensure_initialized()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _ensure_initialized(self):
        """Create default files if missing."""
        if not self.phase_path.exists():
            default_phase = self._default_phase_state()
            self.phase_path.write_text(json.dumps(default_phase, indent=2))

        if not self.asset_stats_path.exists():
            default_asset = {asset: self._default_asset_stats(asset) for asset in TRACKED_ASSETS}
            self.asset_stats_path.write_text(json.dumps(default_asset, indent=2))

    def _default_phase_state(self) -> dict:
        return {
            "current_phase": 1,
            "phase_4_locked": True,
            "phase_4_locked_by_user": True,
            "automatic_progression_enabled": True,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "phase1": {
                "total_trades_analyzed": 0,
                "total_observations_generated": 0,
                "observation_types_seen": [],
                "avg_exit_quality_score": 0.0,
                "stable_exit_quality": False,
                "min_observations_required": 30,
                "to_phase_2": {
                    "trades_required": 50,
                    "trades_met": False,
                    "quality_required": 0.55,
                    "quality_met": False,
                    "current_quality": 0.0,
                    "observations_required": 30,
                    "observations_met": False,
                },
            },
            "phase2": {
                "recommendations_made": 0,
                "recommendations_accurate": 0,
                "recommendations_inaccurate": 0,
                "accuracy_rate": 0.0,
                "recommendations_required": 50,
                "accuracy_required": 0.60,
                "assets_with_recommendations_required": 2,
                "assets_with_recommendations": [],
                "to_phase_3": {
                    "rec_count_met": False,
                    "accuracy_met": False,
                    "multi_asset_met": False,
                },
            },
            "phase3": {
                "shadow_trades": 0,
                "shadow_trades_evaluated": 0,
                "shadow_wins": 0,
                "shadow_loss": 0,
                "actual_wins": 0,
                "actual_losses": 0,
                "shadow_vs_actual_pnl_delta": 0.0,
                "shadow_avg_pnl": 0.0,
                "actual_avg_pnl": 0.0,
                "recommendations_required": 50,
                "accuracy_required": 0.60,
                "assets_required": 3,
                "assets_proven": [],
                "to_phase_4": {
                    "rec_count_met": False,
                    "accuracy_met": False,
                    "shadow_improvement_met": False,
                    "multi_asset_met": False,
                },
            },
            "phase4": {
                "approved": False,
                "approved_at": None,
                "approved_by": None,
            },
        }

    def _default_asset_stats(self, asset: str) -> dict:
        return {
            "asset": asset,
            "trades_analyzed": 0,
            "avg_exit_quality_score": 0.0,
            "avg_mfe": 0.0,
            "avg_mae": 0.0,
            "win_rate": 0.0,
            "avg_profit_if_held": 0.0,
            "observations": [],
            "observation_counts": {},
            "recommendation_count": 0,
            "recommendation_accurate": 0,
            "recommendation_inaccurate": 0,
            "recommendation_accuracy": 0.0,
            "shadow_trades": 0,
            "shadow_pnl_delta": 0.0,
            "shadow_vs_actual_delta": 0.0,
        }

    # ------------------------------------------------------------------
    # Phase state
    # ------------------------------------------------------------------

    def get_phase_state(self) -> dict:
        return json.loads(self.phase_path.read_text())

    def update_phase_state(self, updates: dict) -> None:
        """Merge updates into phase_state.json atomically."""
        state = self.get_phase_state()
        state.update(updates)
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.phase_path.write_text(json.dumps(state, indent=2))

    def update_phase1_counters(self, record: dict) -> None:
        """Increment Phase 1 counters after a new exit record is written."""
        state = self.get_phase_state()
        p1 = state["phase1"]
        p1["total_trades_analyzed"] += 1
        p1["total_observations_generated"] += len(record.get("observations", []))

        # Track unique observation types
        for obs in record.get("observations", []):
            t = obs.get("type", "")
            if t and t not in p1["observation_types_seen"]:
                p1["observation_types_seen"].append(t)

        # Running average exit quality
        old_avg = p1["avg_exit_quality_score"]
        n = p1["total_trades_analyzed"]
        new_score = record.get("exit_quality_score", 0.5)
        p1["avg_exit_quality_score"] = round((old_avg * (n - 1) + new_score) / n, 4)

        # Check stable quality (is avg quality now >= threshold AND at least 10 trades)
        p1["stable_exit_quality"] = (
            p1["avg_exit_quality_score"] >= 0.55 and p1["total_trades_analyzed"] >= 10
        )

        # Update Phase 1 -> Phase 2 gate status
        to2 = p1["to_phase_2"]
        to2["trades_met"] = p1["total_trades_analyzed"] >= 50
        to2["quality_met"] = p1["stable_exit_quality"]
        to2["current_quality"] = p1["avg_exit_quality_score"]
        to2["observations_met"] = (
            p1["total_observations_generated"] >= 30
            and len(p1["observation_types_seen"]) >= 2
        )

        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.phase_path.write_text(json.dumps(state, indent=2))

    def update_recommendation_accuracy(self, asset: str, accurate: Optional[bool]) -> None:
        """Update Phase 2 recommendation accuracy stats and check Phase 2->3 gates."""
        state = self.get_phase_state()
        p2 = state["phase2"]

        if len(p2["assets_with_recommendations"]) == 0:
            for a in TRACKED_ASSETS:
                p2.setdefault("_per_asset_recs", {}).setdefault(a, {"made": 0, "accurate": 0, "inaccurate": 0})

        per = p2.setdefault("_per_asset_recs", {})
        per.setdefault(asset, {"made": 0, "accurate": 0, "inaccurate": 0})
        per[asset]["made"] += 1

        if accurate is True:
            p2["recommendations_accurate"] += 1
            per[asset]["accurate"] += 1
        elif accurate is False:
            p2["recommendations_inaccurate"] += 1
            per[asset]["inaccurate"] += 1
        # None = inconclusive, skip

        total_evaluated = p2["recommendations_accurate"] + p2["recommendations_inaccurate"]
        if total_evaluated > 0:
            p2["accuracy_rate"] = round(
                p2["recommendations_accurate"] / total_evaluated, 4
            )

        # Track which assets have recommendations
        for a, stats in per.items():
            if stats["made"] > 0 and a not in p2["assets_with_recommendations"]:
                p2["assets_with_recommendations"].append(a)

        # Check Phase 2->3 gates
        to3 = p2["to_phase_3"]
        total_recs = p2["recommendations_made"]
        to3["rec_count_met"] = total_recs >= 50
        to3["accuracy_met"] = p2["accuracy_rate"] >= 0.60
        to3["multi_asset_met"] = len(p2["assets_with_recommendations"]) >= 2

        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.phase_path.write_text(json.dumps(state, indent=2))

    def update_phase3_performance(self, shadow_record: dict) -> None:
        """Update Phase 3 shadow vs actual stats after a shadow trade resolves."""
        state = self.get_phase_state()
        p3 = state["phase3"]
        p3["shadow_trades"] += 1

        delta = shadow_record.get("pnl_delta")
        shadow_pnl = shadow_record.get("shadow_pnl_pct", 0)
        actual_pnl = shadow_record.get("actual_pnl_pct", 0)

        if delta is not None:
            p3["shadow_vs_actual_pnl_delta"] = round(
                (p3["shadow_vs_actual_pnl_delta"] * (p3["shadow_trades"] - 1) + delta)
                / p3["shadow_trades"], 4
            )
            p3["shadow_trades_evaluated"] += 1

            # Track win rates
            if shadow_pnl > 0:
                p3["shadow_wins"] += 1
            else:
                p3["shadow_loss"] += 1

            if actual_pnl > 0:
                p3["actual_wins"] += 1
            else:
                p3["actual_losses"] += 1

            total = p3["shadow_trades_evaluated"]
            if total > 0:
                p3["shadow_win_rate"] = round(p3["shadow_wins"] / total * 100, 1)
                p3["actual_win_rate"] = round(p3["actual_wins"] / total * 100, 1)

            # Running avg PnL
            p3["shadow_avg_pnl"] = round(
                (p3["shadow_avg_pnl"] * (total - 1) + shadow_pnl) / total, 4
            )
            p3["actual_avg_pnl"] = round(
                (p3["actual_avg_pnl"] * (total - 1) + actual_pnl) / total, 4
            )

        # Track per-asset shadow performance for multi-asset gate
        asset = shadow_record.get("asset", "")
        asset_delta_key = f"_asset_shadow_delta"
        asset_map = p3.setdefault("_asset_deltas", {})
        ad = asset_map.setdefault(asset, {"count": 0, "total_delta": 0})
        if delta is not None:
            ad["count"] += 1
            ad["total_delta"] += delta
            if ad["count"] >= 5 and (ad["total_delta"] / ad["count"]) > 0:
                if asset not in p3["assets_proven"]:
                    p3["assets_proven"].append(asset)

        # Check Phase 3→4 gates
        to4 = p3["to_phase_4"]
        to4["rec_count_met"] = p3["shadow_trades_evaluated"] >= 20
        to4["accuracy_met"] = p3["shadow_win_rate"] >= p3["actual_win_rate"] + 5
        to4["shadow_improvement_met"] = (
            p3["shadow_win_rate"] - p3["actual_win_rate"] >= 5.0
            and p3["shadow_vs_actual_pnl_delta"] >= 0.5
        )
        to4["multi_asset_met"] = len(p3["assets_proven"]) >= 3

        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.phase_path.write_text(json.dumps(state, indent=2))

    # ------------------------------------------------------------------
    # Exit Intelligence records
    # ------------------------------------------------------------------

    def append_exit_record(self, record: dict) -> None:
        self.ei_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ei_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_recent_exit_records(self, n: int = 50) -> List[dict]:
        if not self.ei_path.exists():
            return []
        lines = self.ei_path.read_text(encoding="utf-8").strip().splitlines()
        records = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return records

    def load_exit_records_by_asset(self, asset: str, n: int = 50) -> List[dict]:
        if not self.ei_path.exists():
            return []
        lines = self.ei_path.read_text(encoding="utf-8").strip().splitlines()
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("asset") == asset:
                    records.append(r)
            except json.JSONDecodeError:
                pass
        return records[-n:]

    def load_all_trade_ids(self) -> set:
        """Return set of trade_ids already in exit_intelligence.jsonl."""
        if not self.ei_path.exists():
            return set()
        ids = set()
        for line in self.ei_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line).get("trade_id", ""))
            except json.JSONDecodeError:
                pass
        return ids

    # ------------------------------------------------------------------
    # Asset stats
    # ------------------------------------------------------------------

    def get_asset_stats(self) -> dict:
        return json.loads(self.asset_stats_path.read_text())

    def update_asset_stats(self, asset: str, record: dict) -> None:
        all_stats = self.get_asset_stats()
        if asset not in all_stats:
            all_stats[asset] = self._default_asset_stats(asset)

        s = all_stats[asset]
        s["trades_analyzed"] += 1

        # Running avg quality
        old_q = s["avg_exit_quality_score"]
        n = s["trades_analyzed"]
        new_q = record.get("exit_quality_score", 0.5)
        s["avg_exit_quality_score"] = round((old_q * (n - 1) + new_q) / n, 4)

        # Running avg MFE/MAE
        old_mfe = s["avg_mfe"]
        s["avg_mfe"] = round((old_mfe * (n - 1) + record.get("mfe_pct", 0)) / n, 4)
        old_mae = s["avg_mae"]
        s["avg_mae"] = round((old_mae * (n - 1) + record.get("mae_pct", 0)) / n, 4)

        # Win rate
        if record.get("pnl_pct", 0) > 0:
            wins = int(s.get("_wins", 0)) + 1
            s["_wins"] = wins
        else:
            s["_wins"] = s.get("_wins", 0)
        s["win_rate"] = round(s.get("_wins", 0) / n * 100, 1)

        # Avg profit if held
        old_pifh = s["avg_profit_if_held"]
        s["avg_profit_if_held"] = round(
            (old_pifh * (n - 1) + record.get("profit_if_held_pct", 0)) / n, 4
        )

        # Track observation types
        for obs in record.get("observations", []):
            t = obs.get("type", "")
            if t:
                s["observation_counts"][t] = s["observation_counts"].get(t, 0) + 1
                if t not in s["observations"]:
                    s["observations"].append(t)

        self.asset_stats_path.write_text(json.dumps(all_stats, indent=2))

    def update_asset_recommendation_stats(self, asset: str, rec_type: str, accurate: Optional[bool]) -> None:
        all_stats = self.get_asset_stats()
        s = all_stats.setdefault(asset, self._default_asset_stats(asset))
        s["recommendation_count"] += 1
        if accurate is True:
            s["recommendation_accurate"] += 1
        elif accurate is False:
            s["recommendation_inaccurate"] += 1

        total = s["recommendation_accurate"] + s["recommendation_inaccurate"]
        if total > 0:
            s["recommendation_accuracy"] = round(
                s["recommendation_accurate"] / total, 4
            )
        self.asset_stats_path.write_text(json.dumps(all_stats, indent=2))

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def append_recommendation(self, rec: dict) -> None:
        self.recs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.recs_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # Increment recommendations_made counter
        state = self.get_phase_state()
        if state.get("current_phase", 1) >= 2:
            state["phase2"]["recommendations_made"] += 1
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            self.phase_path.write_text(json.dumps(state, indent=2))

    def load_recent_recommendations(self, n: int = 50) -> List[dict]:
        if not self.recs_path.exists():
            return []
        lines = self.recs_path.read_text(encoding="utf-8").strip().splitlines()
        recs = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return recs

    def load_recommendations_by_asset(self, asset: str, n: int = 50) -> List[dict]:
        if not self.recs_path.exists():
            return []
        lines = self.recs_path.read_text(encoding="utf-8").strip().splitlines()
        recs = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("asset") == asset:
                    recs.append(r)
            except json.JSONDecodeError:
                pass
        return recs[-n:]

    # ------------------------------------------------------------------
    # Shadow trades
    # ------------------------------------------------------------------

    def append_shadow_trade(self, shadow: dict) -> None:
        self.shadows_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.shadows_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(shadow, ensure_ascii=False) + "\n")

    def update_shadow_trade(self, updated_shadow: dict) -> None:
        """Update a shadow trade entry in place (for when actual outcome is known)."""
        if not self.shadows_path.exists():
            return
        lines = self.shadows_path.read_text(encoding="utf-8").strip().splitlines()
        found = False
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("trade_id") == updated_shadow.get("trade_id"):
                    lines[i] = json.dumps(updated_shadow, ensure_ascii=False)
                    found = True
                    break
            except json.JSONDecodeError:
                pass
        if found:
            self.shadows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def load_open_shadows(self, asset: str, entry_time: str) -> List[dict]:
        """Load unresolved shadow trades for an open position."""
        if not self.shadows_path.exists():
            return []
        shadows = []
        for line in self.shadows_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("actual_pnl_pct") is None and r.get("asset") == asset:
                    shadows.append(r)
            except json.JSONDecodeError:
                pass
        return shadows

    def load_shadow_performance_summary(self) -> dict:
        """Aggregate shadow vs actual performance."""
        if not self.shadows_path.exists():
            return {"shadow_trades_evaluated": 0, "meets_phase4_threshold": False}

        shadows = []
        for line in self.shadows_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("actual_pnl_pct") is not None:
                    shadows.append(r)
            except json.JSONDecodeError:
                pass

        if not shadows:
            return {"shadow_trades_evaluated": 0, "meets_phase4_threshold": False}

        sw = sum(1 for s in shadows if (s.get("shadow_pnl_pct") or 0) > 0)
        aw = sum(1 for s in shadows if (s.get("actual_pnl_pct") or 0) > 0)
        n = len(shadows)
        total_delta = sum(s.get("pnl_delta") or 0 for s in shadows)
        shadow_pnl = sum(s.get("shadow_pnl_pct") or 0 for s in shadows)
        actual_pnl = sum(s.get("actual_pnl_pct") or 0 for s in shadows)

        wins_imp = round(sw / n * 100 - aw / n * 100, 1)
        state = self.get_phase_state()
        p3 = state.get("phase3", {})
        meets = (
            wins_imp >= 5.0
            and total_delta / n >= 0.5
            and len(p3.get("assets_proven", [])) >= 3
        )

        # Per-asset deltas
        asset_deltas = {}
        for s in shadows:
            a = s.get("asset", "?")
            asset_deltas.setdefault(a, []).append(s.get("pnl_delta") or 0)

        multi = {a: f"+{round(sum(v)/len(v), 2)}%" for a, v in asset_deltas.items() if v}

        return {
            "shadow_trades_evaluated": n,
            "shadow_win_rate": round(sw / n * 100, 1),
            "actual_win_rate": round(aw / n * 100, 1),
            "win_rate_improvement": wins_imp,
            "avg_pnl_delta": round(total_delta / n, 4),
            "shadow_avg_pnl": round(shadow_pnl / n, 4),
            "actual_avg_pnl": round(actual_pnl / n, 4),
            "multi_asset": multi,
            "meets_phase4_threshold": meets,
        }

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------

    def append_proposal(self, proposal: dict) -> None:
        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.proposals_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(proposal, ensure_ascii=False) + "\n")

    def load_active_proposal(self) -> Optional[dict]:
        """Return the most recent unapproved proposal."""
        if not self.proposals_path.exists():
            return None
        for line in reversed(self.proposals_path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                if not p.get("approved"):
                    return p
            except json.JSONDecodeError:
                pass
        return None

    def approve_proposal(self, proposal_id: str) -> None:
        """Mark a proposal as approved."""
        if not self.proposals_path.exists():
            return
        lines = self.proposals_path.read_text(encoding="utf-8").strip().splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                p = json.loads(line)
                if p.get("proposal_id") == proposal_id:
                    p["approved"] = True
                    p["approved_at"] = datetime.now(timezone.utc).isoformat()
                    lines[i] = json.dumps(p)
            except json.JSONDecodeError:
                pass
        self.proposals_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Mark phase 4 as approved
        state = self.get_phase_state()
        state["phase4"]["approved"] = True
        state["phase4"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.phase_path.write_text(json.dumps(state, indent=2))