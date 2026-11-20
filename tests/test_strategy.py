import pytest

from nebula.strategy import ema, closed_candles, trend_signal


def test_ema_seed_and_length():
    out = ema([1, 2, 3, 4, 5], 3)
    assert len(out) == 3
    assert out[0] == 2


def test_only_confirmed_candles_are_used():
    rows = [["3", "3", "3.5", "2.5", "3", "1", "1", "1", "0"],
            ["2", "2", "2.5", "1.5", "2", "1", "1", "1", "1"],
            ["1", "1", "1.5", "0.5", "1", "1", "1", "1", "1"]]
    assert [r[0] for r in closed_candles(rows)] == ["1", "2"]


def test_unconfirmed_newest_candle_is_excluded():
    rows = [[str(i), str(i + 1), str(i + 1.5), str(i + 0.5), str(i + 1), "1", "1", "1", "1"] for i in range(1, 5)]
    rows[0][8] = "0"
    # Function sorts oldest-to-newest after OKX newest-first input and drops unconfirmed row.
    assert [r[0] for r in closed_candles(list(reversed(rows)))] == ["2", "3", "4"]



def test_closed_candles_reject_duplicate_or_wrong_order_timestamps():
    row = ["1000", "10", "11", "9", "10", "1", "1", "1", "1"]
    with pytest.raises(ValueError, match="newest-first"):
        closed_candles([row, row])
    older = ["999", "10", "11", "9", "10", "1", "1", "1", "1"]
    with pytest.raises(ValueError, match="newest-first"):
        closed_candles([older, row])


def test_closed_candles_reject_invalid_ohlc_and_confirmation_flag():
    bad_ohlc = ["1000", "10", "9", "8", "10", "1", "1", "1", "1"]
    with pytest.raises(ValueError, match="OHLC"):
        closed_candles([bad_ohlc])
    bad_flag = ["1000", "10", "11", "9", "10", "1", "1", "1", "2"]
    with pytest.raises(ValueError, match="confirmation"):
        closed_candles([bad_flag])


def test_trend_signal_validates_ema_periods():
    with pytest.raises(ValueError, match="EMA periods"):
        trend_signal([], fast=50, slow=20)
    with pytest.raises(ValueError, match="EMA periods"):
        trend_signal([], fast=True, slow=50)


def test_ema_rejects_nonfinite_or_nonpositive_prices():
    with pytest.raises(ValueError, match="finite positive"):
        ema([1, float("nan"), 2], 2)
    with pytest.raises(ValueError, match="finite positive"):
        ema([1, 0, 2], 2)
