"""Entry point: python -m hermes_trading.run"""

import argparse
import asyncio
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


def _gen_token():
    return "".join(random.choices(string.ascii_letters + string.digits, k=32))


def _auth_ok(token_or_none):
    if not _auth["enabled"]:
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
] if os.getenv("STATE_DIR") else []
_STATE_ROOT = Path(os.getenv("STATE_DIR", str(Path(__file__).parent.parent) + "/state"))


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
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _cors_preflight(self):
            self._set_headers(204, cors=True)
            self.wfile.write(b"")

        def _authenticate(self):
            token = self.path.split("?token=", 1)[-1].split("&")[0] if "?" in self.path else None
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
                    "unlocked": _auth_ok(self.path.split("?token=", 1)[-1].split("&")[0]) if _auth["enabled"] else True,
                    "tokenTtl": _auth["token_ttl"],
                }).encode())
            elif self.path.startswith("/debug/state"):
                if _auth["enabled"]:
                    token = self.path.split("?token=", 1)[-1].split("&")[0]
                    if not _auth_ok(token):
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
            elif self.path.startswith("/debug/file/"):
                if not _auth_ok(self.headers.get("Authorization", "").replace("Bearer ", "")):
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
                # Extract token from query string for auth check
                token = self.path.split("?token=", 1)[-1].split("&")[0] if "?token=" in self.path else None
                if _auth["enabled"] and not _auth_ok(token):
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