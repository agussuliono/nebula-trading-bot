"""OKX V5 Demo-only REST adapter. Order requests are never automatically retried."""
import base64
import hashlib
import hmac
import json
import math
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from decimal import Decimal, InvalidOperation

from .config import DEMO_FLAG, DEMO_REST_URL, Settings, SafetyError
from .instruments import InstrumentRuleError, SpotInstrument
from .bill_reconciliation import _create_complete_account_bills_snapshot


class OkxError(RuntimeError):
    """Sanitized exchange error with optional non-secret HTTP diagnostics."""

    def __init__(self, message, *, status_code=None, error_code=None,
                 content_type=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.content_type = content_type
        self.retryable = bool(retryable)

    def diagnostic(self):
        """Return safe metadata only; never include URL, headers, or response body."""
        result = {"error_type": type(self).__name__, "message": str(self)}
        if self.status_code is not None:
            result["http_status"] = self.status_code
        if self.error_code is not None:
            result["api_error_code"] = self.error_code
        if self.content_type is not None:
            result["content_type"] = self.content_type
        if self.retryable:
            result["retryable"] = True
        return result


class _RetryablePublicFailure(OkxError):
    pass


class _PublicApiCircuitBreaker:
    """Small per-client breaker for idempotent public market-data GETs only."""

    def __init__(self, *, failure_threshold=3, cooldown_seconds=15.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = None
        self._probe_in_progress = False

    def before_request(self):
        with self._lock:
            if self._open_until is None:
                return
            now = self._clock()
            if now < self._open_until:
                raise OkxError("Public market-data circuit breaker is open; retry after cooldown")
            if self._probe_in_progress:
                raise OkxError("Public market-data circuit breaker probe is already in progress")
            self._probe_in_progress = True

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._open_until = None
            self._probe_in_progress = False

    def record_transient_failure(self):
        with self._lock:
            if self._probe_in_progress:
                self._failures = self.failure_threshold
            else:
                self._failures += 1
            self._probe_in_progress = False
            if self._failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown_seconds


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
        ("GET", "/api/v5/account/bills"),
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

    @property
    def base_url(self) -> str:
        """Expose the fixed Demo host as a read-only compatibility property."""
        return DEMO_REST_URL

    def __init__(self, settings: Settings, session=None, timeout: float = 10.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("timeout must be a finite positive number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.settings = settings
        self.session = session or requests.Session()
        self.timeout = timeout_value
        self._startup_entry_permit = None
        self._startup_entry_state = None
        self._public_circuit = _PublicApiCircuitBreaker()

    def install_startup_entry_permit(self, permit, state_store):
        from .entry_gate import StartupEntryPermit
        if not isinstance(permit, StartupEntryPermit):
            raise SafetyError("A valid StartupRecovery permit is required")
        if permit._client is not self or permit._state is not state_store:
            raise SafetyError("Startup permit is bound to a different client or state store")
        # A fresh successful recovery may replace an older unused/consumed permit.
        # Managers holding the old object cannot authorize through this client.
        self._startup_entry_permit = permit
        self._startup_entry_state = state_store

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

    @staticmethod
    def _response_content_type(response):
        raw = getattr(response, "headers", {}).get("Content-Type", "")
        value = str(raw).split(";", 1)[0].strip().lower()
        if not value or any(not (char.isalnum() or char in "/.+-") for char in value):
            return "unknown"
        return value[:80]

    def _safe_api_message(self, value):
        message = " ".join(str(value).split())
        for secret in (self.settings.api_key, self.settings.secret_key, self.settings.passphrase):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message[:160]

    @staticmethod
    def _safe_error_code(value):
        text = str(value)
        return text if text.isascii() and text.isdigit() else "unknown"

    def _request_once(self, method, request_path, body, private):
        return self.session.request(
            method, DEMO_REST_URL + request_path,
            headers=self._headers(method, request_path, body, private),
            data=body or None, timeout=self.timeout,
            # Never replay authenticated headers or resubmit a write through redirects.
            allow_redirects=False,
        )

    def _request_with_policy(self, method, request_path, body, private, public_read):
        # Only idempotent, unauthenticated public GETs are retried. Private reads and
        # every POST are single-attempt because exchange outcomes must be reconciled.
        max_attempts = 3 if public_read else 1
        for attempt in range(max_attempts):
            try:
                response = self._request_once(method, request_path, body, private)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if public_read and attempt + 1 < max_attempts:
                    time.sleep(min(0.25 * (2 ** attempt), 0.75))
                    continue
                if public_read:
                    raise _RetryablePublicFailure(
                        f"OKX public transport failed after {attempt + 1} attempts: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                if method == "POST":
                    raise OkxError(
                        "OKX write transport failed; outcome may be unknown; reconcile before retry"
                    ) from exc
                raise OkxError(f"OKX transport error: {type(exc).__name__}") from exc
            except requests.RequestException as exc:
                raise OkxError(f"OKX transport error: {type(exc).__name__}") from exc

            status_code = getattr(response, "status_code", None)
            content_type = self._response_content_type(response)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                raise OkxError(
                    "Unexpected HTTP redirect; automatic redirects are disabled",
                    status_code=status_code, content_type=content_type,
                )

            if isinstance(status_code, int) and (status_code == 429 or 500 <= status_code <= 599):
                if public_read and attempt + 1 < max_attempts:
                    time.sleep(min(0.25 * (2 ** attempt), 0.75))
                    continue
                message = f"OKX HTTP {status_code}: transient server/rate-limit response"
                if public_read:
                    raise _RetryablePublicFailure(
                        message, status_code=status_code, content_type=content_type, retryable=True,
                    )
                if method == "POST":
                    message += "; write outcome must be reconciled before any retry"
                raise OkxError(message, status_code=status_code, content_type=content_type)

            if isinstance(status_code, int) and status_code >= 400:
                error_code = None
                api_message = ""
                if "json" in content_type:
                    try:
                        error_data = response.json()
                    except ValueError:
                        error_data = None
                    if isinstance(error_data, dict):
                        error_code = error_data.get("code")
                        api_message = self._safe_api_message(error_data.get("msg", ""))
                detail = f"API code {error_code}" if error_code not in (None, "") else f"non-success response (content-type={content_type})"
                if api_message:
                    detail += f": {api_message}"
                raise OkxError(
                    f"OKX HTTP {status_code}: {detail}",
                    status_code=status_code, error_code=error_code, content_type=content_type,
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise OkxError(
                    f"OKX returned invalid JSON (HTTP {status_code}; content-type={content_type})",
                    status_code=status_code, content_type=content_type,
                ) from exc

            if not isinstance(data, dict) or data.get("code") != "0":
                raw_code = data.get("code", "unknown") if isinstance(data, dict) else "invalid_response"
                code = self._safe_error_code(raw_code)
                msg = self._safe_api_message(data.get("msg", "")) if isinstance(data, dict) else ""
                detail = f"OKX API error {code}"
                if msg:
                    detail += f": {msg}"
                raise OkxError(
                    detail, status_code=status_code, error_code=code, content_type=content_type,
                )
            return data.get("data", [])

        raise OkxError("Public request retry policy ended unexpectedly")

    def request(self, method: str, path: str, params=None, payload=None, private=False):
        method = str(method).upper()
        endpoint = (method, path)
        public_read = endpoint in self._PUBLIC_READS
        if public_read:
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
            # Final HTTP-boundary check prevents bypass through request() itself.
            if path == "/api/v5/trade/order" and payload.get("side") == "buy":
                permit = self._startup_entry_permit
                if permit is None:
                    raise SafetyError("Spot entry blocked: clean startup recovery permit is missing")
                try:
                    permit.authorize_request(self, payload)
                except Exception as exc:
                    raise SafetyError("Spot entry blocked by startup recovery gate") from exc
        else:
            raise SafetyError("Endpoint is not allowlisted for NEBULA V1: " + method + " " + path)

        query = f"?{urlencode(params, doseq=True)}" if params else ""
        request_path = f"{path}{query}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
        if public_read:
            self._public_circuit.before_request()
        try:
            result = self._request_with_policy(method, request_path, body, private, public_read)
        except _RetryablePublicFailure:
            self._public_circuit.record_transient_failure()
            raise
        except OkxError:
            # A definitive HTTP/API response (including 403) proves the transport
            # is reachable; it must not poison the transient-network circuit.
            if public_read:
                self._public_circuit.record_success()
            raise
        else:
            if public_read:
                self._public_circuit.record_success()
            return result

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

    def get_history_candles(self, inst_id: str, bar: str = "1H", limit: int = 100,
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

    def get_account_bills(self, *, begin: str | None = None, end: str | None = None,
                          limit: int = 100):
        """Read complete account bills for a bounded window inside OKX's 7-day retention.

        The API is read-only. Pagination follows OKX's newest-first billId cursor:
        each after cursor must move strictly toward older bill IDs. Incomplete,
        malformed, out-of-window, or over-cap results fail closed.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Account bill page limit must be an integer between 1 and 100")
        for name, value in (("begin", begin), ("end", end)):
            if value is not None:
                text_value = str(value)
                if isinstance(value, bool) or not text_value.isascii() or not text_value.isdigit():
                    raise ValueError(f"{name} must be a millisecond timestamp")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        oldest_supported_ms = now_ms - 7 * 24 * 60 * 60 * 1000
        effective_begin = int(begin) if begin is not None else oldest_supported_ms
        effective_end = int(end) if end is not None else now_ms
        if effective_begin > effective_end:
            raise ValueError("begin must not be later than end")
        if effective_begin < oldest_supported_ms or effective_end > now_ms:
            raise ValueError("Account bills window must be fully inside the last 7 days")
        rows_all, seen, cursor = [], set(), None
        for _page in range(100):
            params = {"limit": str(limit)}
            if begin is not None:
                params["begin"] = str(begin)
            if end is not None:
                params["end"] = str(end)
            if cursor is not None:
                params["after"] = cursor
            page = self.request("GET", "/api/v5/account/bills", params, private=True)
            if (not isinstance(page, list) or len(page) > limit
                    or any(not isinstance(row, dict) for row in page)):
                raise OkxError("OKX account-bills page is malformed")
            previous_id = None
            for row in page:
                bill_id = str(row.get("billId") or "")
                if not bill_id.isascii() or not bill_id.isdigit() or bill_id in seen:
                    raise OkxError("OKX account-bills pagination has a missing, invalid, or duplicate billId")
                numeric_id = int(bill_id)
                if previous_id is not None and numeric_id >= previous_id:
                    raise OkxError("OKX account-bills page is not in descending billId order")
                if cursor is not None and numeric_id >= int(cursor):
                    raise OkxError("OKX account-bills after cursor did not advance to older bills")
                previous_id = numeric_id
                seen.add(bill_id)
                rows_all.append(row)
            if len(page) < limit:
                return _create_complete_account_bills_snapshot(
                    rows_all, begin_ms=effective_begin, end_ms=effective_end,
                    page_count=_page + 1, terminal_page_seen=True,
                )
            next_cursor = str(page[-1].get("billId") or "")
            if not next_cursor or next_cursor == cursor:
                raise OkxError("OKX account-bills pagination cursor did not advance")
            cursor = next_cursor
        raise OkxError("OKX account-bills pagination exceeded the safety page limit")

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

    def _get_all_pending_pages(self, path: str, params: dict, cursor_field: str):
        """Read every pending-order page; fail closed on malformed or looping cursors."""
        rows_all = []
        seen_ids = set()
        cursor = None
        for _page in range(100):
            page_params = {**params, "limit": "100"}
            if cursor is not None:
                page_params["after"] = cursor
            page = self.request("GET", path, page_params, private=True)
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise OkxError("OKX pending-order page is malformed")
            for row in page:
                record_id = str(row.get(cursor_field) or "")
                if not record_id or record_id in seen_ids:
                    raise OkxError("OKX pending-order pagination has a missing or duplicate cursor")
                seen_ids.add(record_id)
                rows_all.append(row)
            if len(page) < 100:
                return rows_all
            next_cursor = str(page[-1].get(cursor_field) or "")
            if not next_cursor or next_cursor == cursor:
                raise OkxError("OKX pending-order pagination cursor did not advance")
            cursor = next_cursor
        raise OkxError("OKX pending-order pagination exceeded the safety page limit")

    def get_pending_orders(self, inst_type: str = "SPOT"):
        if inst_type != "SPOT":
            raise ValueError("NEBULA V1 supports Spot only")
        return self._get_all_pending_pages(
            "/api/v5/trade/orders-pending",
            {"instType": "SPOT"},
            "ordId",
        )

    def get_pending_algo_orders(self, ord_type: str = "oco"):
        if ord_type not in {"oco", "conditional"}:
            raise ValueError("NEBULA V1 supports OCO/conditional protective orders only")
        return self._get_all_pending_pages(
            "/api/v5/trade/orders-algo-pending",
            {"ordType": ord_type, "instType": "SPOT"},
            "algoId",
        )

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

    def _get_validated_spot_instrument(self, inst_id: str) -> SpotInstrument:
        """Fetch and validate current rules for a live USDT-quoted Spot market."""
        if not isinstance(inst_id, str) or not inst_id.endswith("-USDT"):
            raise ValueError("V1 orders require a USDT-quoted Spot instrument")
        try:
            spec = SpotInstrument.from_okx(self.get_spot_instrument(inst_id))
        except (InstrumentRuleError, AttributeError, TypeError) as exc:
            raise ValueError("OKX Spot instrument specification is invalid") from exc
        if spec.inst_id != inst_id or spec.quote_ccy != "USDT":
            raise ValueError("Instrument does not match the requested USDT Spot market")
        return spec

    @staticmethod
    def _validate_spot_order_values(spec: SpotInstrument, *, side: str,
                                    size: str, price: str) -> tuple[str, str]:
        """Reject values requiring normalization; never silently alter an order."""
        try:
            normalized_size, normalized_price = spec.normalize_order(side, size, price)
        except InstrumentRuleError as exc:
            raise ValueError(str(exc)) from exc
        if Decimal(str(size)) != Decimal(normalized_size):
            raise ValueError("Order size does not comply with OKX lotSz")
        if Decimal(str(price)) != Decimal(normalized_price):
            raise ValueError("Order price does not comply with OKX tickSz")
        return normalized_size, normalized_price

    def place_spot_oco_exit(self, *, inst_id: str, size: str, take_profit: str,
                            stop_loss: str, algo_client_order_id: str):
        """Place a Spot sell OCO for already-filled base-asset quantity only."""
        self.settings.validate_order_permission()
        if not algo_client_order_id or len(algo_client_order_id) > 32 or not algo_client_order_id.isalnum():
            raise ValueError("algo_client_order_id must be 1..32 alphanumeric characters")
        try:
            qty, tp, sl = map(Decimal, (str(size), str(take_profit), str(stop_loss)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("OCO quantity and prices must be valid decimals") from exc
        if not all(x.is_finite() for x in (qty, tp, sl)) or qty <= 0 or sl <= 0 or tp <= sl:
            raise ValueError("OCO requires finite positive quantity/prices and take_profit > stop_loss")
        spec = self._get_validated_spot_instrument(inst_id)
        validated_size, validated_tp = self._validate_spot_order_values(
            spec, side="sell", size=size, price=take_profit
        )
        validated_size_sl, validated_sl = self._validate_spot_order_values(
            spec, side="sell", size=size, price=stop_loss
        )
        if validated_size != validated_size_sl:
            raise ValueError("OCO quantity normalization is inconsistent")
        payload = {
            "instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "oco",
            "sz": validated_size, "algoClOrdId": algo_client_order_id,
            "tpTriggerPx": validated_tp, "tpOrdPx": "-1", "tpTriggerPxType": "last",
            "slTriggerPx": validated_sl, "slOrdPx": "-1", "slTriggerPxType": "last",
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
            numeric_size, numeric_price = Decimal(str(size)), Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("size and price must be valid decimals") from exc
        if (not numeric_size.is_finite() or not numeric_price.is_finite()
                or numeric_size <= 0 or numeric_price <= 0):
            raise ValueError("size and price must be finite and positive")
        spec = self._get_validated_spot_instrument(inst_id)
        validated_size, validated_price = self._validate_spot_order_values(
            spec, side=side, size=size, price=price
        )
        payload = {
            "instId": inst_id, "tdMode": "cash", "side": side,
            "ordType": "limit", "sz": validated_size, "px": validated_price,
            "clOrdId": client_order_id,
        }
        # No retry here. A timeout can occur after exchange acceptance.
        result = self.request("POST", "/api/v5/trade/order", payload=payload, private=True)
        if result and result[0].get("sCode", "0") != "0":
            raise OkxError(f"OKX order rejected {result[0].get('sCode')}: {result[0].get('sMsg', '')}")
        return result
