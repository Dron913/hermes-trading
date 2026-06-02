"""
TradeForge state backup/restore — run BEFORE and AFTER every deploy.

Usage:
  python backup_state.py check    # Show volume contents without backing up
  python backup_state.py backup   # Download state from Railway to local backup/
  python backup_state.py restore  # Upload local backup/ state back to Railway

Pattern for safe deploys:
  1. python backup_state.py backup   # (always do this before deploying)
  2. railway up                       # (deploy new version)
  3. python backup_state.py check     # (verify state survived)
  4. If state lost: python backup_state.py restore
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

BACKUP_DIR = Path(__file__).parent / "backup"
STATE_FILES = [
    "trades.jsonl", "strategy.yaml", "goal.yaml", "hypotheses.jsonl",
    "events.jsonl", "status.json", "heartbeat.json", "bootstrap_proof.json",
    "hermes_check.json", "self_learning_proof.json",
]


def _run(python_code: str, timeout: int = 30) -> str:
    """Run Python code inside the Railway container via railway run."""
    result = subprocess.run(
        ["railway", "run", "--", "python", "-c", python_code],
        capture_output=True, text=True, timeout=timeout, shell=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr[:200]}"
    return result.stdout


def cmd_check():
    """Show current state files in the Railway volume."""
    print("Checking Railway volume contents...")
    code = """
import json, os
state = '/app/state'
info = {}
fnames = ["trades.jsonl","strategy.yaml","goal.yaml","hypotheses.jsonl",
          "events.jsonl","status.json","heartbeat.json","bootstrap_proof.json",
          "hermes_check.json","self_learning_proof.json"]
for fname in fnames:
    p = os.path.join(state, fname)
    try:
        info[fname] = os.path.getsize(p)
    except OSError:
        info[fname] = 0
# Also check volume
try:
    hd = os.listdir(state)
    info['_volume_files'] = len(hd)
    info['_history_count'] = len([f for f in hd if f.startswith('history')])
except:
    pass
print(json.dumps(info))
"""
    out = _run(code)
    try:
        data = json.loads(out.strip())
        for k, v in data.items():
            prefix = "  " if k.startswith("_") else "  "
            suffix = " bytes" if isinstance(v, int) else ""
            print(f"{prefix}{k}: {v}{suffix}")
    except Exception as e:
        print(f"Could not read volume: {e}")
        print(f"Output: {out[:300]}")
        print("Make sure service is running: railway status")


def cmd_backup():
    """Download all state files from Railway volume to local backup/."""
    print("Backing up Railway volume to backup/ ...")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    for fname in STATE_FILES:
        code = f"""
import os
p = '/app/state/{fname}'
exists = os.path.exists(p)
print('EXISTS:'+open(p).read() if exists else 'NOTFOUND')
"""
        out = _run(code)
        lines = (out or "").strip()
        if lines.startswith("NOTFOUND"):
            bf = BACKUP_DIR / fname
            if bf.exists():
                bf.unlink()
            print(f"  {fname}: (not in volume)")
            continue

        content = out[len("EXISTS:"):]  # strip "EXISTS:" prefix
        bf = BACKUP_DIR / fname
        bf.write_text(content, encoding="utf-8")
        print(f"  {fname}: backed up ({len(content)} bytes)")

    # History directory
    code = """
import json, os, base64
hd = '/app/state/history'
files = []
if os.path.isdir(hd):
    for f in os.listdir(hd):
        fp = os.path.join(hd, f)
        if os.path.isfile(fp):
            files.append(f)
print(json.dumps(files))
"""
    out = _run(code)
    if out.strip().startswith("["):
        hist_files = json.loads(out.strip())
        if hist_files:
            print(f"  history/: {len(hist_files)} files")
            for hf in hist_files:
                code = f"print(open('/app/state/history/{hf}','rb').read())"
                raw = _run(code)
                if raw and not raw.startswith("ERROR"):
                    hp = BACKUP_DIR / "history" / hf
                    hp.parent.mkdir(parents=True, exist_ok=True)
                    hp.write_bytes(raw.encode("latin-1"))
                    print(f"    history/{hf}: backed up")
        else:
            print("  history/: empty")
    else:
        print("  history/: not found")

    print(f"\nBackup complete: {BACKUP_DIR}/")


def cmd_restore():
    """Upload local backup/ state back to Railway volume."""
    if not BACKUP_DIR.exists():
        print("No backup/ found. Run 'backup' first.")
        return

    print("Restoring state from backup/ to Railway volume...")

    for fname in STATE_FILES:
        fpath = BACKUP_DIR / fname
        if not fpath.exists():
            print(f"  {fname}: skipped (no backup)")
            continue

        # Use base64 to avoid shell escaping issues
        binary = fpath.read_bytes()
        b64 = base64.b64encode(binary).decode("ascii")

        # Write decoded content in-container
        code = f"""
import base64, os
data = base64.b64decode('{b64}').decode('utf-8', errors='replace')
fname = '{fname}'
with open(f'/app/state/{{fname}}', 'w', encoding='utf-8') as f:
    f.write(data)
"""
        result = _run(code)
        if "ERROR" in result[:10]:
            print(f"  {fname}: FAILED")
        else:
            print(f"  {fname}: restored")

    # Restore history/
    if (BACKUP_DIR / "history").is_dir():
        for hf in (BACKUP_DIR / "history").iterdir():
            if hf.is_file():
                b64 = base64.b64encode(hf.read_bytes()).decode("ascii")
                code = f"""
import base64, os
data = base64.b64decode('{b64}').decode('utf-8', errors='replace')
fname = '{hf.name}'
os.makedirs('/app/state/history', exist_ok=True)
with open(f'/app/state/history/{{fname}}', 'w', encoding='utf-8') as f:
    f.write(data)
"""
                result = _run(code)
                status = "restored" if "ERROR" not in result[:10] else "FAILED"
                print(f"  history/{hf.name}: {status}")

    print("\nRestore done. Restart service if needed: railway up")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "check":
        cmd_check()
    elif sys.argv[1] == "backup":
        cmd_backup()
    elif sys.argv[1] == "restore":
        cmd_restore()
    else:
        print(f"Unknown: {sys.argv[1]}")
        sys.exit(1)