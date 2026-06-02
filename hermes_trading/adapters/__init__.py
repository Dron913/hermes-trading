"""Adapters package."""
from .price import fetch_ohlcv_multitimeframe
from .onchain import fetch_onchain
from .news import fetch_news_sentiment
from .macro import fetch_macro

__all__ = ["fetch_ohlcv_multitimeframe", "fetch_onchain", "fetch_news_sentiment", "fetch_macro"]