"""Offline contract tests for complete, paced OKX account-bills pagination."""
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import parse_qs, urlparse
import fcntl

import pytest

from nebula.bill_reconciliation import CompleteAccountBillsSnapshot
from nebula.config import Settings
from nebula.exchange import OkxDemoClient, OkxError


class Response:
    def __init__(self, data, *, status_code=200, headers=None, code="0"):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.code = code

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": self.code,
            "msg": "rate limit" if self.code == "50011" else "",
            "data": self.data,
        }


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        return self.responses.pop(0)


def make_client(*pages, rate_limit_path):
    settings = Settings(
        api_key="demo-api-key",
        secret_key="demo-secret-key",
        passphrase="demo-passphrase",
    )
    responses = [
        page if isinstance(page, Response) else Response(page)
        for page in pages
    ]
    session = RecordingSession(*responses)
    client = OkxDemoClient(
        settings, session=session,
        private_read_rate_limit_path=rate_limit_path,
    )
    return client, session


def current_window():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return str(now - 60_000), str(now)


def fake_monotonic(monkeypatch, start=1000.0):
    clock = [float(start)]
    delays = []

    def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr("nebula.exchange.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("nebula.exchange.time.sleep", sleep)
    return clock, delays


def test_account_bills_pagination_uses_after_cursor_and_collects_terminal_page(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    begin, end = current_window()
    client, session = make_client(
        [{"billId": "103"}, {"billId": "102"}],
        [{"billId": "101"}, {"billId": "100"}],
        [{"billId": "99"}],
        rate_limit_path=tmp_path / "bills.lock",
    )

    rows = client.get_account_bills(begin=begin, end=end, limit=2)

    assert isinstance(rows, CompleteAccountBillsSnapshot)
    assert rows.begin_ms == int(begin)
    assert rows.end_ms == int(end)
    assert rows.page_count == 3
    assert rows.terminal_page_seen is True
    assert [row["billId"] for row in rows] == ["103", "102", "101", "100", "99"]
    with pytest.raises(TypeError):
        rows[0]["billId"] = "999"
    assert len(session.calls) == 3
    for index, (_, kwargs) in enumerate(session.calls):
        method, url = session.calls[index][0][:2]
        query = parse_qs(urlparse(url).query)
        assert method == "GET"
        assert url.startswith("https://openapi.okx.com/api/v5/account/bills?")
        assert query["limit"] == ["2"]
        assert query["begin"] == [begin]
        assert query["end"] == [end]
        assert kwargs["headers"]["x-simulated-trading"] == "1"
        assert kwargs["allow_redirects"] is False
    assert "after" not in parse_qs(urlparse(session.calls[0][0][1]).query)
    assert parse_qs(urlparse(session.calls[1][0][1]).query)["after"] == ["102"]
    assert parse_qs(urlparse(session.calls[2][0][1]).query)["after"] == ["100"]


def test_account_bills_exact_multiple_requires_and_accepts_empty_terminal_page(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    begin, end = current_window()
    client, session = make_client(
        [{"billId": "20"}, {"billId": "19"}],
        [],
        rate_limit_path=tmp_path / "bills.lock",
    )

    rows = client.get_account_bills(begin=begin, end=end, limit=2)

    assert isinstance(rows, CompleteAccountBillsSnapshot)
    assert rows.page_count == 2
    assert rows.terminal_page_seen is True
    assert [row["billId"] for row in rows] == ["20", "19"]
    assert len(session.calls) == 2
    assert parse_qs(urlparse(session.calls[1][0][1]).query)["after"] == ["19"]


def test_account_bills_rejects_duplicate_or_non_older_next_page(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    begin, end = current_window()
    client, _ = make_client(
        [{"billId": "20"}, {"billId": "19"}],
        [{"billId": "19"}],
        rate_limit_path=tmp_path / "bills.lock",
    )

    with pytest.raises(OkxError, match="duplicate billId|after cursor"):
        client.get_account_bills(begin=begin, end=end, limit=2)


def test_account_bills_rejects_non_descending_or_non_numeric_bill_ids(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    begin, end = current_window()
    client, _ = make_client(
        [{"billId": "19"}, {"billId": "20"}],
        rate_limit_path=tmp_path / "bills.lock",
    )

    with pytest.raises(OkxError, match="descending"):
        client.get_account_bills(begin=begin, end=end, limit=2)

    client, _ = make_client(
        [{"billId": "not-a-number"}],
        rate_limit_path=tmp_path / "other.lock",
    )
    with pytest.raises(OkxError, match="invalid"):
        client.get_account_bills(begin=begin, end=end, limit=2)


def test_account_bills_rejects_window_outside_seven_day_retention_before_http(tmp_path):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    begin = str(now - 7 * 24 * 60 * 60 * 1000 - 1)
    client, session = make_client([], rate_limit_path=tmp_path / "bills.lock")

    with pytest.raises(ValueError, match="last 7 days"):
        client.get_account_bills(begin=begin, end=str(now), limit=2)

    assert session.calls == []


def test_account_bills_rejects_invalid_page_limits_before_http(tmp_path):
    begin, end = current_window()
    for index, limit in enumerate((True, 0, 101, 1.5, "2")):
        client, session = make_client(
            [], rate_limit_path=tmp_path / f"invalid-{index}.lock",
        )
        with pytest.raises(ValueError, match="integer between 1 and 100"):
            client.get_account_bills(begin=begin, end=end, limit=limit)
        assert session.calls == []


def test_account_bills_page_cap_fails_closed_without_claiming_completeness(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    begin, end = current_window()
    pages = ([{"billId": str(10_000 - index)}] for index in range(100))
    client, session = make_client(
        *pages, rate_limit_path=tmp_path / "bills.lock",
    )

    with pytest.raises(OkxError, match="safety page limit"):
        client.get_account_bills(begin=begin, end=end, limit=1)

    assert len(session.calls) == 100
    assert session.calls[-1][0][1].endswith("after=9902")


def test_account_bills_rate_limit_is_shared_between_client_instances(tmp_path, monkeypatch):
    clock, delays = fake_monotonic(monkeypatch, start=2000.0)
    lock_path = tmp_path / "shared" / "bills.lock"
    first, first_session = make_client([], rate_limit_path=lock_path)
    second, second_session = make_client([], rate_limit_path=lock_path)

    first.get_account_bills()
    second.get_account_bills()

    assert len(first_session.calls) == len(second_session.calls) == 1
    assert delays == pytest.approx([0.21], abs=1e-6)
    assert clock[0] == pytest.approx(2000.21)


def test_account_bills_retries_http_429_using_retry_after_seconds(tmp_path, monkeypatch):
    clock, delays = fake_monotonic(monkeypatch, start=3000.0)
    client, session = make_client(
        Response(
            [], status_code=429,
            headers={"Content-Type": "application/json", "Retry-After": "2"},
        ),
        [],
        rate_limit_path=tmp_path / "bills.lock",
    )

    rows = client.get_account_bills(total_timeout=5)

    assert list(rows) == []
    assert len(session.calls) == 2
    assert delays == [2.0]
    assert clock[0] == pytest.approx(3002.0)


def test_account_bills_retries_okx_50011_using_retry_after_http_date(tmp_path, monkeypatch):
    clock, delays = fake_monotonic(monkeypatch, start=4000.0)
    monkeypatch.setattr("nebula.exchange.time.time", lambda: 1000.0)
    retry_date = formatdate(1004, usegmt=True)
    client, session = make_client(
        Response(
            [], headers={"Content-Type": "application/json", "Retry-After": retry_date},
            code="50011",
        ),
        [],
        rate_limit_path=tmp_path / "bills.lock",
    )

    rows = client.get_account_bills(total_timeout=6)

    assert list(rows) == []
    assert len(session.calls) == 2
    assert delays == [4.0]
    assert clock[0] == pytest.approx(4004.0)


def test_account_bills_does_not_retry_early_when_retry_after_exceeds_deadline(tmp_path, monkeypatch):
    _, delays = fake_monotonic(monkeypatch, start=5000.0)
    client, session = make_client(
        Response(
            [], status_code=429,
            headers={"Content-Type": "application/json", "Retry-After": "60"},
        ),
        [],
        rate_limit_path=tmp_path / "bills.lock",
    )

    with pytest.raises(OkxError, match="exceeds the remaining total deadline"):
        client.get_account_bills(total_timeout=1)

    assert len(session.calls) == 1
    assert delays == []


def test_account_bills_only_retries_rate_limit_errors_not_other_http_errors(tmp_path, monkeypatch):
    fake_monotonic(monkeypatch)
    client, session = make_client(
        Response([], status_code=503),
        rate_limit_path=tmp_path / "bills.lock",
    )

    with pytest.raises(OkxError, match="HTTP 503"):
        client.get_account_bills()

    assert len(session.calls) == 1


def test_account_bills_rejects_invalid_total_timeout_before_http(tmp_path):
    for index, timeout in enumerate((True, 0, -1, float("inf"), float("nan"))):
        client, session = make_client(
            [], rate_limit_path=tmp_path / f"timeout-{index}.lock",
        )
        with pytest.raises(ValueError, match="total_timeout"):
            client.get_account_bills(total_timeout=timeout)
        assert session.calls == []


def test_account_bills_rate_limit_lock_contention_obeys_total_deadline(tmp_path, monkeypatch):
    from nebula.exchange import _PrivateReadRateLimiter

    clock = [6000.0]
    delays = []

    def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr("nebula.exchange.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("nebula.exchange.time.sleep", sleep)
    lock_path = tmp_path / "held.lock"
    with open(lock_path, "w", encoding="ascii") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(OkxError, match="deadline expired waiting"):
            _PrivateReadRateLimiter.wait_for_slot(
                lock_path, deadline=clock[0] + 0.09,
            )
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    assert delays
    assert sum(delays) == pytest.approx(0.09, abs=1e-6)


def test_paginated_private_get_endpoints_share_one_cross_process_limiter(tmp_path, monkeypatch):
    clock, delays = fake_monotonic(monkeypatch, start=7000.0)
    client, session = make_client(
        [], [], [],
        rate_limit_path=tmp_path / "shared-private.lock",
    )

    assert list(client.get_account_bills()) == []
    assert list(client.get_fills_history("BTC-USDT", "123456")) == []
    assert client.get_pending_orders() == []

    assert len(session.calls) == 3
    assert [call[0][1].split("?", 1)[0] for call in session.calls] == [
        "https://openapi.okx.com/api/v5/account/bills",
        "https://openapi.okx.com/api/v5/trade/fills-history",
        "https://openapi.okx.com/api/v5/trade/orders-pending",
    ]
    assert delays == pytest.approx([0.21, 0.21], abs=1e-6)
    assert clock[0] == pytest.approx(7000.42)


def test_fills_history_rejects_boolean_or_non_integer_limits_before_http(tmp_path):
    for index, limit in enumerate((True, 0, 101, 1.5, "2")):
        client, session = make_client(
            [], rate_limit_path=tmp_path / f"fills-{index}.lock",
        )
        with pytest.raises(ValueError, match="fill history limit"):
            client.get_fills_history("BTC-USDT", "123456", limit=limit)
        assert session.calls == []
