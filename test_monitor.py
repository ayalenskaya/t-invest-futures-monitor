from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from monitor import CandlePoint, analyze, atr, ema, format_snapshot, rsi


def series(*, breakout: str | None = None, volume: int = 100) -> list[CandlePoint]:
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    result: list[CandlePoint] = []
    for index in range(30):
        close = 100 + index * 0.05
        result.append(
            CandlePoint(
                time=start + timedelta(minutes=index * 5),
                open=close - 0.02,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=100,
                is_complete=True,
            )
        )
    last = result[-1]
    if breakout == "up":
        result[-1] = CandlePoint(last.time, last.open, 103.0, last.low, 103.0, volume, True)
    elif breakout == "down":
        result[-1] = CandlePoint(last.time, last.open, last.high, 97.0, 97.0, volume, True)
    return result


def confirmed_long_series() -> list[CandlePoint]:
    start = datetime(2026, 9, 2, tzinfo=timezone.utc)
    result: list[CandlePoint] = []
    close = 100.0
    changes = (0.08, 0.08, -0.10)
    for index in range(30):
        if index:
            close += changes[(index - 1) % len(changes)]
        result.append(
            CandlePoint(
                time=start + timedelta(minutes=index * 5),
                open=close - 0.03,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                volume=100,
                is_complete=True,
            )
        )
    last = result[-1]
    result[-1] = CandlePoint(
        last.time,
        last.open,
        100.85,
        last.low,
        100.80,
        200,
        True,
    )
    return result


class IndicatorTests(unittest.TestCase):
    def test_ema_tracks_latest_values(self) -> None:
        self.assertGreater(ema([1, 2, 3, 4, 5], 3), 3)

    def test_rsi_is_neutral_for_flat_series(self) -> None:
        self.assertEqual(rsi([10.0] * 20), 50.0)

    def test_atr_is_positive(self) -> None:
        self.assertGreater(atr(series()), 0)

    def test_upward_breakout_creates_alert(self) -> None:
        snapshot = analyze("TEST", series(breakout="up", volume=200), live_price=103.0)
        self.assertIn("ПРОБОЙ ВВЕРХ", snapshot.alerts)

    def test_confirmed_setup_has_clear_plan(self) -> None:
        snapshot = analyze("TEST", confirmed_long_series(), live_price=100.80)
        self.assertEqual(snapshot.decision, "LONG")
        self.assertIn("ЛОНГ-КАНДИДАТ", format_snapshot(snapshot))
        self.assertIsNotNone(snapshot.stop)
        self.assertIsNotNone(snapshot.target)

    def test_forming_breakout_is_not_a_signal(self) -> None:
        candles = series()
        last = candles[-1]
        candles[-1] = CandlePoint(last.time, last.open, 103.0, last.low, 103.0, 300, False)
        snapshot = analyze("TEST", candles, live_price=103.0)
        self.assertEqual(snapshot.decision, "WAIT")

    def test_quiet_market_has_no_alert(self) -> None:
        snapshot = analyze("TEST", series(), live_price=None)
        self.assertEqual(snapshot.alerts, ())
        self.assertEqual(snapshot.decision, "WAIT")


if __name__ == "__main__":
    unittest.main()
