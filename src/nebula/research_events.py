"""Phase 0A research event foundation.

Research-only module: canonical JSON, typed event builders with allowlist
schemas, Ed25519 scheduler heartbeat signing, an atomic anti-replay
sequence store, a best-effort bounded exporter, and sealed-config loading.
It imports nothing from the trading paths, never touches the network, and
exporter failures never propagate into caller decisions.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


class EventError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Canonical JSON (spec v2.2.5-v2.2.7)
# ---------------------------------------------------------------------------

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize a flat string/integer payload to canonical JSON bytes."""
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        nkey = unicodedata.normalize("NFC", key)
        if nkey in normalized:
            raise EventError(f"key collision after NFC normalization: {nkey}")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise EventError(f"field {nkey} must be string or integer")
        if isinstance(value, int) and value < 0:
            raise EventError(f"field {nkey} must be a non-negative integer")
        normalized[nkey] = (
            unicodedata.normalize("NFC", value) if isinstance(value, str) else value
        )
    parts = []
    for key in sorted(normalized, key=lambda k: k.encode("utf-8")):
        value = normalized[key]
        rendered = _escape_string(value) if isinstance(value, str) else str(value)
        parts.append(f"{_escape_string(key)}:{rendered}")
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def parse_canonical(raw: bytes) -> dict[str, Any]:
    """Parse canonical JSON bytes, rejecting non-canonical encodings."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EventError("payload is not valid UTF-8") from exc
    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise EventError(f"duplicate key: {key}")
            obj[key] = value
        return obj

    try:
        parsed = json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise EventError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise EventError("payload must be a JSON object")
    if canonical_bytes(parsed) != raw:
        raise EventError("payload bytes are not canonical")
    return parsed


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULER_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def _validate_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise EventError(f"field {field} must be a YYYY-MM-DDTHH:MM:SSZ timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EventError(f"field {field} is not a valid calendar timestamp") from exc
    return value


def _validate_hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise EventError(f"field {field} must be 64 lowercase hex characters")
    return value


def _validate_int(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EventError(f"field {field} must be an integer >= {minimum}")
    return value


# ---------------------------------------------------------------------------
# Typed event schemas (allowlist, spec v2.2.7)
# ---------------------------------------------------------------------------

HEARTBEAT_FIELDS = (
    "payload_version",
    "scheduler_id",
    "key_id",
    "key_epoch",
    "scheduler_sequence",
    "schedule_hash",
    "issued_at_utc",
    "window_start_utc",
    "window_end_utc",
)

REGISTRY_COMMON_FIELDS = ("event_type", "registry_seq", "prev_registry_hash", "issued_at_utc")
REGISTRY_EVENT_FIELDS = {
    "register": ("scheduler_id", "key_id", "key_epoch", "public_key"),
    "rotate": ("scheduler_id", "old_key_id", "new_key_id", "new_key_epoch", "public_key"),
    "revoke": ("scheduler_id", "key_id", "reason_code"),
}
REVOKE_REASONS = ("compromised", "retired", "superseded")


def _check_exact_fields(payload: Mapping[str, Any], expected: Sequence[str]) -> None:
    if set(payload) != set(expected):
        raise EventError(
            f"fields must be exactly {sorted(expected)}, got {sorted(payload)}"
        )


def validate_heartbeat(payload: Mapping[str, Any]) -> dict[str, Any]:
    _check_exact_fields(payload, HEARTBEAT_FIELDS)
    if (
        isinstance(payload["payload_version"], bool)
        or not isinstance(payload["payload_version"], int)
        or payload["payload_version"] != 1
    ):
        raise EventError("payload_version must be 1")
    if not isinstance(payload["scheduler_id"], str) or not _SCHEDULER_ID_RE.match(
        payload["scheduler_id"]
    ):
        raise EventError("scheduler_id must match [a-z0-9-]{1,64}")
    _validate_hex64(payload["key_id"], "key_id")
    _validate_int(payload["key_epoch"], "key_epoch", 1)
    _validate_int(payload["scheduler_sequence"], "scheduler_sequence", 1)
    _validate_hex64(payload["schedule_hash"], "schedule_hash")
    _validate_timestamp(payload["issued_at_utc"], "issued_at_utc")
    start = _validate_timestamp(payload["window_start_utc"], "window_start_utc")
    end = _validate_timestamp(payload["window_end_utc"], "window_end_utc")
    if end <= start:
        raise EventError("window_end_utc must be after window_start_utc")
    return dict(payload)


def validate_registry_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    event_type = payload.get("event_type")
    if event_type not in REGISTRY_EVENT_FIELDS:
        raise EventError(f"unknown registry event_type: {event_type}")
    _check_exact_fields(
        payload, tuple(REGISTRY_COMMON_FIELDS) + REGISTRY_EVENT_FIELDS[event_type]
    )
    _validate_int(payload["registry_seq"], "registry_seq", 1)
    _validate_hex64(payload["prev_registry_hash"], "prev_registry_hash")
    _validate_timestamp(payload["issued_at_utc"], "issued_at_utc")
    if not isinstance(payload["scheduler_id"], str) or not _SCHEDULER_ID_RE.match(
        payload["scheduler_id"]
    ):
        raise EventError("scheduler_id must match [a-z0-9-]{1,64}")
    if event_type == "register":
        _validate_hex64(payload["key_id"], "key_id")
        _validate_int(payload["key_epoch"], "key_epoch", 1)
        _validate_public_key(payload["public_key"], payload["key_id"])
    elif event_type == "rotate":
        _validate_hex64(payload["old_key_id"], "old_key_id")
        _validate_hex64(payload["new_key_id"], "new_key_id")
        _validate_int(payload["new_key_epoch"], "new_key_epoch", 2)
        _validate_public_key(payload["public_key"], payload["new_key_id"])
    else:
        _validate_hex64(payload["key_id"], "key_id")
        if payload["reason_code"] not in REVOKE_REASONS:
            raise EventError(f"reason_code must be one of {REVOKE_REASONS}")
    return dict(payload)


def _validate_public_key(value: Any, key_id: str) -> bytes:
    if not isinstance(value, str) or len(value) != 43:
        raise EventError("public_key must be 43-char canonical base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except Exception as exc:
        raise EventError("public_key is not valid base64url") from exc
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise EventError("public_key is not canonical base64url")
    if hashlib.sha256(raw).hexdigest() != key_id:
        raise EventError("key_id must be the SHA-256 of the public key")
    try:
        point = _decode_point(raw)
    except Exception as exc:
        raise EventError("public_key is not a valid curve point") from exc
    if not _is_valid_subgroup_point(point):
        raise EventError("public_key is identity or small-order")
    return raw


# ---------------------------------------------------------------------------
# Ed25519 (RFC 8032, pure Python)
# ---------------------------------------------------------------------------

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = ((y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q)) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if (x * x - xx) % _Q != 0:
        raise EventError("point is not on the Ed25519 curve")
    if x % 2 != 0:
        x = _Q - x
    return x % _Q


_BY = (4 * pow(5, _Q - 2, _Q)) % _Q
_BX = _xrecover(_BY)
_B = (_BX, _BY)


def _edwards(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    denom = pow(1 + _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    x3 = (x1 * y2 + x2 * y1) * denom % _Q
    denom = pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    y3 = (y1 * y2 + x1 * x2) * denom % _Q
    return x3, y3


def _scalarmult(p: tuple[int, int], e: int) -> tuple[int, int]:
    if e == 0:
        return 0, 1
    q = _scalarmult(p, e // 2)
    q = _edwards(q, q)
    if e & 1:
        q = _edwards(q, p)
    return q


def _encode_point(p: tuple[int, int]) -> bytes:
    x, y = p[0] % _Q, p[1] % _Q
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _decode_point(raw: bytes) -> tuple[int, int]:
    y = int.from_bytes(raw, "little") & (2**255 - 1)
    if y >= _Q:
        raise EventError("non-canonical point encoding")
    sign = (raw[31] >> 7) & 1
    x = _xrecover(y)
    if x == 0 and sign == 1:
        raise EventError("non-canonical point encoding: x=0 with sign bit set")
    if (x & 1) != sign:
        x = (_Q - x) % _Q
    point = (x % _Q, y % _Q)
    if _encode_point(point) != raw:
        raise EventError("point does not re-encode canonically")
    return point


_IDENTITY = (0, 1)


def _is_valid_subgroup_point(point: tuple[int, int]) -> bool:
    """Reject identity and small-order points; require prime-order subgroup."""
    x, y = point[0] % _Q, point[1] % _Q
    if (y * y - x * x - 1 - _D * x * x * y * y) % _Q != 0:
        return False
    normalized = (x, y)
    if normalized == _IDENTITY:
        return False
    return _scalarmult(normalized, _L) == _IDENTITY


def _hint(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def ed25519_public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise EventError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    return _encode_point(_scalarmult(_B, a))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise EventError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    public = _encode_point(_scalarmult(_B, a))
    r = _hint(h[32:64] + message) % _L
    r_point = _encode_point(_scalarmult(_B, r))
    s = (r + _hint(r_point + public + message) * a) % _L
    return r_point + s.to_bytes(32, "little")


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        r_point = _decode_point(signature[:32])
        a_point = _decode_point(public_key)
    except Exception:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    if not _is_valid_subgroup_point(a_point) or not _is_valid_subgroup_point(r_point):
        return False
    h = _hint(signature[:32] + public_key + message) % _L
    return _scalarmult(_B, s) == _edwards(r_point, _scalarmult(a_point, h))


HEARTBEAT_DOMAIN = b"NEBULA-SCHEDULER-HEARTBEAT-V1"


def heartbeat_digest(payload: Mapping[str, Any]) -> bytes:
    return hashlib.sha256(HEARTBEAT_DOMAIN + b"\x00" + canonical_bytes(payload)).digest()


def sign_heartbeat(seed: bytes, payload: Mapping[str, Any]) -> str:
    validate_heartbeat(payload)
    signature = ed25519_sign(seed, heartbeat_digest(payload))
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def verify_heartbeat(public_key: bytes, payload: Mapping[str, Any], signature_b64: str) -> bool:
    validate_heartbeat(payload)
    if len(signature_b64) != 86:
        return False
    try:
        signature = base64.urlsafe_b64decode(signature_b64 + "==")
    except Exception:
        return False
    if base64.urlsafe_b64encode(signature).rstrip(b"=").decode() != signature_b64:
        return False
    return ed25519_verify(public_key, heartbeat_digest(payload), signature)


# ---------------------------------------------------------------------------
# Anti-replay sequence store (single-writer, atomic, fail-closed)
# ---------------------------------------------------------------------------


class SequenceStore:
    """Durable monotonic counter; a second writer is refused."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._lock_path = self._dir / "sequence.lock"
        self._counter_path = self._dir / "sequence.counter"
        self._lock_file = open(self._lock_path, "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise EventError("sequence store already has a writer") from exc
        self._value = self._load()

    def _load(self) -> int:
        if not self._counter_path.exists():
            return 0
        text = self._counter_path.read_text(encoding="utf-8").strip()
        value_text, _, checksum = text.partition(":")
        if hashlib.sha256(value_text.encode()).hexdigest() != checksum:
            raise EventError("sequence counter checksum mismatch")
        return int(value_text)

    def next(self) -> int:
        value = self._value + 1
        self._persist(value)
        return value

    def _persist(self, value: int) -> None:
        content = f"{value}:{hashlib.sha256(str(value).encode()).hexdigest()}"
        tmp = self._dir / "sequence.counter.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._counter_path)
        dir_fd = os.open(self._dir, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self._value = value

    def check_replay(self, sequence: int) -> bool:
        """True when the sequence is strictly newer than the stored value."""
        return sequence > self._value

    def accept_sequence(self, sequence: int) -> bool:
        """Atomically accept and persist an incoming sequence (anti-replay)."""
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            return False
        if sequence <= self._value:
            return False
        self._persist(sequence)
        return True

    def close(self) -> None:
        self._lock_file.close()


# ---------------------------------------------------------------------------
# Best-effort bounded exporter (flag off by default)
# ---------------------------------------------------------------------------


class ResearchExporter:
    """Writes canonical events to a JSONL outbox; failures never raise."""

    def __init__(
        self,
        outbox_path: str | Path,
        enabled: bool = False,
        capacity: int = 1000,
        max_bytes: int = 1 << 20,
    ) -> None:
        self._path = Path(outbox_path)
        self._enabled = enabled
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._count = 0
        self._bytes = 0
        if self._path.exists():
            data = self._path.read_bytes()
            self._bytes = len(data)
            self._count = data.count(b"\n")
        self.dropped = 0

    def export(self, payload: Mapping[str, Any]) -> bool:
        if not self._enabled:
            return False
        try:
            line = canonical_bytes(payload) + b"\n"
        except Exception:
            self.dropped += 1
            return False
        if self._count >= self._capacity or self._bytes + len(line) > self._max_bytes:
            self.dropped += 1
            return False
        try:
            with open(self._path, "ab") as handle:
                handle.write(line)
            self._count += 1
            self._bytes += len(line)
            return True
        except Exception:
            self.dropped += 1
            return False


# ---------------------------------------------------------------------------
# Sealed config loading and merger (P2-03 acceptance surface)
# ---------------------------------------------------------------------------


def load_sealed_config(fd: int = 3) -> dict[str, Any]:
    """Load config only from the sealed descriptor handed over at exec."""
    try:
        raw = os.read(fd, 1 << 20)
    except OSError as exc:
        raise EventError(f"sealed config descriptor {fd} unavailable") from exc
    try:
        config = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise EventError("sealed config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise EventError("sealed config must be a JSON object")
    return config


def merge_config(
    sources: Sequence[Mapping[str, Any]],
    schema: Mapping[str, type],
    schema_version: int,
) -> dict[str, Any]:
    """Merge config sources against an allowlist schema, fail-closed."""
    merged: dict[str, Any] = {}
    for source in sources:
        version = source.get("config_schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != schema_version:
            raise EventError("unknown config_schema_version")
        for key, value in source.items():
            if key == "config_schema_version":
                continue
            if key not in schema:
                raise EventError(f"unknown config key: {key}")
            if key in merged:
                raise EventError(f"duplicate config override: {key}")
            expected = schema[key]
            if isinstance(value, bool) and expected is not bool:
                raise EventError(f"config key {key} has wrong type")
            if not isinstance(value, expected):
                raise EventError(f"config key {key} has wrong type")
            merged[key] = value
    return merged
