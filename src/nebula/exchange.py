"""OKX V5 Demo-only REST adapter. Order requests are never automatically retried."""
import base64
import hashlib
import hmac
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from .config import DEMO_FLAG, DEMO_REST_URL, Settings, SafetyError


class OkxError(RuntimeError):
    pass


class OkxDemoClient:
    # Explicit endpoint/method allowlist. New API paths require review here.
    _PUBLIC_READS = {
        ("GET", "/api/v5/public/instruments"),
        ("GET", "/api/v5/market/tickers"),
        ("GET", "/api/v5/market/candles"),
        ("GET", "/api/v5/market/history-candles"),
    }
    _PRIVATE_READS = {
        ("GET", "/api/v5/account/config"),
        ("GET", "/api/v5/account/balance"),
        ("GET", "/api/v5/trade/order"),
        ("GET", "/api/v5/trade/fills-history"),
        ("GET", "/api/v5/trade/orders-pending"),
        ("GET", "/api/v5/trade/orders-algo-pending"),
        ("GET", "/api/v5/trade/order-algo"),
        ("GET", "/api/v5/trade/orders-algo-history"),
    }
    _PRIVATE_WRITES = {
        ("POST", "/api/v5/trade/order"),
        ("POST", "/api/v5/trade/order-algo"),
        ("POST", "/api/v5/trade/cancel-order"),
        ("POST", "/api/v5/trade/cancel-algos"),
    }

    def __init__(self, settings: Settings, session=None, timeout: float = 10.0):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.settings = settings
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base_url = DEMO_REST_URL

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _headers(self, method: str, path: str, body: str = "", private: bool = False) -> dict:
        headers = {"Content-Type": "application/json", "x-simulated-trading": DEMO_FLAG}
        if private:
            if not all((self.settings.api_key, self.settings.secret_key, self.settings.passphrase)):
                raise SafetyError("Private API credentials are missing")
            ts = self._timestamp()
            prehash = f"{ts}{method.upper()}{path}{body}".encode()
            digest = hmac.new(self.settings.secret_key.encode(), prehash, hashlib.sha256).digest()
            headers.update({
                "OK-ACCESS-KEY": self.settings.api_key,
                "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.settings.passphrase,
            })
        return headers

    def request(self, method: str, path: str, params=None, payload=None, private=False):
        method = str(method).upper()
        endpoint = (method, path)
        if endpoint in self._PUBLIC_READS:
            if private or payload is not None:
                raise SafetyError("Public market endpoints are read-only")
        elif endpoint in self._PRIVATE_READS:
            if not private or payload is not None:
                raise SafetyError("Private read endpoint requires authenticated GET only")
        elif endpoint in self._PRIVATE_WRITES:
            if not private or params is not None or payload is None:
                raise SafetyError("Private write endpoint requires authenticated POST with a body")
            if path == "/api/v5/trade/cancel-order":
                self.settings.validate_cancel_permission()
            else:
                self.settings.validate_order_permission()
        else:
            raise SafetyError("Endpoint is not allowlisted for NEBULA V1: " + method + " " + path)
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        request_path = f"{path}{query}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
        try:
            response = self.session.request(
                method.upper(), self.base_url + request_path,
                headers=self._headers(method, request_path, body, private),
                data=body or None, timeout=self.timeout,
                # Never let requests replay authenticated OKX headers to a redirect
                # target or transparently resubmit an order after a 307/308 response.
                allow_redirects=False,
            )
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                raise OkxError("Unexpected HTTP redirect; automatic redirects are disabled")
            response.raise_for_status()
            data = response.json()
        except requests.Timeout as exc:
            # Especially important for POST: caller must reconcile clOrdId before any retry.
            raise OkxError("OKX request timed out; outcome may be unknown; reconcile before retry") from exc
        except requests.RequestException as exc:
            raise OkxError(f"OKX transport error: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise OkxError("OKX returned invalid JSON") from exc
        if not isinstance(data, dict) or data.get("code") != "0":
            code = data.get("code", "unknown") if isinstance(data, dict) else "invalid_response"
            msg = data.get("msg", "") if isinstance(data, dict) else ""
            raise OkxError(f"OKX error {code}: {msg}")
        return data.get("data", [])

    def get_instruments(self):
        return self.request("GET", "/api/v5/public/instruments", {"instType": "SPOT"})

    def get_spot_instrument(self, inst_id: str):
        if not inst_id or not isinstance(inst_id, str):
            raise ValueError("inst_id is required")
        rows = self.request("GET", "/api/v5/public/instruments", {"instType": "SPOT", "instId": inst_id})
        if len(rows) != 1 or rows[0].get("instId") != inst_id:
            raise OkxError("Instrument specification unavailable or ambiguous")
        return rows[0]

    def get_tickers(self):
        return self.request("GET", "/api/v5/market/tickers", {"instType": "SPOT"})

    def get_candles(self, inst_id: str, bar: str = "1H", limit: int = 200):
        if not inst_id or limit < 2 or limit > 300:
            raise ValueError("invalid instrument or candle limit (2..300)")
        return self.request("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})

    def get_history_candles(self, inst_id: str, bar: str = "1H", limit: int = 300,
                            *, after: str | None = None, before: str | None = None):
        """Read one page of public historical candles; never accesses private APIs."""
        if not inst_id or not isinstance(inst_id, str) or isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("invalid instrument or history candle limit (1..100)")
        if after is not None and not str(after).isdigit():
            raise ValueError("after cursor must be a millisecond timestamp")
        if before is not None and not str(before).isdigit():
            raise ValueError("before cursor must be a millisecond timestamp")
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        return self.request("GET", "/api/v5/market/history-candles", params)

    def get_account_config(self):
        """Read account mode and borrowing configuration; never changes settings."""
        return self.request("GET", "/api/v5/account/config", private=True)

    def get_balance(self):
        """Read account balances; requires a read-only-capable Demo API key."""
        return self.request("GET", "/api/v5/account/balance", private=True)

    def get_order(self, inst_id: str, client_order_id: str):
        return self.request("GET", "/api/v5/trade/order", {"instId": inst_id, "clOrdId": client_order_id}, private=True)

    def get_order_by_id(self, inst_id: str, order_id: str):
        if not inst_id or not str(order_id).isdigit():
            raise ValueError("Valid instrument and numeric order_id are required")
        return self.request("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": str(order_id)}, private=True)

    def get_fills_history(self, inst_id: str, order_id: str, limit: int = 100, after: str | None = None):
        """Read one page of trade-level fills (OKX retains up to three months)."""
        if not inst_id or not str(order_id).isdigit() or not 1 <= int(limit) <= 100:
            raise ValueError("Invalid instrument, order_id, or fill history limit")
        if after is not None and not str(after).isdigit():
            raise ValueError("Fill history cursor must be a numeric billId")
        params = {"instType": "SPOT", "instId": inst_id,
                  "ordId": str(order_id), "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        return self.request("GET", "/api/v5/trade/fills-history", params, private=True)

    def cancel_order(self, *, inst_id: str, order_id: str):
        """Cancel an incomplete regular order, including while emergency stop is active."""
        self.settings.validate_cancel_permission()
        if not inst_id or not order_id or not str(order_id).isdigit():
            raise ValueError("Valid instrument and numeric order_id are required")
        result = self.request("POST", "/api/v5/trade/cancel-order",
                              payload={"instId": inst_id, "ordId": str(order_id)}, private=True)
        if result and result[0].get("sCode", "0") != "0":
            raise OkxError(f"OKX order cancel rejected {result[0].get('sCode')}: {result[0].get('sMsg', '')}")
        return result

    def get_pending_orders(self, inst_type: str = "SPOT"):
        if inst_type != "SPOT":
            raise ValueError("NEBULA V1 supports Spot only")
        return self.request("GET", "/api/v5/trade/orders-pending", {"instType": "SPOT"}, private=True)

    def get_pending_algo_orders(self, ord_type: str = "oco"):
        if ord_type not in {"oco", "conditional"}:
            raise ValueError("NEBULA V1 supports OCO/conditional protective orders only")
        return self.request("GET", "/api/v5/trade/orders-algo-pending",
                            {"ordType": ord_type, "instType": "SPOT"}, private=True)

    def get_algo_order(self, algo_client_order_id: str):
        if not algo_client_order_id or len(algo_client_order_id) > 32 or not algo_client_order_id.isalnum():
            raise ValueError("Valid algo client order ID is required")
        return self.request("GET", "/api/v5/trade/order-algo",
                            {"algoClOrdId": algo_client_order_id}, private=True)

    def get_algo_order_history(self, ord_type: str = "oco", state: str = "effective"):
        if ord_type not in {"oco", "conditional"} or state not in {"effective", "canceled", "order_failed"}:
            raise ValueError("Unsupported algo history filter")
        return self.request("GET", "/api/v5/trade/orders-algo-history",
                            {"ordType": ord_type, "state": state, "instType": "SPOT"}, private=True)

    def cancel_algo_order(self, *, inst_id: str, algo_id: str):
        self.settings.validate_order_permission()
        if not inst_id or not algo_id or not str(algo_id).isdigit():
            raise ValueError("Valid instrument and numeric algo_id are required")
        result = self.request("POST", "/api/v5/trade/cancel-algos",
                              payload=[{"instId": inst_id, "algoId": str(algo_id)}], private=True)
        if result and result[0].get("sCode", "0") != "0":
            raise OkxError(f"OKX algo cancel rejected {result[0].get('sCode')}: {result[0].get('sMsg', '')}")
        return result

    def place_spot_oco_exit(self, *, inst_id: str, size: str, take_profit: str,
                            stop_loss: str, algo_client_order_id: str):
        """Place a Spot sell OCO for already-filled base-asset quantity only."""
        self.settings.validate_order_permission()
        from decimal import Decimal, InvalidOperation
        if not inst_id or not inst_id.endswith("-USDT"):
            raise ValueError("V1 protective exits require a USDT-quoted Spot instrument")
        if not algo_client_order_id or len(algo_client_order_id) > 32 or not algo_client_order_id.isalnum():
            raise ValueError("algo_client_order_id must be 1..32 alphanumeric characters")
        try:
            qty, tp, sl = map(lambda x: Decimal(str(x)), (size, take_profit, stop_loss))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("OCO quantity and prices must be valid decimals") from exc
        if not all(x.is_finite() for x in (qty, tp, sl)) or qty <= 0 or sl <= 0 or tp <= sl:
            raise ValueError("OCO requires finite positive quantity/prices and take_profit > stop_loss")
        payload = {
            "instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "oco",
            "sz": format(qty, "f"), "algoClOrdId": algo_client_order_id,
            "tpTriggerPx": format(tp, "f"), "tpOrdPx": "-1", "tpTriggerPxType": "last",
            "slTriggerPx": format(sl, "f"), "slOrdPx": "-1", "slTriggerPxType": "last",
        }
        result = self.request("POST", "/api/v5/trade/order-algo", payload=payload, private=True)
        if result and result[0].get("sCode", "0") != "0":
            raise OkxError(f"OKX OCO rejected {result[0].get('sCode')}: {result[0].get('sMsg', '')}")
        return result

    def place_spot_limit(self, *, inst_id: str, side: str, size: str, price: str, client_order_id: str):
        self.settings.validate_order_permission()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if not client_order_id or len(client_order_id) > 32 or not client_order_id.isalnum():
            raise ValueError("client_order_id must be 1..32 alphanumeric characters")
        try:
            numeric_size, numeric_price = float(size), float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError("size and price must be numeric") from exc
        if not math.isfinite(numeric_size) or not math.isfinite(numeric_price) or numeric_size <= 0 or numeric_price <= 0:
            raise ValueError("size and price must be finite and positive")
        payload = {
            "instId": inst_id, "tdMode": "cash", "side": side,
            "ordType": "limit", "sz": str(size), "px": str(price),
            "clOrdId": client_order_id,
        }
        # No retry here. A timeout can occur after exchange acceptance.
        result = self.request("POST", "/api/v5/trade/order", payload=payload, private=True)
        if result and result[0].get("sCode", "0") != "0":
            raise OkxError(f"OKX order rejected {result[0].get('sCode')}: {result[0].get('sMsg', '')}")
        return result
