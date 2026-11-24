from nebula.scanner import (build_candidates, candle_range_volatility, rank_candidates,
                             valid_traded_candle_count, MarketCandidate)


def test_build_candidates_uses_usdt_live_spot_and_quote_volume():
    instruments = [
        {"instId":"BTC-USDT", "state":"live", "quoteCcy":"USDT", "listTime":"1000"},
        {"instId":"ETH-USDT", "state":"suspend", "quoteCcy":"USDT", "listTime":"1000"},
        {"instId":"BTC-USDC", "state":"live", "quoteCcy":"USDC", "listTime":"1000"},
    ]
    tickers = [{"instId":"BTC-USDT", "bidPx":"99", "askPx":"101", "bidSz":"10", "askSz":"10", "volCcy24h":"100000", "ts":str(86_400_000 * 10 - 1000)}]
    got = build_candidates(instruments, tickers, now_ms=86_400_000 * 10)
    assert len(got) == 1
    assert got[0].inst_id == "BTC-USDT"
    assert got[0].quote_volume == 100000


def test_rank_rejects_nonfinite_and_applies_filters():
    items = [MarketCandidate("BTC-USDT", 1000, .1, 1.0, 100, 30),
             MarketCandidate("BAD-USDT", float("nan"), .1, 1.0, 100, 30)]
    got = rank_candidates(items, min_quote_volume=100, max_spread_pct=.5,
                          min_volatility_pct=.2, max_volatility_pct=3,
                          min_listing_age_days=30, min_data_points=20)
    assert [x.inst_id for x in got] == ["BTC-USDT"]


def test_candle_volatility_ignores_unconfirmed():
    rows = [
        ["3", "10", "12", "9", "10", "1", "10", "10", "1"],
        ["2", "15", "20", "10", "15", "1", "15", "15", "0"],
        ["1", "10", "11", "9", "10", "1", "10", "10", "1"],
    ]
    value, count = candle_range_volatility(rows, window=10)
    assert count == 2
    assert value == 25


def test_rank_uses_volatility_listing_age_and_data_quality_tiebreakers():
    items = [
        MarketCandidate("AA-USDT", 1000, .1, .2, 100, 80),
        MarketCandidate("BB-USDT", 1000, .1, 1.0, 200, 90),
        MarketCandidate("CC-USDT", 1000, .1, 1.0, 100, 90),
    ]
    got = rank_candidates(items, min_quote_volume=100, max_spread_pct=.5,
                          min_volatility_pct=.2, max_volatility_pct=1.8,
                          min_listing_age_days=30, min_data_points=50)
    # Midpoint volatility is 1.0%; among equally liquid/tight candidates,
    # B wins on listing age, then data completeness is the next tie-break.
    assert [x.inst_id for x in got] == ["BB-USDT", "CC-USDT", "AA-USDT"]


def test_volatility_uses_true_range_and_ignores_zero_volume_and_unconfirmed():
    rows = [
        ["4", "12", "13", "11", "12", "1", "12", "12", "1"],
        ["3", "10", "10", "10", "10", "0", "0", "0", "1"],
        ["2", "15", "16", "14", "15", "1", "15", "15", "0"],
        ["1", "10", "11", "9", "10", "1", "10", "10", "1"],
    ]
    value, count = candle_range_volatility(rows, window=10)
    # Candle 1: 20%; candle 4: gap true-range 3 / close 12 = 25%.
    assert count == 2
    assert value == (20.0 + 25.0) / 2


def test_volatility_rejects_invalid_window():
    import pytest
    with pytest.raises(ValueError):
        candle_range_volatility([], window=0)



def test_build_candidates_rejects_stale_future_and_duplicate_tickers():
    now = 10_000_000_000
    instrument = {"instId":"BTC-USDT", "instType":"SPOT", "state":"live",
                  "quoteCcy":"USDT", "listTime":str(now - 90 * 86_400_000)}
    base = {"instId":"BTC-USDT", "bidPx":"99", "askPx":"100",
            "bidSz":"5", "askSz":"5", "volCcy24h":"1000000", "ts":str(now - 1000)}
    assert len(build_candidates([instrument], [base], now_ms=now)) == 1
    stale = {**base, "ts":str(now - 31_000)}
    future = {**base, "ts":str(now + 1)}
    assert build_candidates([instrument], [stale], now_ms=now) == []
    assert build_candidates([instrument], [future], now_ms=now) == []
    assert build_candidates([instrument], [base, base], now_ms=now) == []


def test_build_candidates_rejects_future_listing_and_nonfinite_market_fields():
    now = 10_000_000_000
    future_listing = {"instId":"BTC-USDT", "instType":"SPOT", "state":"live",
                      "quoteCcy":"USDT", "listTime":str(now + 1)}
    ticker = {"instId":"BTC-USDT", "bidPx":"99", "askPx":"100",
              "bidSz":"5", "askSz":"5", "volCcy24h":"1000000", "ts":str(now)}
    assert build_candidates([future_listing], [ticker], now_ms=now) == []
    bad = {**ticker, "volCcy24h":"nan"}
    assert build_candidates([{"instId":"BTC-USDT", "instType":"SPOT", "state":"live",
                              "quoteCcy":"USDT", "listTime":str(now - 1000)}],
                            [bad], now_ms=now) == []



def test_valid_traded_candle_count_excludes_unconfirmed_zero_and_invalid_volume():
    rows = [
        ["5", "12", "13", "11", "12", "1", "12", "12", "1"],
        ["4", "12", "13", "11", "12", "0", "0", "0", "1"],
        ["3", "12", "13", "11", "12", "nan", "0", "0", "1"],
        ["2", "12", "13", "11", "12", "1", "12", "12", "0"],
        ["1", "12", "13", "11", "12", "1", "12", "12", "1"],
    ]
    assert valid_traded_candle_count(rows) == 2
