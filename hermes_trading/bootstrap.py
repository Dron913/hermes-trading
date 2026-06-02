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

    # Gap-fill only: essential state files must exist in /app/state/,
    # but we NEVER overwrite existing data — that's the user's learning.
    for f in defaults.rglob("*"):
        if f.is_file() and "/.venv/" not in str(f):
            rel = f.relative_to(defaults)
            dest = state / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(f.read_bytes())

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