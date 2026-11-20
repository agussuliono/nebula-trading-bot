from nebula.strategy import ema, closed_candles


def test_ema_seed_and_length():
    out = ema([1, 2, 3, 4, 5], 3)
    assert len(out) == 3
    assert out[0] == 2


def test_only_confirmed_candles_are_used():
    rows = [["3", "", "", "", "3", "", "", "", "0"],
            ["2", "", "", "", "2", "", "", "", "1"],
            ["1", "", "", "", "1", "", "", "", "1"]]
    assert [r[0] for r in closed_candles(rows)] == ["1", "2"]


def test_unconfirmed_newest_candle_is_excluded():
    rows = [[str(i), "", "", "", str(i + 1), "", "", "", "1"] for i in range(1, 5)]
    rows[0][8] = "0"
    # Function sorts oldest-to-newest after OKX newest-first input and drops unconfirmed row.
    assert [r[0] for r in closed_candles(list(reversed(rows)))] == ["2", "3", "4"]
