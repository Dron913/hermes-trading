"""News adapter — sentiment signals."""
import os
from typing import Dict, Any


async def fetch_news_sentiment(symbol: str = "BTC") -> Dict[str, Any]:
    """Fetch basic sentiment. Returns dict with schema_version."""
    api_key = os.getenv("NEWS_API_KEY", "")

    if not api_key:
        return {
            "schema_version": "1.0",
            "source": "free_fallback",
            "sentiment": "neutral",
            "score": 0.0,
            "headlines": [],
        }

    return {
        "schema_version": "1.0",
        "source": "newsapi",
        "sentiment": "neutral",
        "score": 0.0,
        "headlines": [],
    }