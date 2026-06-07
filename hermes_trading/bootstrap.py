"""Bootstrap: seed persistent volume from defaults, then hand off to trading.

Logic:
  - /app/defaults/  ← baked into Docker image (never changes)
  - /app/state/     ← Railway persistent volume mount (survives all restarts)

  Always seeds/updates all essential state files:
    - FIRST DEPLOY (volume empty): seeds all default files to volume
    - SUBSEQUENT DEPLOY (volume has data): fills any gaps + updates proof

  This makes the system self-healing: if Railway removes the volume,
  the next deploy re-seeds from defaults; existing volumes keep learning.
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

def main():
    defaults = Path("/app/defaults")
    state    = Path("/app/state")
    state.mkdir(parents=True, exist_ok=True)
    existing = any(f for f in state.rglob("*") if f.is_file())

    if existing:
        print("[BOOT] Persistent state found — checking for gap-fill...")
        files = [str(p.relative_to(state)) for p in state.rglob("*") if p.is_file()]
        print(f"[BOOT] Volume has: {files}")
    else:
        print("[BOOT] Fresh volume — seeding from /app/defaults/")
    sys.stdout.flush()

    # Rebuild from defaults — ensures all essential state files exist with real content.
    # On FIRST DEPLOY (volume empty): all files seeded from defaults.
    # On CORRUPTED volume (files contain only placeholder "OK" etc.): delete + re-seed.
    # On HEALTHY volume (has real data): gap-fill only (never overwrite).
    for f in defaults.rglob("*"):
        if f.is_file() and "/.venv/" not in str(f):
            rel = f.relative_to(defaults)
            dest = state / rel
            content_ok = False
            is_json = dest.suffix in (".json", ".jsonl")
            is_yaml = dest.suffix in (".yaml", ".yml")

            if dest.exists():
                raw = dest.read_text(encoding="utf-8").strip()
                # Corrupted: file is empty, or contains only placeholder "OK"/"true"/"false"
                if raw in ("", "OK", "true", "false", "null"):
                    print(f"[BOOT] Corrupted state file '{rel}' — removing for re-seed")
                    dest.unlink()
                else:
                    # Non-empty, non-placeholder content — keep existing (preserve learning)
                    content_ok = True

            if not content_ok and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(f.read_bytes())

    # Also ensure runtime-generated files exist (not in defaults/ but needed by worker)
    runtime_files = {
        "status.json": json.dumps({"open_positions": [], "paper_account": {"starting_balance": 100000, "current_balance": 100000, "realized_pnl_usd": 0, "unrealized_pnl_usd": 0, "available_capital": 100000, "deployed_capital": 0, "capital_utilization_pct": 0.0}}, indent=2),
    }
    for fname, default_content in runtime_files.items():
        fp = state / fname
        if not fp.exists() or fp.read_text(encoding="utf-8").strip() in ("", "OK", "true", "false", "null"):
            print(f"[BOOT] Re-seeding runtime file '{fname}'")
            fp.write_text(default_content, encoding="utf-8")

    seeded = [str(p.relative_to(state)) for p in state.rglob("*") if p.is_file()]
    print(f"[BOOT] State in volume: {seeded}")
    sys.stdout.flush()

    proof = {
        "bootstrap_at": datetime.now(timezone.utc).isoformat(),
        "persistent_volume_mount": str(state),
        "defaults_source": str(defaults),
        "existing_state_found": existing,
        "persistence_epoch": 0,
        "files": seeded,
    }

    proof_path = state / "bootstrap_proof.json"
    if proof_path.exists():
        prev = json.loads(proof_path.read_text())
        proof["persistence_epoch"] = prev.get("persistence_epoch", 0) + 1
        proof["previous_bootstrap_at"] = prev.get("bootstrap_at")

    (state / "bootstrap_proof.json").write_text(json.dumps(proof, indent=2))
    print(f"[BOOT] Proof: {state}/bootstrap_proof.json epoch={proof['persistence_epoch']}")
    sys.stdout.flush()

if __name__ == "__main__":
    main()