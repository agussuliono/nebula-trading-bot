"""Fase 0A research event foundation tests."""

import hashlib

import pytest

from nebula.research_events import (
    EventError,
    ResearchExporter,
    SequenceStore,
    canonical_bytes,
    ed25519_public_key,
    merge_config,
    parse_canonical,
    sign_heartbeat,
    validate_heartbeat,
    validate_registry_event,
    verify_heartbeat,
)

SEED = bytes(range(32))


def _heartbeat():
    public = ed25519_public_key(SEED)
    return {
        "payload_version": 1,
        "scheduler_id": "sched-1",
        "key_id": hashlib.sha256(public).hexdigest(),
        "key_epoch": 1,
        "scheduler_sequence": 7,
        "schedule_hash": "a" * 64,
        "issued_at_utc": "2026-11-26T00:00:00Z",
        "window_start_utc": "2026-11-26T00:00:00Z",
        "window_end_utc": "2026-11-26T01:00:00Z",
    }


def test_canonical_bytes_sorted_and_roundtrip():
    raw = canonical_bytes({"b": 1, "a": "x"})
    assert raw == b'{"a":"x","b":1}'
    assert parse_canonical(raw) == {"a": "x", "b": 1}
    with pytest.raises(EventError):
        parse_canonical(b'{"b":1,"a":"x"}')
    with pytest.raises(EventError):
        canonical_bytes({"a": -1})


def test_heartbeat_validation_rejects_unknown_field_and_bad_window():
    payload = _heartbeat()
    assert validate_heartbeat(payload)["scheduler_sequence"] == 7
    with pytest.raises(EventError):
        validate_heartbeat({**payload, "extra": 1})
    with pytest.raises(EventError):
        validate_heartbeat({**payload, "window_end_utc": "2026-11-26T00:00:00Z"})
    with pytest.raises(EventError):
        validate_heartbeat({**payload, "issued_at_utc": "2026-02-30T00:00:00Z"})


def test_heartbeat_signature_roundtrip_and_tamper():
    payload = _heartbeat()
    signature = sign_heartbeat(SEED, payload)
    assert len(signature) == 86
    assert verify_heartbeat(ed25519_public_key(SEED), payload, signature) is True
    tampered = {**payload, "scheduler_sequence": 8}
    assert verify_heartbeat(ed25519_public_key(SEED), tampered, signature) is False


def test_registry_event_key_binding():
    public = ed25519_public_key(SEED)
    import base64

    event = {
        "event_type": "register",
        "registry_seq": 1,
        "prev_registry_hash": "0" * 64,
        "issued_at_utc": "2026-11-26T00:00:00Z",
        "scheduler_id": "sched-1",
        "key_id": hashlib.sha256(public).hexdigest(),
        "key_epoch": 1,
        "public_key": base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
    }
    assert validate_registry_event(event)["event_type"] == "register"
    with pytest.raises(EventError):
        validate_registry_event({**event, "key_id": "b" * 64})


def test_sequence_store_single_writer_and_replay(tmp_path):
    store = SequenceStore(tmp_path / "seq")
    assert store.next() == 1
    assert store.next() == 2
    assert store.check_replay(2) is False
    assert store.check_replay(3) is True
    with pytest.raises(EventError):
        SequenceStore(tmp_path / "seq")
    store.close()
    reopened = SequenceStore(tmp_path / "seq")
    assert reopened.next() == 3
    reopened.close()


def test_exporter_flag_off_and_failure_is_best_effort(tmp_path):
    payload = {"event_id": "e1"}
    off = ResearchExporter(tmp_path / "out.jsonl", enabled=False)
    assert off.export(payload) is False
    assert not (tmp_path / "out.jsonl").exists()
    on = ResearchExporter(tmp_path / "out.jsonl", enabled=True, capacity=1)
    assert on.export(payload) is True
    assert on.export(payload) is False  # capacity bound
    broken = ResearchExporter(tmp_path / "missing-dir" / "out.jsonl", enabled=True)
    assert broken.export(payload) is False  # never raises


def test_merge_config_fail_closed():
    schema = {"risk": int, "label": str}
    merged = merge_config(
        [{"config_schema_version": 1, "risk": 5}, {"config_schema_version": 1, "label": "x"}],
        schema,
        1,
    )
    assert merged == {"risk": 5, "label": "x"}
    with pytest.raises(EventError):
        merge_config([{"config_schema_version": 1, "risk": 5}, {"config_schema_version": 1, "risk": 6}], schema, 1)
    with pytest.raises(EventError):
        merge_config([{"config_schema_version": 1, "unknown": 1}], schema, 1)
    with pytest.raises(EventError):
        merge_config([{"config_schema_version": 2, "risk": 5}], schema, 1)
    with pytest.raises(EventError):
        merge_config([{"config_schema_version": True, "risk": 5}], schema, 1)


def test_ed25519_rfc8032_vector_and_identity_rejection():
    from nebula.research_events import ed25519_verify

    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519_verify(public, b"", signature) is True
    identity = b"\x01" + b"\x00" * 31
    forged = identity + (0).to_bytes(32, "little")
    assert ed25519_verify(identity, b"anything", forged) is False
    alt_identity = b"\x01" + b"\x00" * 30 + b"\x80"
    forged_alt = alt_identity + (0).to_bytes(32, "little")
    assert ed25519_verify(alt_identity, b"anything", forged_alt) is False
    assert ed25519_verify(public, b"anything", forged_alt) is False


def test_alternate_identity_encoding_rejected_by_registry():
    import base64

    alt_identity = b"\x01" + b"\x00" * 30 + b"\x80"
    event = {
        "event_type": "register",
        "registry_seq": 1,
        "prev_registry_hash": "0" * 64,
        "issued_at_utc": "2026-11-26T00:00:00Z",
        "scheduler_id": "sched-1",
        "key_id": hashlib.sha256(alt_identity).hexdigest(),
        "key_epoch": 1,
        "public_key": base64.urlsafe_b64encode(alt_identity).rstrip(b"=").decode(),
    }
    with pytest.raises(EventError):
        validate_registry_event(event)


def test_accept_sequence_persists_and_rejects_replay(tmp_path):
    store = SequenceStore(tmp_path / "seq")
    assert store.accept_sequence(3) is True
    assert store.accept_sequence(3) is False
    assert store.accept_sequence(2) is False
    store.close()
    reopened = SequenceStore(tmp_path / "seq")
    assert reopened.accept_sequence(3) is False
    assert reopened.accept_sequence(4) is True
    reopened.close()


def test_canonical_string_minus_zero_and_bool_version():
    assert parse_canonical(b'{"x":"-0"}') == {"x": "-0"}
    payload = _heartbeat()
    with pytest.raises(EventError):
        validate_heartbeat({**payload, "payload_version": True})


def test_exporter_byte_bound_persists_across_restart(tmp_path):
    path = tmp_path / "out.jsonl"
    first = ResearchExporter(path, enabled=True, max_bytes=30)
    assert first.export({"event_id": "e1"}) is True
    assert first.export({"event_id": "e2"}) is False
    restarted = ResearchExporter(path, enabled=True, max_bytes=30)
    assert restarted.export({"event_id": "e3"}) is False
