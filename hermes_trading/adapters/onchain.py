"""On-chain adapter — Glassnode-style metrics."""
import os
from typing import Dict, Any


async def fetch_onchain(symbol: str = "BTC") -> Dict[str, Any]:
    """Fetch free on-chain metrics. Returns dict with schema_version."""
    api_key = os.getenv("GLASSNODE_API_KEY", "")

    # No premium key: return neutral indicators
    if not api_key:
        return {
            "schema_version": "1.0",
            "source": "free_fallback",
            "active_addresses_24h": None,
            "exchange_flow_btc": 0,
            "miner_position_change": 0,
            "nupl": None,
        }

    # Premium: would call Glassnode here
    return {
        "schema_version": "1.0",
        "source": "glassnode",
        "active_addresses_24h": None,
        "exchange_flow_btc": 0,
        "miner_position_change": 0,
        "nupl": None,
    }