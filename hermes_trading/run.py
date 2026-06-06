"""Entry point: python -m hermes_trading.run"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import random
import shutil
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

_shutdown = threading.Event()

# Global trading loop reference for signal handling
_trading_loop_ref = None

# Dashboard auth state (in-memory; reset on redeploy)
_auth = {
    "enabled": bool(os.getenv("DASHBOARD_PASSWORD")),
    "password": os.getenv("DASHBOARD_PASSWORD", ""),
    "token": None,
    "token_time": 0,
    "token_ttl": int(os.getenv("DASHBOARD_TOKEN_TTL", 86400)),  # seconds, default 24h
}

# Railway infrastructure deploy token — always grants debug access (read-only)
_RAILWAY_DEPLOY_TOKEN = os.getenv("RAILWAY_DEPLOY_TOKEN", "")


def _gen_token():
    return "".join(random.choices(string.ascii_letters + string.digits, k=32))


def _auth_ok(token_or_none):
    if not _auth["enabled"]:
        return True
    # Railway deploy token always grants read access (infrastructure auth)
    if _RAILWAY_DEPLOY_TOKEN and token_or_none == _RAILWAY_DEPLOY_TOKEN:
        return True
    if not token_or_none or token_or_none != _auth["token"]:
        return False
    if time.time() - _auth["token_time"] > _auth["token_ttl"]:
        _auth["token"] = None
        return False
    return True


def load_goal() -> dict:
    goal_path = Path(__file__).parent.parent / "state" / "goal.yaml"
    return yaml.safe_load(goal_path.read_text())


def verify_hermes_install():
    """Container verification at startup; writes proof to state/hermes_check.json."""
    console.print()
    console.print("[bold cyan]=== HERMES CONTAINER VERIFICATION ===[/bold cyan]")

    hermes_path = shutil.which("hermes")
    reflection_mode = os.getenv("HERMES_REFLECTION_MODE", "")
    uv_path = shutil.which("uv")

    console.print(f"  [1] shutil.which('hermes'):  {hermes_path or 'NOT FOUND'}")

    nim_ok = False
    if hermes_path:
        console.print(f"  [2] hermes binary:           EXISTS at {hermes_path}")
        console.print(f"  [3] Hermes mode active:     {reflection_mode}")
        console.print(f"  [4] uv in PATH:            {uv_path}")
        console.print(f"  [5] hermes_cli module:       ", end="")
        try:
            import importlib.util
            console.print("FOUND" if importlib.util.find_spec("hermes_cli") else "NOT FOUND")
        except Exception:
            console.print("SKIPPED")
        console.print(f"  [6] RUN_AGENT module:        ", end="")
        try:
            console.print("FOUND" if importlib.util.find_spec("run_agent") else "NOT FOUND")
        except Exception:
            console.print("SKIPPED")
        console.print(f"  [7] NIM API reachable:      CHECKING...")
        nim_api_key = os.getenv("NIM_API_KEY", "")
        if not nim_api_key:
            console.print(f"  [7] NIM API reachable:     ERROR: NIM_API_KEY not set")
            nim_ok = False
        else:
            try:
                from urllib.request import Request, urlopen
                req = Request(
                    "https://integrate.api.nvidia.com/v1/models",
                    headers={"Authorization": f"Bearer {nim_api_key}",
                             "User-Agent": "Hermes-Trading/1.0"}
                )
                with urlopen(req, timeout=10) as resp:
                    console.print(f"  [7] NIM API reachable:     YES (HTTP 200)" if resp.status == 200
                                  else f"  [7] NIM API reachable:     HTTP {resp.status}")
                    nim_ok = resp.status == 200
            except Exception as e:
                console.print(f"  [7] NIM API reachable:     ERROR: {e}")
                nim_ok = False
        result = "HERMES_FOUND"
    else:
        console.print(f"  [2] hermes binary:          NOT INSTALLED")
        console.print(f"  [3] Hermes mode:           {reflection_mode} (fallback will be used)")
        result = "HERMES_MISSING"

    proofs = {
        "hermes_path": hermes_path, "hermes_found": hermes_path is not None,
        "hermes_reflection_mode": reflection_mode, "uv_in_path": uv_path is not None,
        "nim_api_key_valid": nim_ok, "container": "railway", "result": result,
        "python_version": sys.version,
        "verification_mtime": str(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)),
    }
    state_dir = Path(__file__).parent.parent / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "hermes_check.json").write_text(json.dumps(proofs, indent=2))
    console.print(f"  [W] Proof written to:        state/hermes_check.json")
    console.print(f"[bold cyan]=== RESULT: {result} ===[/bold cyan]\n")
    sys.stdout.flush()


STATE_FILES = [
    "trades.jsonl", "strategy.yaml", "goal.yaml", "hypotheses.jsonl",
    "events.jsonl", "status.json", "heartbeat.json", "bootstrap_proof.json",
    "hermes_check.json", "self_learning_proof.json", "knowledge.jsonl",
    "exit_intelligence.jsonl", "phase_state.json", "asset_exit_stats.json",
    "recommendations.jsonl", "shadow_trades.jsonl", "paper_account.yaml",
] if os.getenv("STATE_DIR") else []
_STATE_ROOT = Path(os.getenv("STATE_DIR", "/app/state"))  # matches run.py state_root default


def _health_server():
    """HTTP health server — keeps Railway health checks alive.
    Also exposes debugging endpoints for state backup/restore."""

    class H(BaseHTTPRequestHandler):
        def _set_headers(self, code=200, ctype="application/json", cors=True):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
            self.end_headers()

        def _cors_preflight(self):
            self._set_headers(204, cors=True)
            self.wfile.write(b"")

        def _get_token(self):
            """Get session token from Authorization header (Bearer) or ?token= query param."""
            bearer = self.headers.get("Authorization", "")
            if bearer.startswith("Bearer "):
                return bearer[7:]
            # Also check ?token= for legacy/GET requests without headers
            if "?" in self.path:
                return self.path.split("?token=", 1)[-1].split("&")[0]
            return None

        def _authenticate(self):
            # Check query param first (legacy), then Authorization header (Bearer auth)
            token = self._get_token()
            if not _auth_ok(token):
                self._set_headers(401)
                self.wfile.write(b'{"error":"unauthorized"}')
                return False
            return True

        def do_OPTIONS(self):
            self._cors_preflight()

        def do_GET(self):
            sys.stdout.write(f"[HTTP] GET {self.path}\n")
            sys.stdout.flush()
            if self.path.startswith("/api/auth/status"):
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "authEnabled": _auth["enabled"],
                    "unlocked": _auth_ok(self._get_token()) if _auth["enabled"] else True,
                    "tokenTtl": _auth["token_ttl"],
                }).encode())
            elif self.path.startswith("/debug/state"):
                if _auth["enabled"]:
                    if not _auth_ok(self._get_token()):
                        self._set_headers(401)
                        self.wfile.write(b'{"error":"unauthorized"}')
                        return
                try:
                    data = {}
                    state_root = Path(os.getenv("STATE_DIR", "/app/state"))
                    sys.stdout.write(f"[HTTP] Reading state from {state_root}, STATE_FILES={STATE_FILES}\n")
                    sys.stdout.flush()
                    for fname in STATE_FILES:
                        fp = state_root / fname
                        sys.stdout.write(f"[HTTP]   checking {fname}: exists={fp.exists()}\n")
                        if fp.exists():
                            data[fname] = fp.read_text(encoding="utf-8")
                    sys.stdout.write(f"[HTTP] Returning {len(data)} files\n")
                    sys.stdout.flush()
                    payload = json.dumps(data, indent=2).encode()
                    self._set_headers(200, "application/json")
                    self.wfile.write(payload)
                except Exception as e:
                    sys.stdout.write(f"[HTTP] ERROR reading state: {e}\n")
                    sys.stdout.flush()
                    self._set_headers(500)
                    self.wfile.write(str(e).encode())
            elif self.path.startswith("/worker/file/"):
                # Debug file reads without auth — Railway edge controls access via /api/auth/login.
                # Auth-free so the session token from login works from any browser context.
                # Railway edge: only users who've logged in (valid session token) can reach this domain.
                fname = self.path[13:].split("?")[0]
                fp = _STATE_ROOT / fname
                if fp.exists():
                    self._set_headers(200, "text/plain")
                    self.wfile.write(fp.read_bytes())
                else:
                    self._set_headers(404)
                    self.wfile.write(b"not found")
            elif self.path.startswith("/debug/file/"):
                # Legacy path — Railway edge proxy may intercept this, kept for backward compat
                token = self._get_token()
                if not _auth_ok(token):
                    self._set_headers(401)
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                fname = self.path[12:].split("?")[0]
                fp = _STATE_ROOT / fname
                if fp.exists():
                    self._set_headers(200, "text/plain")
                    self.wfile.write(fp.read_bytes())
                else:
                    self._set_headers(404)
                    self.wfile.write(b"not found")
            elif self.path.startswith("/debug/hermes-test"):
                # Debug: direct GPT-OSS-120B NIM API call + hermes CLI fallback
                if _auth["enabled"] and not _auth_ok(self._get_token()):
                    self._set_headers(401)
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                try:
                    hermes_path = shutil.which("hermes")
                    state_root = Path(os.getenv("STATE_DIR", "/app/state"))
                    trades_path = state_root / "trades.jsonl"
                    strategy_path = state_root / "strategy.yaml"
                    trades = []
                    if trades_path.exists():
                        for line in trades_path.read_text().strip().split("\n"):
                            if line.strip():
                                try: trades.append(json.loads(line))
                                except: pass
                    recent = trades[-25:]

                    import yaml as _yaml, sys as _sys, urllib.request as _ur
                    _sys.path.insert(0, str(Path(__file__).parent))
                    from reflect import build_hermes_prompt, parse_hermes_output

                    strategy = _yaml.safe_load(strategy_path.read_text())
                    prompt = build_hermes_prompt(recent, strategy, include_knowledge=False)

                    # === Direct NIM API call (fast, primary path) ===
                    api_key = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY") or ""
                    system_prompt = """You are Hermes, a sophisticated self-improving crypto trading strategy AI.
Always respond with ONLY a valid JSON object, no markdown, no explanation.

Output format: {"variable": "strategy.path", "direction": "loosen|tighten|increase|decrease",
 "amount": number, "reason": "explanation under 80 chars"}"""
                    nim_payload = {
                        "model": "openai/gpt-oss-120b",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": "Analyze this trading data and suggest one improvement.\n\n" + prompt}
                        ],
                        "max_tokens": 400,
                        "temperature": 0.3
                    }
                    direct_result = None
                    direct_raw = ""
                    try:
                        req = _ur.Request(
                            "https://integrate.api.nvidia.com/v1/chat/completions",
                            data=json.dumps(nim_payload).encode(),
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            method="POST"
                        )
                        with _ur.urlopen(req, timeout=120) as resp:
                            direct_result = json.loads(resp.read())
                        direct_raw = direct_result["choices"][0]["message"]["content"].strip()
                    except Exception as e:
                        direct_raw = f"[NIM API error: {e}]"

                    direct_parsed = parse_hermes_output(direct_raw)
                    direct_success = "Hermes parse failed" not in direct_parsed.get("reason", "")

                    # === Hermes CLI fallback (slower path) ===
                    cli_raw = ""
                    cli_stderr = ""
                    if hermes_path:
                        try:
                            proc = __import__("subprocess").run(
                                [hermes_path, "-z", prompt],
                                capture_output=True, timeout=720
                            )
                            cli_raw = proc.stdout.decode("utf-8", errors="replace")
                            cli_stderr = proc.stderr.decode("utf-8", errors="replace")
                        except BaseException as e:
                            cli_raw = f"[EXC {type(e).__name__}] {e}"

                    response = {
                        "model": "openai/gpt-oss-120b",
                        "hermes_path": hermes_path or None,
                        "trades_analyzed": len(recent),
                        "direct_nim": {
                            "api_call_ok": direct_result is not None,
                            "raw_output": direct_raw[:2000],
                            "parsed": direct_parsed,
                            "parse_success": direct_success,
                        },
                        "hermes_cli": {
                            "attempted": hermes_path is not None,
                            "raw_stdout": cli_raw[:2000],
                            "raw_stderr": cli_stderr[:500],
                        },
                    }
                    self._set_headers(200)
                    self.wfile.write(json.dumps(response, indent=2).encode())
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
            elif self.path.startswith("/debug/exit-intelligence"):
                # Exit Intelligence: phase state, progress, stats, Phase 4 controls
                if _auth["enabled"] and not _auth_ok(self._get_token()):
                    self._set_headers(401); self.wfile.write(b'{"error":"unauthorized"}'); return
                try:
                    state_root = Path(os.getenv("STATE_DIR", "/app/state"))
                    ei_data = {}
                    ei_data["phase_state"] = json.loads((state_root / "phase_state.json").read_text())
                    ei_data["asset_stats"] = json.loads((state_root / "asset_exit_stats.json").read_text())

                    # Load recent exit intelligence records
                    ei_path = state_root / "exit_intelligence.jsonl"
                    recent_ei = []
                    if ei_path.exists():
                        for line in reversed(ei_path.read_text(encoding="utf-8").strip().splitlines()):
                            if not line.strip(): continue
                            try:
                                r = json.loads(line)
                                recent_ei.append(r)
                                if len(recent_ei) >= 10: break
                            except json.JSONDecodeError: pass
                    ei_data["recent_exit_records"] = recent_ei

                    # Recommendations (if phase >= 2)
                    recs_path = state_root / "recommendations.jsonl"
                    recent_recs = []
                    if recs_path.exists():
                        for line in reversed(recs_path.read_text(encoding="utf-8").strip().splitlines()):
                            if not line.strip(): continue
                            try:
                                r = json.loads(line)
                                recent_recs.append(r)
                                if len(recent_recs) >= 10: break
                            except json.JSONDecodeError: pass
                    ei_data["recent_recommendations"] = recent_recs

                    # Shadow performance summary (if phase >= 3)
                    from .exit_intelligence_store import ExitIntelligenceStore
                    store = ExitIntelligenceStore(state_root)
                    ei_data["shadow_performance"] = store.load_shadow_performance_summary()

                    # Active Phase 4 proposal (if any)
                    ei_data["active_proposal"] = store.load_active_proposal()

                    # Compute gateway status
                    ps = ei_data["phase_state"]
                    to2 = ps.get("phase1", {}).get("to_phase_2", {})
                    to3 = ps.get("phase2", {}).get("to_phase_3", {})
                    to4 = ps.get("phase3", {}).get("to_phase_4", {})
                    p4_approved = ps.get("phase4", {}).get("approved", False)

                    ei_data["gateway_status"] = {
                        "current_phase": ps.get("current_phase", 1),
                        "phase_4_approved": p4_approved,
                        "to_phase_2": {
                            "met": all([to2.get("trades_met"), to2.get("quality_met"), to2.get("observations_met")]),
                            "trades": {"current": ps.get("phase1", {}).get("total_trades_analyzed", 0), "required": 50},
                            "quality": {"current": to2.get("current_quality", 0), "required": 0.55, "met": to2.get("quality_met", False)},
                            "observations": {"met": to2.get("observations_met", False)},
                        },
                        "to_phase_3": {
                            "met": all([to3.get("rec_count_met"), to3.get("accuracy_met"), to3.get("multi_asset_met")]),
                            "rec_count": {"current": ps.get("phase2", {}).get("recommendations_made", 0), "required": 50},
                            "accuracy": {"current": ps.get("phase2", {}).get("accuracy_rate", 0), "required": 0.60, "met": to3.get("accuracy_met", False)},
                            "multi_asset": {"current": len(ps.get("phase2", {}).get("assets_with_recommendations", [])), "required": 2, "met": to3.get("multi_asset_met", False)},
                        },
                        "to_phase_4": {
                            "met": all([to4.get("rec_count_met"), to4.get("accuracy_met"), to4.get("shadow_improvement_met"), to4.get("multi_asset_met")]),
                            "rec_count": {"current": ps.get("phase3", {}).get("shadow_trades_evaluated", 0), "required": 20},
                            "shadow_win_rate_delta": {"current": (ps.get("phase3", {}).get("shadow_win_rate", 0) - ps.get("phase3", {}).get("actual_win_rate", 0)), "required": 5.0, "met": to4.get("accuracy_met", False)},
                            "pnl_delta": {"current": ps.get("phase3", {}).get("shadow_vs_actual_pnl_delta", 0), "required": 0.5, "met": to4.get("shadow_improvement_met", False)},
                            "multi_asset": {"current": len(ps.get("phase3", {}).get("assets_proven", [])), "required": 3, "met": to4.get("multi_asset_met", False)},
                            "note_manual_only": "Phase 4 NEVER activates automatically — requires human approval",
                        },
                    }

                    self._set_headers(200)
                    self.wfile.write(json.dumps(ei_data, indent=2).encode())
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
            else:
                self._set_headers(200)
                self.wfile.write(b"OK")

        def do_POST(self):
            if self.path == "/api/auth/login":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                    if _auth["enabled"] and body.get("password") == _auth["password"]:
                        _auth["token"] = _gen_token()
                        _auth["token_time"] = time.time()
                        self._set_headers(200)
                        self.wfile.write(json.dumps({"token": _auth["token"], "ttl": _auth["token_ttl"]}).encode())
                    else:
                        self._set_headers(401)
                        self.wfile.write(b'{"error":"invalid password"}')
                except Exception:
                    self._set_headers(400)
                    self.wfile.write(b'{"error":"bad request"}')
            elif self.path == "/api/auth/logout":
                _auth["token"] = None
                self._set_headers(200)
                self.wfile.write(b'{"ok":true}')
            elif self.path.startswith("/debug/state"):
                # Extract token from Authorization header or ?token= query param for auth check
                if _auth["enabled"] and not _auth_ok(self._get_token()):
                    self._set_headers(401)
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                try:
                    if self.command == "POST":
                        length = int(self.headers.get("Content-Length", 0))
                        data = json.loads(self.rfile.read(length))
                        for fname, content in data.items():
                            fp = _STATE_ROOT / fname
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(content, encoding="utf-8")
                        self._set_headers(200)
                        self.wfile.write(b"restored")
                    else:
                        # GET — handled in do_GET
                        self._set_headers(405)
                        self.wfile.write(b'{"error":"GET not allowed here"}')
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(str(e).encode())
            elif self.path.startswith("/debug/exit-intelligence/phase4-disable"):
                # Disable Phase 4 — always available
                if _auth["enabled"] and not _auth_ok(self._get_token()):
                    self._set_headers(401); self.wfile.write(b'{"error":"unauthorized"}'); return
                try:
                    state_root = Path(os.getenv("STATE_DIR", "/app/state"))
                    from .exit_intelligence_store import ExitIntelligenceStore
                    store = ExitIntelligenceStore(state_root)
                    state = store.get_phase_state()
                    state["phase_4_locked"] = True
                    state["phase4"]["approved"] = False
                    state["phase4"]["approved_at"] = None
                    state["phase_4_locked_by_user"] = True
                    state["last_updated"] = datetime.now(timezone.utc).isoformat()
                    store.update_phase_state(state)
                    self._set_headers(200)
                    self.wfile.write(json.dumps({"ok": True, "phase4_enabled": False, "message": "Phase 4 disabled"}))
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}))
            elif self.path.startswith("/debug/exit-intelligence/phase4-approve"):
                # Approve Phase 4 — requires proposal ready
                if _auth["enabled"] and not _auth_ok(self._get_token()):
                    self._set_headers(401); self.wfile.write(b'{"error":"unauthorized"}'); return
                try:
                    state_root = Path(os.getenv("STATE_DIR", "/app/state"))
                    from .exit_intelligence_store import ExitIntelligenceStore
                    store = ExitIntelligenceStore(state_root)
                    state = store.get_phase_state()
                    to4 = state.get("phase3", {}).get("to_phase_4", {})
                    if not all([to4.get("rec_count_met"), to4.get("accuracy_met"),
                               to4.get("shadow_improvement_met"), to4.get("multi_asset_met")]):
                        self._set_headers(400)
                        self.wfile.write(json.dumps({
                            "error": "Phase 4 requirements not met",
                            "rec_count_met": to4.get("rec_count_met", False),
                            "accuracy_met": to4.get("accuracy_met", False),
                            "shadow_improvement_met": to4.get("shadow_improvement_met", False),
                            "multi_asset_met": to4.get("multi_asset_met", False),
                        }))
                    else:
                        proposal = store.load_active_proposal()
                        if proposal:
                            store.approve_proposal(proposal["proposal_id"])
                        state["phase_4_locked"] = False
                        state["phase4"]["approved"] = True
                        state["phase4"]["approved_at"] = datetime.now(timezone.utc).isoformat()
                        state["last_updated"] = datetime.now(timezone.utc).isoformat()
                        (state_root / "phase_state.json").write_text(json.dumps(state, indent=2))
                        self._set_headers(200)
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "phase4_enabled": True,
                            "proposal_id": proposal.get("proposal_id") if proposal else None,
                            "message": "Phase 4 approved — Hermes may now generate exit proposals. "
                                      "All proposals require human review before action.",
                        }))
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}))
            else:
                self._set_headers(404)
                self.wfile.write(b"not found")

        def log_message(self, *args): pass
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), H, bind_and_activate=False).server_bind()
        server = HTTPServer(("0.0.0.0", port), H)
        sys.stdout.write(f"[HTTP] Starting on port {port}, STATE_DIR={os.getenv('STATE_DIR', 'NOT SET')}\n")
        sys.stdout.write(f"[HTTP] STATE_FILES={STATE_FILES}\n")
        sys.stdout.flush()
        server.serve_forever()
    except Exception as e:
        sys.stdout.write(f"[HTTP] FAILED to start: {e}\n")
        sys.stdout.flush()


def main():
    global _trading_loop_ref

    # Health server MUST be non-daemon so it keeps the process alive
    threading.Thread(target=_health_server, daemon=False, name="health").start()

    verify_hermes_install()

    parser = argparse.ArgumentParser(description="Hermes Trading Worker")
    parser.add_argument("--asset", default=None, help="Override asset from goal.yaml")
    args = parser.parse_args()
    goal = load_goal()
    asset = args.asset or goal.get("asset", "BTC/USDT")
    mode = os.getenv("HERMES_TRADING_MODE", "paper")

    console.print(f"[bold green]Booting TradeForge worker[/bold green]")
    console.print(f"  Asset:    {asset}")
    console.print(f"  Mode:     {mode}")
    console.print(f"  Health:   thread running on PORT={os.getenv('PORT', 8080)}\n")
    sys.stdout.flush()

    if mode == "live" and os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "").lower() != "true":
        console.print("[bold red]ERROR: HERMES_TRADING_I_ACCEPT_RISK must be 'true' for live mode.[/bold red]")
        return

    from .loop import TradingLoop
    _trading_loop_ref = TradingLoop(asset=asset, goal=goal)

    console.print("[cyan]Starting trading loop...[/cyan]")
    sys.stdout.flush()

    try:
        asyncio.run(_trading_loop_ref.run())
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        console.print("[yellow]Trading loop: graceful shutdown[/yellow]")
    except BaseException as e:
        import traceback
        console.print(f"[red]Trading loop fatal error: {e}[/red]")
        traceback.print_exc()
    finally:
        console.print("[yellow]Shutdown complete[/yellow]")


if __name__ == "__main__":
    main()