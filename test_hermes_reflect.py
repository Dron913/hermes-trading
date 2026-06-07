import json, urllib.request

API_KEY = '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'

# ==========================================================
# INLINED PARSER (same as deployed reflect.py fix)
# ==========================================================
def extract_all_json_objects(text: str):
    """Bracket-depth scanner (deployed: reflect.py)"""
    results = []
    depth = 0; in_string = False; esc = False; start = -1
    for c in text:
        if esc: esc=False; continue
        if c == '\\' and in_string: esc=True; continue
        if c == '"': in_string = not in_string; continue
        if in_string: continue
        if c == '{':
            if depth == 0: start = text.index(c) if False else [i for i,ch in enumerate(text) if ch == '{'][0]
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start != -1:
                chunk = text[start:text.index(c)+1]
                if chunk.strip(): results.append(chunk)
                start = -1
    return results

# Actually let's be cleaner
def extract_all_json_objects_v2(text: str):
    results = []; depth = 0; in_str = False; esc = False; start = -1; i = 0
    while i < len(text):
        c = text[i]
        if esc: esc = False; i += 1; continue
        if c == '\\' and in_str: esc = True; i += 1; continue
        if c == '"': in_str = not in_str; i += 1; continue
        if in_str: i += 1; continue
        if c == '{':
            if depth == 0: start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start != -1:
                chunk = text[start:i+1]
                if chunk.strip(): results.append(chunk)
                start = -1
        i += 1
    return results

import re

def parse_hermes_output_v2(output: str) -> dict:
    """Deployed parser: markdown strip + bracket-depth + regex fallbacks"""
    cleaned = output
    for fence_pattern in [r'^```json\s*\n', r'^```\s*\n', r'\n```json\s*$', r'\n```\s*$', r'^```$']:
        cleaned = re.sub(fence_pattern, '', cleaned, flags=re.MULTILINE).strip()

    for text in (cleaned, output):
        objects = extract_all_json_objects_v2(text)
        if objects:
            for obj in objects:
                try:
                    data = json.loads(obj)
                    if isinstance(data, list) and len(data) > 0: data = data[0]
                    if isinstance(data, dict) and 'variable' in data:
                        d = str(data.get('direction','')).lower().strip()
                        dir_map = {
                            'loosen':'loosen','relax':'loosen','loosening':'loosen',
                            'tighten':'tighten','tightening':'tighten',
                            'increase':'increase','raising':'increase','higher':'increase',
                            'decrease':'decrease','lowering':'decrease','lower':'decrease',
                            'reduce':'decrease','reducing':'decrease',
                        }
                        if d in dir_map: d = dir_map[d]
                        if d not in ('loosen','tighten','increase','decrease'): continue
                        amt = data.get('amount')
                        if amt is None: continue
                        try: amt = float(amt)
                        except: continue
                        return {'variable': str(data['variable']), 'direction': d,
                                'amount': amt, 'reason': str(data.get('reason',''))}
                except: pass

    # Regex fallbacks
    for text in (cleaned, output):
        for pattern in [r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', r'\{[\s\S]*?"\w+"\s*:\s*["\-\d]']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                    if isinstance(data, list): data = data[0]
                    if isinstance(data, dict) and 'variable' in data:
                        amt = data.get('amount')
                        if amt is not None:
                            return {'variable': str(data['variable']),
                                    'direction': str(data.get('direction','')).lower(),
                                    'amount': float(amt),
                                    'reason': str(data.get('reason',''))}
                except: pass

    return {'variable': 'entry.rsi_threshold', 'direction': 'loosen', 'amount': 2,
            'reason': 'Hermes parse failed — default fallback: loosen RSI threshold.'}


# ==========================================================
# 1. GET RAILWAY TOKEN
# ==========================================================
with urllib.request.urlopen(
    urllib.request.Request('https://tradeforge-production-fbc1.up.railway.app/api/auth/login',
        data=json.dumps({'password':'drontradeforge1121'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
) as resp:
    login = json.loads(resp.read())
railway_token = login['token']

# ==========================================================
# 2. GET CURRENT TRADE DATA
# ==========================================================
with urllib.request.urlopen(
    urllib.request.Request(f'https://tradeforge-production-fbc1.up.railway.app/debug/state?token={railway_token}',
        headers={'Authorization': f'Bearer {railway_token}'})
) as resp:
    state_data = json.loads(resp.read())

trades_raw = state_data['trades.jsonl']
all_trades = [json.loads(l) for l in trades_raw.strip().split('\n') if l.strip()]
closed = [t for t in all_trades if t.get('exit_reason') not in (None, 'open', '')]
recent = closed[-25:] if len(closed) >= 25 else closed

print(f"Total closed trades: {len(closed)}, Using: {len(recent)}")
print(f"Strategy: {state_data['strategy.yaml'][:200]}")

# Build Hermes-style summary
stats = {'total_trades': len(recent), 'wins': len([t for t in recent if t.get('pnl_pct',0)>0]),
         'losses': len([t for t in recent if t.get('pnl_pct',0)<0]),
         'win_rate': round(len([t for t in recent if t.get('pnl_pct',0)>0])/len(recent)*100,1) if recent else 0,
         'total_pnl': round(sum(t.get('pnl_pct',0) for t in recent),2)}

print(f"\nStats: {stats}")

# ==========================================================
# 3. BUILD THE EXACT PROMPT + SYSTEM PROMPT THAT HERMES USES
# ==========================================================
trade_lines = []
for t in recent[-8:]:
    dur = t.get('duration_sec', 0)
    hr, mn = dur//3600, (dur%3600)//60
    dur_fmt = f"{hr}h{mn}m" if hr>0 else f"{mn}m"
    pnl = t.get('pnl_pct', 0)
    side = t.get('side', '?')
    asset = t.get('asset', '?')
    exit_r = t.get('exit_reason', '?')
    ind = t.get('indicators', {}).get('entry', {})
    pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"
    trade_lines.append(f"  {asset:10s} {side:5s} pnl={pnl_str} dur={dur_fmt} exit={exit_r}")

system_prompt = """You are Hermes, a sophisticated self-improving crypto trading strategy AI.
You analyze closed trade performance and suggest precise parameter adjustments.
Always respond with ONLY a valid JSON object and nothing else.

Output format (JSON only, no markdown, no explanation):
{"variable": "strategy.path", "direction": "loosen|tighten|increase|decrease",
 "amount": number, "reason": "explanation under 80 chars"}"""

user_prompt = f"""TRADING STRATEGY ADVISOR — analyze closed trades and propose one improvement.

== CLOSED TRADES (newest last, showing last 8) ==
{chr(10).join(trade_lines)}

Total trades in analysis window: {len(recent)}
Stats: {stats['total_trades']} trades, WR={stats['win_rate']}%, wins={stats['wins']}, losses={stats['losses']}, total_pnl={stats['total_pnl']}%

== GUIDANCE ==
- Analyze win rate, average winners vs losers, and per-asset performance
- Focus on parameters: RSI thresholds, stop loss %, take profit %, position sizing, EMAs
- Consider market regime: were losses due to noise or bad entries?
- Suggest one precise change — variable name, direction, amount, and reason
- Be aggressive enough to generate returns but conservative to avoid over-fitting

Respond ONLY with valid JSON."""

print(f"\n=== Hermes System Prompt ===\n{system_prompt[:200]}\n...")
print(f"\n=== Hermes User Prompt ===\n{user_prompt[:300]}\n...")

# ==========================================================
# 4. CALL GPT-OSS-120B WITH EXACT HERMES PROMPT
# ==========================================================
print("\n=== Calling GTP-OSS-120b via NIM API ===")
print("(This mirrors exactly what Hermes CLI sends to the model)")

payload = {
    "model": "openai/gpt-oss-120b",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    "max_tokens": 400,
    "temperature": 0.3
}

req = urllib.request.Request(
    "https://integrate.api.nvidia.com/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    method="POST"
)

with urllib.request.urlopen(req, timeout=120) as resp:
    result = json.loads(resp.read())

raw_output = result['choices'][0]['message']['content'].strip()
print(f"\nRAW OUTPUT ({len(raw_output)} chars):\n---START---\n{raw_output}\n---END---")

# ==========================================================
# 5. TEST THE PARSER ON RAW OUTPUT
# ==========================================================
print("\n=== Parser Test (bracket-depth + markdown strip) ===")
objects = extract_all_json_objects_v2(raw_output)
print(f"JSON objects found: {len(objects)}")
for j, o in enumerate(objects):
    print(f"  [{j}] {o[:150]}")
    try:
        parsed = json.loads(o)
        print(f"      Valid JSON: {list(parsed.keys())}")
    except: print(f"      Invalid JSON!")

print()
parsed = parse_hermes_output_v2(raw_output)
success = 'Hermes parse failed' not in parsed.get('reason','')
print(f"PARSER RESULT: {'SUCCESS' if success else 'FAILED'}")
print(f"Parsed: {json.dumps(parsed, indent=2)}")
print()

if success:
    print("="*70)
    print("✅ HERMES PARSER SUCCESS — GPT-OSS-120B IS DRIVING REFLECTION!")
    print("="*70)
else:
    print("❌ PARSER FAILED — will fall back to default")
    print(f"Reason: {parsed.get('reason','')}")