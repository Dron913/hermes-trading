#!/usr/bin/env bash

# Directory for state
STATE_DIR="$HOME/hermes-trading/state"
HISTORY_DIR="$STATE_DIR/history"
HYPOTHESES_FILE="$STATE_DIR/hypotheses.jsonl"
COUNTER_FILE="$STATE_DIR/closed_trade_counter.txt"

mkdir -p "$HISTORY_DIR"
# Initialize counter file if not exists
if [ ! -f "$COUNTER_FILE" ]; then
  echo 0 > "$COUNTER_FILE"
fi

# Fetch recent logs (last 200 lines)
LOGS=$(railway --service TradeForge logs --tail 200 2>/dev/null || true)

# Count new closed trades since last check (simple heuristic: look for 'Trade closed' lines)
NEW_CLOSED=$(echo "$LOGS" | grep -c "Trade closed")
if [ "$NEW_CLOSED" -gt 0 ]; then
  # Update counter
  CURRENT=$(cat "$COUNTER_FILE")
  TOTAL=$((CURRENT + NEW_CLOSED))
  echo "$TOTAL" > "$COUNTER_FILE"
  echo "Detected $NEW_CLOSED new closed trades, total since last reflection: $TOTAL"
fi

# If we have reached 10 closed trades, perform reflection
if [ "$(cat "$COUNTER_FILE")" -ge 10 ]; then
  echo "Reflection triggered. Fetching recent trades and strategy..."
  # Pull last 25 outcomes
  railway run --service TradeForge cat /app/state/trades.jsonl > "$STATE_DIR/trades_latest.jsonl"
  # Pull current strategy
  railway run --service TradeForge cat /app/state/strategy.yaml > "$STATE_DIR/strategy.yaml"
  # Placeholder for scoring, hypothesis generation, and applying change
  # For now, just log that reflection would happen.
  echo "[{\"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"event\": \"reflection_start\"}]" >> "$HYPOTHESES_FILE"
  # Reset counter
  echo 0 > "$COUNTER_FILE"
  # Push potential changes (none for now)
  cd "$HOME/hermes-trading" && railway up --service TradeForge --detach
  echo "Pushed (no changes) to Railway."
fi
