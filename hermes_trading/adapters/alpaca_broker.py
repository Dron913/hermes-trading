"""Alpaca paper trading broker — real orders on Alpaca, OHLCV stays via ccxt."""
import httpx, os, sys, time
from typing import Dict, Optional


KRKEN_TO_ALPACA = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
    "SOL/USDT": "SOLUSD",
    "XRP/USDT": "XRPUSD",
}


class AlpacaBroker:
    """Wraps Alpaca Paper Trading REST API v2."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://paper-api.alpaca.markets"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _get(self, path: str) -> dict:
        resp = self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = self._client.post(path, json=data)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        resp = self._client.delete(path)
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------------------
    # Account
    # -------------------------------------------------------------------------
    def get_equity(self) -> float:
        """Current equity in dollars."""
        return float(self._get("/v2/account")["equity"])

    def get_buying_power(self) -> float:
        """Available buying power."""
        return float(self._get("/v2/account")["buying_power"])

    # -------------------------------------------------------------------------
    # Positions — sync these into loop._positions at the start of each cycle
    # -------------------------------------------------------------------------
    def get_positions(self) -> Dict[str, dict]:
        """Returns {kraken_symbol: {qty, entry_price, side, current_price, unrealized_pl_pct}}."""
        raw = self._get("/v2/positions")
        positions = {}
        # Reverse mapping: BTCUSD → BTC/USDT
        alpaca_to_kraken = {v: k for k, v in KRKEN_TO_ALPACA.items()}
        for p in raw:
            asset = p["symbol"]           # e.g. BTCUSD
            kraken_sym = alpaca_to_kraken.get(asset, asset)
            qty = abs(float(p["qty"]))
            avg_entry = float(p["avg_entry_price"])
            side = p["side"]              # long or short
            current_price = float(p["current_price"])
            upnl_pct = float(p["unrealized_plpc"])   # e.g. -0.0235 = -2.35%
            positions[kraken_sym] = {
                "qty": qty,
                "entry_price": avg_entry,
                "side": side,
                "current_price": current_price,
                "unrealized_pl_pct": upnl_pct,
                "alpaca_symbol": asset,   # keep for order routing
            }
        return positions

    def get_position(self, alpaca_sym: str) -> Optional[dict]:
        """Get single position or None."""
        try:
            p = self._get(f"/v2/positions/{alpaca_sym}")
            return p
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    # -------------------------------------------------------------------------
    # Order execution
    # -------------------------------------------------------------------------
    def submit_entry(
        self,
        symbol: str,
        side: str,
        equity: float,
        risk_pct: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        entry_price: Optional[float] = None,
    ) -> tuple[Optional[str], Optional[float]]:
        """
        Submit market entry + separate TP/SL orders.
        Returns (entry_order_id, filled_price) or (None, None).
        Crypto does NOT support bracket orders — we submit as separate orders.
        """
        alpaca_sym = KRKEN_TO_ALPACA.get(symbol, symbol)
        if entry_price is None:
            entry_price = self.get_current_price(symbol)
        if entry_price is None:
            sys.stdout.write(f"[ERROR] Cannot estimate entry price for {symbol}\n")
            sys.stdout.flush()
            return None, None

        qty = self._calc_qty(symbol, equity, risk_pct, entry_price)
        order_side = side  # long → buy, short → sell

        # Step 1: Submit market order
        order_body = {
            "symbol": alpaca_sym,
            "qty": str(qty),
            "side": order_side,
            "type": "market",
            "time_in_force": "day",
        }
        try:
            entry_result = self._post("/v2/orders", order_body)
            entry_order_id = entry_result.get("id")
            sys.stdout.write(f"[INFO] Alpaca market order submitted: {entry_order_id} {side} {qty} {symbol}\n")
            sys.stdout.flush()
        except httpx.HTTPStatusError as e:
            sys.stdout.write(f"[ERROR] Alpaca entry failed for {symbol}: {e.response.text}\n")
            sys.stdout.flush()
            return None, None

        # Step 2: Wait for fill (up to 15s)
        filled_price = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                orders = self._get(f"/v2/orders?status=filled&limit=5")
                for o in orders:
                    if o.get("id") == entry_order_id and o.get("status") == "filled":
                        filled_price = float(o.get("filled_avg_price", entry_price))
                        sys.stdout.write(f"[INFO] Alpaca entry FILL: {filled_price} (order {entry_order_id})\n")
                        sys.stdout.flush()
                        break
                if filled_price:
                    break
            except Exception:
                pass
            time.sleep(1)

        if filled_price is None:
            # Could not get fill — use estimated price but log warning
            filled_price = entry_price
            sys.stdout.write(f"[WARN] Alpaca fill not confirmed for {symbol}, using estimate {entry_price}\n")
            sys.stdout.flush()

        # Step 3: Submit separate TP and SL orders with the real filled price
        close_side = "sell" if side == "long" else "buy"
        self._submit_tp_sl(alpaca_sym, qty, close_side, filled_price, stop_loss_pct, take_profit_pct)

        return entry_order_id, filled_price

    def _submit_tp_sl(self, alpaca_sym, qty, close_side, entry_price, sl_pct, tp_pct):
        """Submit separate take-profit and stop-loss orders from a known entry price."""
        # For stop-loss: opposite side from entry. Buy to close long, sell to close short.
        # For take-profit: same as close_side (opposite of entry)
        if close_side == "sell":  # was long (entry was buy), close by selling
            stop_price = round(entry_price * (1 - sl_pct), 4)
            tp_price = round(entry_price * (1 + tp_pct), 4)
            sl_type, tp_type = "stop", "limit"
        else:  # was short (entry was sell), close by buying
            stop_price = round(entry_price * (1 + sl_pct), 4)
            tp_price = round(entry_price * (1 - tp_pct), 4)
            sl_type, tp_type = "stop", "limit"

        try:
            # Stop loss
            sl_order = self._post("/v2/orders", {
                "symbol": alpaca_sym,
                "qty": str(qty),
                "side": close_side,       # sell to close long, buy to close short
                "type": sl_type,
                "stop_price": str(stop_price),
                "time_in_force": "gtc",
            })
            sys.stdout.write(f"[INFO] Alpaca SL: {sl_order.get('id')} stop={stop_price}\n")
            sys.stdout.flush()

            # Take profit
            tp_order = self._post("/v2/orders", {
                "symbol": alpaca_sym,
                "qty": str(qty),
                "side": close_side,
                "type": tp_type,
                "limit_price": str(tp_price),
                "time_in_force": "gtc",
            })
            sys.stdout.write(f"[INFO] Alpaca TP: {tp_order.get('id')} limit={tp_price}\n")
            sys.stdout.flush()
        except httpx.HTTPStatusError as e:
            sys.stdout.write(f"[ERROR] Alpaca TP/SL failed: {e.response.text}\n")
            sys.stdout.flush()

    def submit_market_order(self, symbol: str, qty: Optional[float], side: str) -> Optional[str]:
        """Close a position with a market order.

        If qty is None, fetches the current position qty from Alpaca.
        Pass the CLOSE side (sell for long, buy for short).
        """
        alpaca_sym = KRKEN_TO_ALPACA.get(symbol, symbol)
        if qty is None:
            pos = self.get_position(alpaca_sym)
            if pos is None:
                sys.stdout.write(f"[WARN] No Alpaca position for {symbol} to close\n")
                sys.stdout.flush()
                return None
            qty = abs(float(pos["qty"]))
        try:
            result = self._post("/v2/orders", {
                "symbol": alpaca_sym,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": "gtc",
            })
            return result.get("id")
        except httpx.HTTPStatusError as e:
            sys.stdout.write(f"[ERROR] Alpaca submit_market_order failed for {symbol}: {e.response.text}\n")
            sys.stdout.flush()
            return None

    def close_all_positions(self, symbol: Optional[str] = None) -> Optional[dict]:
        """Close all positions, or just one symbol."""
        path = f"/v2/positions/{symbol}" if symbol else "/v2/positions"
        try:
            return self._delete(path)
        except httpx.HTTPStatusError as e:
            sys.stdout.write(f"[ERROR] Alpaca close failed: {e.response.text}\n")
            sys.stdout.flush()
            return None

    def wait_for_fill(self, order_id: str, timeout: float = 30.0) -> bool:
        """Poll until order is filled (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                orders = self._get("/v2/orders")
                for o in orders:
                    if o["id"] == order_id:
                        if o["status"] == "filled":
                            return True
                        if o["status"] in ("rejected", "cancelled", "expired"):
                            return False
                        break
            except Exception:
                pass
            time.sleep(2)
        return False

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    def _calc_qty(self, symbol: str, equity: float, risk_pct: float, entry_price: float) -> float:
        """Calculate position size in base units based on risk % of equity."""
        alpaca_sym = KRKEN_TO_ALPACA.get(symbol, symbol)
        if entry_price is None or entry_price <= 0:
            return 0.0
        dollar_risk = equity * risk_pct
        qty = dollar_risk / entry_price
        # Round to asset-specific lot sizes
        if alpaca_sym == "BTCUSD":
            return round(qty, 4)
        elif alpaca_sym in ("ETHUSD", "SOLUSD"):
            return round(qty, 3)
        elif alpaca_sym == "XRPUSD":
            return round(qty, 1)
        else:
            return round(qty, 2)

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Latest trade price from Alpaca crypto market data.

        Note: Alpaca crypto uses USD pairs (BTCUSD), strategy uses USDT (BTC/USDT).
        Price difference is negligible (<0.1%) so we use the USD price as proxy.
        Returns None if fetch fails — callers should have a fallback.
        """
        data_base = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
        data_key = os.getenv("ALPACA_DATA_API_KEY", self.api_key)
        data_secret = os.getenv("ALPACA_DATA_API_SECRET", self.api_secret)

        alpaca_sym = KRKEN_TO_ALPACA.get(symbol, symbol)
        try:
            c = httpx.Client(
                base_url=data_base,
                headers={
                    "APCA-API-KEY-ID": data_key,
                    "APCA-API-SECRET-KEY": data_secret,
                },
                timeout=10.0,
            )
            resp = c.get(
                f"/v1beta1/crypto/{alpaca_sym}/trades",
                params={"limit": 1},
            )
            c.close()
            if resp.status_code == 200:
                data = resp.json()
                if data.get("trades"):
                    return float(data["trades"][0]["p"])
        except Exception:
            pass
        return None

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancel all open orders for a symbol (used before manual close)."""
        alpaca_sym = KRKEN_TO_ALPACA.get(symbol, symbol)
        try:
            resp = self._client.get(f"/v2/orders?status=open&symbols={alpaca_sym}")
            if resp.status_code == 200:
                orders = resp.json()
                for o in orders:
                    if o.get("symbol") == alpaca_sym:
                        del_id = o["id"]
                        self._client.delete(f"/v2/orders/{del_id}")
        except Exception as e:
            sys.stdout.write(f"[WARN] cancel_open_orders failed: {e}\n")
            sys.stdout.flush()

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass