"""Process-local, one-shot authorization issued only by clean startup recovery."""
import hashlib
import json
import os


class StartupPermitError(RuntimeError):
    pass


_ISSUER = object()


def _state_fingerprint(state):
    digest = hashlib.sha256()
    with state._connect() as db:
        for table in ("intents", "order_snapshots", "position_lock",
                      "protection_intents", "exit_child_orders", "audit_events", "fills"):
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            digest.update(table.encode())
            for row in rows:
                digest.update(json.dumps(tuple(row), sort_keys=True, default=str,
                                         separators=(",", ":")).encode())
    return digest.hexdigest()


class StartupEntryPermit:
    """One-shot permit bound to the exact client, state, API identity and process."""

    def __init__(self, issuer, client, state, account_uid):
        if issuer is not _ISSUER:
            raise StartupPermitError("Only clean startup recovery can issue an entry permit")
        self._client = client
        self._state = state
        self._pid = os.getpid()
        self._key_digest = hashlib.sha256(
            str(getattr(getattr(client, "settings", None), "api_key", "")).encode()
        ).hexdigest()
        self._account_uid = str(account_uid or "")
        self._state_digest = _state_fingerprint(state)
        self._consumed = False
        self._write_started = False
        self._order_binding = None

    def consume(self, client, state, *, instrument, size, price, client_order_id):
        if self._consumed:
            raise StartupPermitError("Startup permit has already been consumed")
        if client is not self._client or state is not self._state or os.getpid() != self._pid:
            raise StartupPermitError("Startup permit client, state, or process binding mismatch")
        current_key = hashlib.sha256(
            str(getattr(getattr(client, "settings", None), "api_key", "")).encode()
        ).hexdigest()
        if current_key != self._key_digest:
            raise StartupPermitError("Startup permit API identity mismatch")
        if _state_fingerprint(state) != self._state_digest:
            raise StartupPermitError("Local state changed after recovery; run recovery again")
        if (not instrument or not size or not price or not client_order_id):
            raise StartupPermitError("A specific normalized entry order must be bound to the permit")
        self._order_binding = {
            "instId": str(instrument), "sz": str(size), "px": str(price),
            "clOrdId": str(client_order_id),
        }
        self._consumed = True

    def authorize_request(self, client, payload):
        if (client is not self._client or os.getpid() != self._pid
                or not self._consumed or self._write_started):
            raise StartupPermitError("Fresh one-shot startup recovery permit required")
        current_key = hashlib.sha256(
            str(getattr(getattr(client, "settings", None), "api_key", "")).encode()
        ).hexdigest()
        if current_key != self._key_digest or not isinstance(payload, dict):
            raise StartupPermitError("Startup permit identity or order payload mismatch")
        if payload.get("side") != "buy" or payload.get("tdMode") != "cash":
            raise StartupPermitError("Permit only authorizes a Spot cash buy")
        if any(str(payload.get(field, "")) != value
               for field, value in self._order_binding.items()):
            raise StartupPermitError("Spot buy does not match the exact order bound to startup permit")
        self._write_started = True


def issue_startup_entry_permit(client, state, account_uid):
    """Internal issuer called only after a fully clean StartupRecovery result."""
    return StartupEntryPermit(_ISSUER, client, state, account_uid)
