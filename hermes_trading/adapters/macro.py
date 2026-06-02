"""Macro adapter — free FRED/DXY macro regime data."""
import os
from typing import Dict, Any
from datetime import datetime, timezone


async def fetch_macro() -> Dict[str, Any]:
    """Fetch macro regime indicators. Returns dict with schema_version."""
    return {
        "schema_version": "1.0",
        "source": "free",
        "dxy_index": None,
        "us10y_yield": None,
        "vix": None,
        " regime": "unknown",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }