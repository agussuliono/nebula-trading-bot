"""Offline contract tests for complete, bounded OKX account-bills pagination."""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from nebula.config import Settings
from nebula.exchange import OkxDemoClient, OkxError


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": "0", "msg": "", "data": self.data}


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        return self.responses.pop(0)


def make_client(*pages):
    settings = Settings(
        api_key="demo-api-key",
        secret_key="demo-secret-key",
        passphrase="demo-passphrase",
    )
    session = RecordingSession(*(Response(page) for page in pages))
    return OkxDemoClient(settings, session=session), session


def current_window():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return str(now - 60_000), str(now)


def test_account_bills_pagination_uses_after_cursor_and_collects_terminal_page():
    begin, end = current_window()
    client, session = make_client(
        [{"billId": "103"}, {"billId": "102"}],
        [{"billId": "101"}, {"billId": "100"}],
        [{"billId": "99"}],
    )

    rows = client.get_account_bills(begin=begin, end=end, limit=2)

    assert [row["billId"] for row in rows] == ["103", "102", "101", "100", "99"]
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


def test_account_bills_exact_multiple_requires_and_accepts_empty_terminal_page():
    begin, end = current_window()
    client, session = make_client(
        [{"billId": "20"}, {"billId": "19"}],
        [],
    )

    rows = client.get_account_bills(begin=begin, end=end, limit=2)

    assert [row["billId"] for row in rows] == ["20", "19"]
    assert len(session.calls) == 2
    assert parse_qs(urlparse(session.calls[1][0][1]).query)["after"] == ["19"]


def test_account_bills_rejects_duplicate_or_non_older_next_page():
    begin, end = current_window()
    client, _ = make_client(
        [{"billId": "20"}, {"billId": "19"}],
        [{"billId": "19"}],
    )

    with pytest.raises(OkxError, match="duplicate billId|after cursor"):
        client.get_account_bills(begin=begin, end=end, limit=2)


def test_account_bills_rejects_non_descending_or_non_numeric_bill_ids():
    begin, end = current_window()
    client, _ = make_client([{"billId": "19"}, {"billId": "20"}])

    with pytest.raises(OkxError, match="descending"):
        client.get_account_bills(begin=begin, end=end, limit=2)

    client, _ = make_client([{"billId": "not-a-number"}])
    with pytest.raises(OkxError, match="invalid"):
        client.get_account_bills(begin=begin, end=end, limit=2)


def test_account_bills_rejects_window_outside_seven_day_retention_before_http():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    begin = str(now - 7 * 24 * 60 * 60 * 1000 - 1)
    client, session = make_client([])

    with pytest.raises(ValueError, match="last 7 days"):
        client.get_account_bills(begin=begin, end=str(now), limit=2)

    assert session.calls == []


def test_account_bills_rejects_invalid_page_limits_before_http():
    begin, end = current_window()
    for limit in (True, 0, 101, 1.5, "2"):
        client, session = make_client([])
        with pytest.raises(ValueError, match="integer between 1 and 100"):
            client.get_account_bills(begin=begin, end=end, limit=limit)
        assert session.calls == []


def test_account_bills_page_cap_fails_closed_without_claiming_completeness():
    begin, end = current_window()
    pages = ([{"billId": str(10_000 - index)}] for index in range(100))
    client, session = make_client(*pages)

    with pytest.raises(OkxError, match="safety page limit"):
        client.get_account_bills(begin=begin, end=end, limit=1)

    assert len(session.calls) == 100
    assert session.calls[-1][0][1].endswith("after=9901")
