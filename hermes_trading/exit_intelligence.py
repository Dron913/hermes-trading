"""Exit Intelligence — 4-phase graduated exit learning system.

Phases:
  1 — Observer: Score exits, generate observations (Hermes NIM + deterministic)
  2 — Advisor: Generate exit timing recommendations for open positions
  3 — Shadow: Simulate virtual exits vs actual outcomes
  4 — Authority: Generate exit-modification proposals (requires human approval)
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .exit_intelligence_store import ExitIntelligenceStore, TRACKED_ASSETS


# ------------------------------------------------------------------
# Deterministic exit observation patterns
# ------------------------------------------------------------------

def generate_deterministic_observations(trade: dict) -> List[dict]:
    """Generate exit observations using rules only — no AI needed.

    New trades can trigger many more observation types thanks to:
    - Specific exit_reason values (take_profit, stop_loss, early_exit_1h_rsi)
    - MFE/MAE tracked at the final tick before exit
    - Quality scoring context (ceiling capture ratios)
    """
    observations = []
    mfe = trade.get("mfe_pct", 0.0)
    mae = trade.get("mae_pct", 0.0)
    pnl = trade.get("pnl_pct", 0.0)
    dur_sec = trade.get("duration_sec", 0)
    exit_reason = trade.get("exit_reason", "unknown")
    asset = trade.get("asset", "?")
    is_win = pnl >= 0

    # 1. Take profit hit — clean, profitable exit at the target
    # Fires for all trades that hit TP (mfe may or may not be available)
    if exit_reason == "take_profit":
        observations.append({
            "type": "take_profit_hit",
            "detail": f"{asset} TP triggered — exit at {pnl:+.2f}%, "
                      f"MFE={mfe:+.2f}% ({round(pnl/mfe*100) if mfe > 0 else 'n/a'}% of ceiling)",
            "confidence": 0.80,
        })
        return observations  # TP exits don't need further analysis

    # 2. Stop loss was appropriate — saved from worse loss
    if exit_reason == "stop_loss":
        mae_ratio = abs(mae) / abs(pnl) if pnl != 0 else 0
        if mae_ratio < 0.8:  # MAE shows we were heading for worse
            observations.append({
                "type": "stop_loss_appropriate",
                "detail": f"{asset} SL saved from larger loss — MAE {mae:+.2f}% vs "
                          f"stopped at {pnl:+.2f}%",
                "confidence": 0.72,
            })
        elif mae_ratio > 1.2 and is_win:  # MAE proves we recovered after stop
            observations.append({
                "type": "stop_loss_unnecessary",
                "detail": f"{asset} stopped at {pnl:+.2f}% but recovered — MAE {mae:+.2f}% "
                          f"(stop was premature)",
                "confidence": 0.65,
            })
        else:
            observations.append({
                "type": "stop_loss",
                "detail": f"{asset} stopped out at {pnl:+.2f}%, MAE {mae:+.2f}%",
                "confidence": 0.55,
            })
        return observations  # stop_loss exits done

    # 3. Early exit (RSI signal) on a winning trade — trend may have continued
    if exit_reason == "early_exit_1h_rsi":
        if is_win:
            observations.append({
                "type": "trend_exhausted",
                "detail": f"{asset} exited via RSI signal at {pnl:+.2f}% — "
                          f"trend may have continued",
                "confidence": 0.62,
            })
        else:
            observations.append({
                "type": "early_exit_loss",
                "detail": f"{asset} exited via RSI signal at {pnl:+.2f}% — "
                          f"loss suggests exit was appropriate",
                "confidence": 0.58,
            })
        return observations

    # --- Generic exit (no specific signal matched) ---
    # For these, MFE is the primary differentiator

    # 4. Significant ceiling missed (>75% of ceiling lost)
    if mfe > 0 and pnl < mfe * 0.25:
        pct_cap = round(pnl / mfe * 100, 1) if mfe > 0 else 0
        observations.append({
            "type": "exited_too_early",
            "detail": f"{asset}: Ceiling {mfe:+.2f}% vs actual {pnl:+.2f}% — "
                      f"only {pct_cap}% of MFE captured",
            "confidence": min(round((mfe - pnl) / mfe, 2) if mfe > 0 else 0.5, 0.95),
        })

    # 5. Momentum still existed (short duration, profitable, room left)
    if dur_sec < 7200 and is_win and mfe > 0 and mfe > pnl * 1.2:  # < 2h and room left
        observations.append({
            "type": "momentum_still_existed",
            "detail": f"{asset} exited in {dur_sec // 3600}h{dur_sec % 3600 // 60}m — "
                      f"profit {pnl:+.2f}% but {mfe - pnl:+.2f}% additional was available",
            "confidence": 0.68,
        })

    # 6. Good ceiling capture but not TP (50-85% of MFE) — solid exit
    if mfe > 0 and pnl >= mfe * 0.50 and pnl < mfe * 0.85 and is_win:
        observations.append({
            "type": "good_ceiling_capture",
            "detail": f"{asset} captured {round(pnl/mfe*100)}% of {mfe:+.2f}% ceiling — "
                      f"exit at {pnl:+.2f}%",
            "confidence": 0.70,
        })

    # 7. Loss exit that caught good MFE before reversal
    if mfe > 0 and not is_win and mfe > abs(pnl):  # had profit but gave it back
        observations.append({
            "type": "gave_back_profit",
            "detail": f"{asset} MFE was {mfe:+.2f}% but closed at {pnl:+.2f}% — "
                      f"gave back {mfe - abs(pnl):+.2f}%",
            "confidence": 0.65,
        })

    # 8. Long hold, decent capture (24h+ but captured <60% of MFE)
    if dur_sec > 86400 and mfe > 0 and pnl < mfe * 0.6:  # > 24h
        observations.append({
            "type": "ceiling_missed",
            "detail": f"{asset} held {dur_sec // 3600}h captured "
                      f"{round(pnl/mfe*100)}% of MFE {mfe:+.2f}%",
            "confidence": 0.55,
        })

    # 9. Generic exit with no MFE — truly unknown quality
    if mfe == 0:
        observations.append({
            "type": "exited_correctly",
            "detail": f"{asset} exited at {pnl:+.2f}%, duration={dur_sec}s — "
                      f"no excursion data available",
            "confidence": 0.50,
        })

    # Fallback: exit with MFE but no other condition matched
    if not observations:
        observations.append({
            "type": "exited_correctly",
            "detail": f"{asset} exit: MFE={mfe:+.2f}%, MAE={mae:+.2f}%, "
                      f"PnL={pnl:+.2f}%, dur={dur_sec}s",
            "confidence": 0.55,
        })

    return observations


# ------------------------------------------------------------------
# Exit quality scoring
# ------------------------------------------------------------------

def exit_quality_score(trade: dict) -> float:
    """Deterministic exit quality score 0.0–1.0.

    Principles:
    - Exit reason is a strong signal even when MFE is 0.
    - 'take_profit' means we captured the ceiling — score high regardless.
    - 'stop_loss' quality depends on whether the loss was necessary.
    - 'early_exit_1h_rsi' quality depends on whether the trade was winning.
    - Generic 'exit' with mfe=0 is genuinely unknown — score at baseline.
    - With MFE available, ceiling capture ratio drives the score.
    """
    mfe = trade.get("mfe_pct", 0.0)
    mae = trade.get("mae_pct", 0.0)
    pnl = trade.get("pnl_pct", 0.0)
    exit_reason = trade.get("exit_reason", "unknown")

    # MFE-aware scoring (strongest signal when we have it)
    if mfe > 0 and pnl >= mfe * 0.85:
        return 0.90
    if mfe > 0 and pnl >= mfe * 0.50:
        return 0.75
    if mfe > 0 and pnl < mfe * 0.25:
        return 0.40
    # Default when mfe > 0 but no other match applies
    if mfe > 0:
        return 0.70

    # MFE unavailable (mfe == 0). Score based on known exit reason.
    if exit_reason == "take_profit":
        # We know we captured the ceiling target — high quality even without MFE
        return 0.65
    if exit_reason == "stop_loss":
        # MAE for stop-loss: abs(mae) > 0 means we tracked excursion; use it
        if abs(mae) > 0 and abs(mae) < abs(pnl):
            return 0.55  # MAE confirms loss was smaller than actual — stopped in time
        if abs(mae) > 0 and abs(mae) > abs(pnl) * 1.2:
            return 0.30  # MAE confirms we stopped unnecessarily
        return 0.50     # MAE unavailable — give benefit of the doubt
    if exit_reason == "early_exit_1h_rsi" and pnl >= 0:
        return 0.55
    if exit_reason == "early_exit_1h_rsi" and pnl < 0:
        return 0.45
    if exit_reason == "exit":
        # Generic exit with no MFE — genuinely unknown quality
        return 0.50
    return 0.50  # any other reason — default to baseline


# ------------------------------------------------------------------
# Hermes NIM integration (reuses reflect.py helpers)
# ------------------------------------------------------------------

def _extract_json_objects(text: str) -> list:
    """Bracket-depth JSON extractor — copied from reflect.py for standalone use."""
    results = []
    depth = 0
    in_string = False
    escape_next = False
    obj_start = -1

    for i, c in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            if depth == 0:
                obj_start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and obj_start != -1:
                chunk = text[obj_start:i + 1]
                if chunk.strip():
                    results.append(chunk)
                obj_start = -1
    return results


async def call_hermes_exit_insights(trade: dict, phase: int) -> List[dict]:
    """Generate qualitative exit observations via GPT-OSS-120B via NIM."""
    import urllib.request as _ur

    api_key = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY") or ""
    if not api_key:
        return []

    system_prompt = (
        "You are Exit Intelligence Analyst for a crypto trading system.\n"
        "Analyze this trade and provide exactly 2 qualitative observations about exit quality.\n"
        "Observation types: exited_too_early, exited_too_late, exited_correctly, "
        "momentum_still_existed, trend_exhausted, stop_loss_appropriate, "
        "stop_loss_unnecessary, ceiling_missed.\n\n"
        'Respond ONLY with a valid JSON object: '
        '{"observations": [{"type": "...", "detail": "...", "confidence": 0.0-1.0}]}'
    )

    phase_context = {
        1: "Phase 1 Observer: Provide only exit quality observations. Focus on whether the exit was early, late, or optimal.",
        2: "Phase 2 Advisor: Also briefly assess current position status and suggest whether to hold or exit any active position mentioned.",
        3: "Phase 3 Shadow: If position data is provided, estimate the optimal exit point and expected PnL at that exit.",
        4: "Phase 4 Authority: Generate a specific exit modification proposal if warranted.",
    }
    phase_instruction = phase_context.get(phase, phase_context[1])

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{phase_instruction}\n\nTrade data:\n{json.dumps(trade)}"},
        ],
        "max_tokens": 400,
        "temperature": 0.3,
    }

    try:
        req = _ur.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read())

        raw = result["choices"][0]["message"]["content"].strip()
        for txt in (_extract_json_objects(raw), [raw]):
            for obj in txt:
                try:
                    data = json.loads(obj)
                    obs = data.get("observations") or []
                    if obs:
                        return obs
                except (json.JSONDecodeError, KeyError):
                    pass
    except Exception as e:
        print(f"[EXIT INTEL] Hermes NIM call failed: {e}")

    return []


def build_exit_intelligence_prompt(
    trades: List[dict],
    phase: int,
    open_positions: Optional[List[dict]] = None,
) -> str:
    """Build a prompt for batch exit intelligence analysis (Phase 2+)."""
    recs_text = ""
    if open_positions:
        pos_lines = []
        for pos in open_positions:
            dur = int(time.time() - datetime.fromisoformat(
                pos.get("entry_time", datetime.now(timezone.utc).isoformat())
            ).timestamp())
            dh, dm = dur // 3600, (dur % 3600) // 60
            mfe = pos.get("_mfe", 0.0)
            mae = pos.get("_mae", 0.0)
            entries = pos.get("indicators", {})
            rsi_1h = entries.get("rsi_1h", "?")
            pos_lines.append(
                f"  - {pos.get('asset','?')} {pos.get('side')} "
                f"entry={pos.get('entry_price')} dur={dh}h{dm}m "
                f"mfe={mfe:+.2f}% mae={mae:+.2f}% rsi_1h={rsi_1h}"
            )
        recs_text = "\n\n== OPEN POSITIONS ==\n" + "\n".join(pos_lines)
    elif trades:
        # Single trade context
        t = trades[-1]
        recs_text = f"\n\n== THIS TRADE ==\n  Asset: {t.get('asset')} {t.get('side')} | " \
                    f"PnL={t.get('pnl_pct'):+.2f}% MFE={t.get('mfe_pct'):+.2f}% " \
                    f"MAE={t.get('mae_pct'):+.2f}%dur={t.get('duration_sec',0)//60}m"

    phase_asks = {
        1: "Provide 2 qualitative exit observations for this trade.",
        2: "Analyze open positions and provide exit recommendations. For each: type, detail, confidence.",
        3: "For each open position, provide a predicted optimal exit point and expected PnL — this is a SHADOW trade, no real action taken.",
        4: "Generate exit modification proposals for open positions. Each proposal: type, detail, confidence, expected_pnl_delta.",
    }
    ask = phase_asks.get(phase, phase_asks[1])

    return f"""Exit Intelligence Analysis — Phase {phase}

{recs_text}

== TASK ==
{ask}

Respond only with a valid JSON object."""


# ------------------------------------------------------------------
# Core analyzer classes
# ------------------------------------------------------------------

class ExitIntelligenceAnalyzer:
    """Main analyzer — call on_trade_closed() for each closed trade."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = ExitIntelligenceStore(self.root)
        self._backfill_done = False

    # ------------------------------------------------------------------
    # Per-trade entry point
    # ------------------------------------------------------------------

    async def on_trade_closed(self, trade: dict) -> dict:
        """Run full exit intelligence pipeline for one closed trade."""
        phase = self.store.get_phase_state().get("current_phase", 1)
        trade_id = f"{trade['asset']}_{trade.get('timestamp', '')}"

        # 1. Score exit quality
        score = exit_quality_score(trade)

        # 2. Generate observations (Hermes NIM + deterministic)
        observations = generate_deterministic_observations(trade)
        if phase >= 1:
            hermes_obs = await call_hermes_exit_insights(trade, phase)
            # Merge Hermes obs with deterministic, dedupe by type
            seen_types = {o["type"] for o in observations}
            for obs in hermes_obs:
                if obs.get("type") not in seen_types:
                    observations.append(obs)
                    seen_types.add(obs.get("type"))

        # 3. Build record
        record = {
            "trade_id": trade_id,
            "asset": trade.get("asset"),
            "side": trade.get("side"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "pnl_pct": round(trade.get("pnl_pct", 0), 4),
            "exit_reason": trade.get("exit_reason", "unknown"),
            "duration_sec": trade.get("duration_sec", 0),
            "exit_quality_score": round(score, 4),
            "mfe_pct": round(trade.get("mfe_pct", 0.0), 4),
            "mae_pct": round(trade.get("mae_pct", 0.0), 4),
            "profit_if_held_pct": round(trade.get("profit_if_held_pct", 0.0), 4),
            "loss_avoided_pct": round(trade.get("loss_avoided_pct", 0.0), 4),
            "timestamp": trade.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "strategy_version": trade.get("strategy_version"),
            "is_backfill": False,
            "observations": observations,
        }

        # 4. Persist
        self.store.append_exit_record(record)
        self.store.update_asset_stats(trade["asset"], record)
        self.store.update_phase1_counters(record)

        # 5. Track Phase 2 recommendation outcomes for this trade
        if phase >= 2:
            self._track_open_recommendations(trade, phase)

        # 6. Track Phase 3 shadow outcomes for this trade
        if phase >= 3 and trade.get("asset") and trade.get("timestamp"):
            self._resolve_shadow_trades(trade)

        # 7. Phase progression
        self.check_phase_progression()

        return record

    # ------------------------------------------------------------------
    # Phase progression
    # ------------------------------------------------------------------

    def check_phase_progression(self) -> None:
        """Check and execute phase gates. Phase 4 requires human approval."""
        state = self.store.get_phase_state()
        current = state.get("current_phase", 1)

        if current == 1:
            to2 = state.get("phase1", {}).get("to_phase_2", {})
            if all([
                to2.get("trades_met", False),
                to2.get("quality_met", False),
                to2.get("observations_met", False),
            ]):
                self._advance_phase(1, 2)
                print("[EXIT INTEL] Phase 1 -> Phase 2 transition triggered")

        elif current == 2:
            to3 = state.get("phase2", {}).get("to_phase_3", {})
            if all([
                to3.get("rec_count_met", False),
                to3.get("accuracy_met", False),
                to3.get("multi_asset_met", False),
            ]):
                self._advance_phase(2, 3)
                print("[EXIT INTEL] Phase 2 -> Phase 3 transition triggered")

        elif current == 3:
            # Phase 4 never auto-advances
            to4 = state.get("phase3", {}).get("to_phase_4", {})
            if all([
                to4.get("rec_count_met", False),
                to4.get("accuracy_met", False),
                to4.get("shadow_improvement_met", False),
                to4.get("multi_asset_met", False),
            ]):
                proposal = self._generate_phase4_proposal()
                print("[EXIT INTEL] Phase 4 proposal generated — awaiting human approval")

    def _advance_phase(self, from_p: int, to_p: int) -> None:
        state = self.store.get_phase_state()
        state["current_phase"] = to_p
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.store.update_phase_state(state)

    # ------------------------------------------------------------------
    # Phase 2: Recommendation tracking
    # ------------------------------------------------------------------

    def _track_open_recommendations(self, trade: dict, phase: int) -> None:
        """Match the closed trade against active recommendations and update accuracy."""
        asset = trade.get("asset", "")
        actual_pnl = trade.get("pnl_pct", 0)

        recs = []
        if self.store.recs_path.exists():
            for line in self.store.recs_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("asset") == asset and r.get("outcome") is None:
                        recs.append(r)
                except json.JSONDecodeError:
                    pass

        for rec in recs:
            outcome = self._evaluate_recommendation_outcome(rec, trade)
            if outcome is not None:
                rec["outcome"] = outcome
                rec["actual_pnl_pct"] = actual_pnl
                # Rewrite the recs file with the updated record
                self._update_recommendation(rec)
                self.store.update_recommendation_accuracy(asset, outcome)
                self.store.update_asset_recommendation_stats(
                    asset, rec.get("recommendation_type"), outcome
                )

    def _evaluate_recommendation_outcome(self, rec: dict, trade: dict) -> Optional[bool]:
        """Determine if a recommendation was accurate. Returns None if inconclusive."""
        rec_type = rec.get("recommendation_type", "")
        rec_trigger_pct = rec.get("would_have_triggered_pct", 0)
        actual_pnl = trade.get("pnl_pct", 0)
        rec_trigger_price = rec.get("recommendation_price")

        if rec_type == "hold":
            if actual_pnl > 0.3:
                return True
            if actual_pnl < -0.5:
                return False
            return None  # inconclusive

        if rec_type == "early_exit":
            exit_price = trade.get("exit_price", 0)
            if rec_trigger_price and exit_price:
                if rec_trigger_price <= exit_price:
                    return True  # we exited near or above recommended level
                if actual_pnl < rec.get("expected_pnl_pct", 0) - 0.3:
                    return True  # exit was below recommendation
            if actual_pnl <= 0 and rec_trigger_pct > 0:
                return True  # recommendation to exit before loss was correct
            return None

        if rec_type == "partial_profit":
            if actual_pnl > rec_trigger_pct:
                return True
            if actual_pnl < rec_trigger_pct * 0.5:
                return False
            return None

        if rec_type == "tighten_stop":
            # Stop loss was hit — was it the tightened stop or original?
            exit_reason = trade.get("exit_reason", "")
            if exit_reason == "stop_loss":
                return actual_pnl > -0.5  # tightened SL saved us
            return None

        return None

    def _update_recommendation(self, updated_rec: dict) -> None:
        """Rewrite recommendations.jsonl with updated record."""
        if not self.store.recs_path.exists():
            return
        lines = self.store.recs_path.read_text(encoding="utf-8").strip().splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("recommendation_id") == updated_rec.get("recommendation_id"):
                    lines[i] = json.dumps(updated_rec, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        self.store.recs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def evaluate_open_positions(
        self,
        open_positions: dict,
        recent_trades: List[dict],
        asset_stats: dict,
    ) -> List[dict]:
        """Phase 2+: generate recommendations for current open positions."""
        state = self.store.get_phase_state()
        if state.get("current_phase", 1) < 2:
            return []

        recommendations = []
        for asset, pos in open_positions.items():
            rec = self.generate_recommendation(asset, pos, recent_trades, asset_stats)
            if rec:
                recommendations.append(rec)
                self.store.append_recommendation(rec)

        return recommendations

    def generate_recommendation(
        self, asset: str, pos: dict, recent_trades: List[dict], asset_stats: dict
    ) -> Optional[dict]:
        """Generate an exit recommendation for an open position."""
        state = self.store.get_phase_state()
        if state.get("current_phase", 1) < 2:
            return None

        mfe = pos.get("_mfe", 0.0)
        mae = pos.get("_mae", 0.0)
        entry_price = pos.get("entry_price", 0)
        entries = pos.get("indicators", {})
        rsi_1h = entries.get("rsi_1h", 50)
        side = pos.get("side", "?")
        dur_sec = int(
            time.time()
            - datetime.fromisoformat(
                pos.get("entry_time", datetime.now(timezone.utc).isoformat())
            ).timestamp()
        )

        # Simple rule-based recommendation logic
        rec_type = None
        detail = ""
        confidence = 0.5
        trigger_pct = 0.0

        exit_reason = "unknown"
        expected_pnl = 0.0

        # RSI overbought/oversold signals
        if side == "long" and rsi_1h > 70:
            rec_type = "early_exit"
            detail = f"{asset} RSI 1h={rsi_1h:.0f} overbought — historical early exits improve PnL"
            confidence = 0.62
            trigger_pct = mae if mae < 0 else -0.5
            exit_reason = "rsi_overbought"

        elif side == "short" and rsi_1h < 30:
            rec_type = "early_exit"
            detail = f"{asset} RSI 1h={rsi_1h:.0f} oversold — trend reversal risk"
            confidence = 0.62
            trigger_pct = mae if mae < 0 else -0.5
            exit_reason = "rsi_oversold"

        # MFE ceiling approach
        elif mfe > 1.5:
            pct_left = round(mfe * 0.2, 2)
            rec_type = "partial_profit"
            detail = f"{asset} MFE {mfe:+.2f}% approaching ceiling — lock in {pct_left:+.2f}% now, ride remainder"
            confidence = 0.68
            trigger_pct = pct_left
            exit_reason = "partial_profit"
            expected_pnl = mfe * 0.7

        # Long hold, momentum fading
        elif dur_sec > 14400 and side == "long" and rsi_1h < 50:
            rec_type = "tighten_stop"
            detail = f"{asset} held {dur_sec//3600}h with fading RSI ({rsi_1h:.0f}) — tighten stop to lock gains"
            confidence = 0.58
            trigger_pct = mfe * 0.5
            exit_reason = "tighten_stop"

        if rec_type is None:
            return None

        # Check if this recommendation already exists for this position
        existing_id = f"{asset}_{pos.get('entry_time', '')}"
        rec_id = f"rec_{existing_id}_{rec_type}_{int(time.time())}"

        return {
            "recommendation_id": rec_id,
            "asset": asset,
            "side": side,
            "recommendation_type": rec_type,
            "detail": detail,
            "confidence": round(confidence, 3),
            "would_have_triggered_pct": round(trigger_pct, 4),
            "recommendation_price": None,
            "expected_pnl_pct": round(expected_pnl, 4),
            "exit_reason": exit_reason,
            "entry_time": pos.get("entry_time"),
            "mfe_at_recommendation": round(mfe, 4),
            "mae_at_recommendation": round(mae, 4),
            "outrocome": None,  # filled when trade closes
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Phase 3: Shadow exit system
    # ------------------------------------------------------------------

    def evaluate_shadow_exits(
        self,
        open_positions: dict,
        recent_trades: List[dict],
        asset_stats: dict,
    ) -> List[dict]:
        """Phase 3: Generate virtual exit decisions for all open positions."""
        state = self.store.get_phase_state()
        if state.get("current_phase", 1) < 3:
            return []

        shadows = []
        for asset, pos in open_positions.items():
            shadow = self.decide_shadow_exit(asset, pos, recent_trades, asset_stats)
            if shadow:
                shadows.append(shadow)
                self.store.append_shadow_trade(shadow)
        return shadows

    def decide_shadow_exit(
        self,
        asset: str,
        pos: dict,
        recent_trades: List[dict],
        asset_stats: dict,
    ) -> Optional[dict]:
        """Simulate optimal exit decision for an open position (Phase 3 shadow)."""
        mfe = pos.get("_mfe", 0.0)
        mae = pos.get("_mae", 0.0)
        entry_price = pos.get("entry_price", 0)
        entries = pos.get("indicators", {})
        rsi_1h = entries.get("rsi_1h", 50)
        side = pos.get("side", "?")

        # Shadow exit logic: simulate what Hermes would recommend
        shadow_reason = "hold"
        shadow_price = None

        if side == "long":
            if rsi_1h > 75 or (mfe > 0 and mfe > 2.0):
                shadow_reason = "early_exit"
                shadow_pct = mfe * 0.85
                shadow_pnl = round(shadow_pct, 4)
                shadow_price = round(entry_price * (1 + shadow_pct / 100), 6)
            elif mfe > 0:
                shadow_pnl = round(mfe, 4)
                shadow_price = round(entry_price * (1 + mfe / 100), 6)
            else:
                shadow_pnl = 0.0
        elif side == "short":
            if rsi_1h < 25 or (mfe > 0 and mfe > 2.0):
                shadow_reason = "early_exit_shadow"
                shadow_pct = mfe * 0.85
                shadow_pnl = round(shadow_pct, 4)
                shadow_price = round(entry_price * (1 - shadow_pct / 100), 6)
            elif mfe > 0:
                shadow_pnl = round(mfe, 4)
                shadow_price = round(entry_price * (1 - mfe / 100), 6)
            else:
                shadow_pnl = 0.0

        trade_id = f"{asset}_{pos.get('entry_time', '')}"
        return {
            "trade_id": trade_id,
            "asset": asset,
            "side": side,
            "entry_price": entry_price,
            "entry_time": pos.get("entry_time"),
            "shadow_exit_reason": shadow_reason,
            "shadow_exit_price": shadow_price,
            "shadow_pnl_pct": shadow_pnl,
            "actual_exit_reason": None,
            "actual_pnl_pct": None,
            "pnl_delta": None,
            "mfe_at_shadow": round(mfe, 4),
            "mae_at_shadow": round(mae, 4),
            "shadow_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _resolve_shadow_trades(self, trade: dict) -> None:
        """Match a closed trade against its open shadow record and resolve."""
        asset = trade.get("asset", "")
        entry_time = trade.get("timestamp", "")

        shadows = self.store.load_open_shadows(asset, entry_time)
        if not shadows:
            # Try to match by entry_time approximation
            shadows = [s for s in self.store.load_open_shadows(asset, "")
                       if s.get("entry_time", "").startswith(entry_time[:13])]

        for shadow in shadows:
            actual_pnl = trade.get("pnl_pct", 0)
            delta = round(shadow.get("shadow_pnl_pct", 0) - actual_pnl, 4)
            shadow["actual_exit_reason"] = trade.get("exit_reason", "closed")
            shadow["actual_pnl_pct"] = round(actual_pnl, 4)
            shadow["pnl_delta"] = delta
            self.store.update_shadow_trade(shadow)
            self.store.update_phase3_performance(shadow)

    # ------------------------------------------------------------------
    # Phase 4: Proposal generation
    # ------------------------------------------------------------------

    def _generate_phase4_proposal(self) -> dict:
        """Generate Phase 4 proposal with evidence — does NOT auto-activate."""
        summary = self.store.load_shadow_performance_summary()
        state = self.store.get_phase_state()
        p3 = state.get("phase3", {})
        p2 = state.get("phase2", {})

        proposal = {
            "proposal_id": f"phase4_{datetime.now(timezone.utc).isoformat()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": 4,
            "proposal": (
                "Phase 4 Exit Authority Proposal — Hermes requests controlled exit "
                "proposal generation. No automatic execution. All proposals require "
                "human review and approval before any action."
            ),
            "evidence": {
                "shadow_trades_evaluated": summary.get("shadow_trades_evaluated", 0),
                "shadow_win_rate": summary.get("shadow_win_rate", 0),
                "actual_win_rate": summary.get("actual_win_rate", 0),
                "win_rate_improvement": summary.get("win_rate_improvement", 0),
                "avg_pnl_delta": summary.get("avg_pnl_delta", 0),
                "shadow_avg_pnl": summary.get("shadow_avg_pnl", 0),
                "actual_avg_pnl": summary.get("actual_avg_pnl", 0),
                "multi_asset_proven": p3.get("assets_proven", []),
                "recommendation_accuracy": p2.get("accuracy_rate", 0),
                "recommendations_evaluated": (
                    p2.get("recommendations_accurate", 0)
                    + p2.get("recommendations_inaccurate", 0)
                ),
                "shadow_pnl_delta": summary.get("avg_pnl_delta", 0),
            },
            "restrictions": [
                "Proposals only — no automatic execution",
                "Human must approve each exit modification proposal",
                "Complete audit log of all proposals and decisions",
                "Disable available at any time via dashboard",
                "No changes to stop loss without human approval",
            ],
            "human_approval_required": True,
            "approved": False,
            "note": "This proposal was auto-generated when Phase 3 thresholds were met. "
                    "Phase 4 does not activate automatically — manual approval required.",
        }

        self.store.append_proposal(proposal)
        return proposal

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def backfill_historical_trades(self, trades: List[dict]) -> int:
        """Process historical trades not yet in exit_intelligence.jsonl."""
        if self._backfill_done:
            return 0

        existing_ids = self.store.load_all_trade_ids()
        analyzed = 0

        for trade in trades:
            trade_id = f"{trade.get('asset')}_{trade.get('timestamp', '')}"
            if trade_id in existing_ids:
                continue

            score = exit_quality_score(trade)
            observations = generate_deterministic_observations(trade)

            record = {
                "trade_id": trade_id,
                "asset": trade.get("asset"),
                "side": trade.get("side"),
                "entry_price": trade.get("entry_price"),
                "exit_price": trade.get("exit_price"),
                "pnl_pct": round(trade.get("pnl_pct", 0), 4),
                "exit_reason": trade.get("exit_reason", "unknown"),
                "duration_sec": trade.get("duration_sec", 0),
                "exit_quality_score": round(score, 4),
                "mfe_pct": round(trade.get("mfe_pct", 0.0), 4),
                "mae_pct": round(trade.get("mae_pct", 0.0), 4),
                "profit_if_held_pct": round(trade.get("profit_if_held_pct", 0.0), 4),
                "loss_avoided_pct": round(trade.get("loss_avoided_pct", 0.0), 4),
                "timestamp": trade.get("timestamp", ""),
                "strategy_version": trade.get("strategy_version"),
                "is_backfill": True,
                "observations": observations,
            }

            self.store.append_exit_record(record)
            self.store.update_asset_stats(trade.get("asset", ""), record)
            self.store.update_phase1_counters(record)
            existing_ids.add(trade_id)
            analyzed += 1

        if analyzed > 0:
            print(f"[EXIT INTEL] Backfilled {analyzed} historical trades into Exit Intelligence")
            self.check_phase_progression()

        self._backfill_done = True
        return analyzed


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

async def backfill_historical_trades(root: Path, trades: List[dict]) -> int:
    """Standalone backfill — used at startup."""
    analyzer = ExitIntelligenceAnalyzer(root)
    return await analyzer.backfill_historical_trades(trades)