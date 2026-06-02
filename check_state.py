import os, json, sys

for path in ["/app/state/trades.jsonl", "/app/state/strategy.yaml", "/app/state/hypotheses.jsonl", "/app/state/heartbeat.json"]:
    print(f"=== {path} ===")
    if not os.path.exists(path):
        print("NOT FOUND")
    else:
        content = open(path, "rb").read()
        print(f"size={len(content)} bytes")
        try:
            text = content.decode()
            if path.endswith(".jsonl"):
                lines = text.strip().split("\n")
                print(f"lines={len(lines)}")
                if lines and lines[0]:
                    print(f"  sample: {lines[0][:150]}")
            else:
                print(text[:400])
        except:
            print(content[:100])
    print(sys.stdout.flush())