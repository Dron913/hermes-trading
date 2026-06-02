#!/usr/bin/env python
"""E2E verification checkpoint script — output as JSON for easy parsing."""
import os, json, sys, glob

out = {"checks": [], "mode": "live"}

checks = [
    ("railway_worker", "/app/hermes_trading/loop.py", os.path.exists),
    ("strategy_yaml",  "/app/state/strategy.yaml",     os.path.exists),
    ("trades_jsonl",   "/app/state/trades.jsonl",       os.path.exists),
    ("goal_yaml",      "/app/state/goal.yaml",           os.path.exists),
]

for name, path, fn in checks:
    try:
        result = fn(path)
        out["checks"].append({"name": name, "path": path, "ok": result})
    except Exception as e:
        out["checks"].append({"name": name, "path": path, "ok": False, "error": str(e)})

# trades summary
trades_path = "/app/state/trades.jsonl"
if os.path.exists(trades_path):
    content = open(trades_path).read()
    lines = [l for l in content.strip().split("\n") if l]
    out["trades"] = {"count": len(lines)}
    if lines:
        try:
            first = json.loads(lines[0])
            out["trades"]["first"] = first.get("asset") + " " + first.get("side") + " " + str(first.get("pnl_pct")) + "%"
        except:
            out["trades"]["first"] = lines[0][:80]
else:
    out["trades"] = {"count": 0}

# strategy summary
strat_path = "/app/state/strategy.yaml"
if os.path.exists(strat_path):
    content = open(strat_path).read()
    for line in content.split("\n"):
        if line.startswith("version:"):
            out["strategy_version"] = line.split(":")[1].strip()
            break
else:
    out["strategy_version"] = "NOT FOUND"

# heartbeat
hb_path = "/app/state/heartbeat.json"
if os.path.exists(hb_path):
    out["heartbeat"] = json.loads(open(hb_path).read())

print(json.dumps(out, indent=2))