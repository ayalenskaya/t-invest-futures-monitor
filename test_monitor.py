from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from monitor import (
    CandlePoint,
    EntryPlan,
    OpenPosition,
    analyze,
    assess_position,
    atr,
    create_position_plan,
    ema,
    entry_plan_status,
    format_entry_plan,
    format_position_advice,
    format_snapshot,
    rsi,
)


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


class PositionManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = OpenPosition(
            key="account:TEST",
            account_name="Тестовый счёт",
            ticker="TEST",
            instrument_id="test-uid",
            quantity=1.0,
            average_price=100.0,
        )
        raw_snapshot = analyze("TEST", series(), live_price=100.1)
        self.snapshot = replace(
            raw_snapshot,
            price=100.1,
            atr=1.0,
            rsi=55.0,
            ema_fast=101.0,
            ema_slow=100.0,
        )

    def test_new_long_position_gets_stop_and_target(self) -> None:
        plan = create_position_plan(self.position, self.snapshot)
        self.assertAlmostEqual(plan.stop, 99.25)
        self.assertAlmostEqual(plan.target, 101.5)
        self.assertEqual(assess_position(self.position, self.snapshot, plan).action, "HOLD")

    def test_long_stop_and_target_generate_exit(self) -> None:
        plan = create_position_plan(self.position, self.snapshot)
        stopped = assess_position(
            self.position, replace(self.snapshot, price=99.2), plan
        )
        targeted = assess_position(
            self.position, replace(self.snapshot, price=101.6), plan
        )
        self.assertEqual(stopped.action, "EXIT_STOP")
        self.assertEqual(targeted.action, "EXIT_TARGET")

    def test_long_reversal_generates_exit(self) -> None:
        plan = create_position_plan(self.position, self.snapshot)
        reversed_market = replace(
            self.snapshot,
            price=100.2,
            rsi=42.0,
            ema_fast=99.0,
            ema_slow=100.0,
        )
        advice = assess_position(self.position, reversed_market, plan)
        self.assertEqual(advice.action, "EXIT_REVERSAL")

    def test_profitable_move_tightens_stop(self) -> None:
        plan = create_position_plan(self.position, self.snapshot)
        advice = assess_position(
            self.position, replace(self.snapshot, price=100.8), plan
        )
        self.assertEqual(advice.action, "MOVE_STOP")
        self.assertEqual(advice.plan.stage, "BREAKEVEN")
        self.assertAlmostEqual(advice.plan.stop, 100.0)

    def test_short_position_uses_opposite_levels(self) -> None:
        short = replace(self.position, quantity=-2.0)
        short_snapshot = replace(
            self.snapshot,
            price=98.4,
            rsi=45.0,
            ema_fast=99.0,
            ema_slow=100.0,
        )
        plan = create_position_plan(short, self.snapshot)
        advice = assess_position(short, short_snapshot, plan)
        self.assertEqual(advice.action, "EXIT_TARGET")

    def test_position_message_contains_a_clear_decision(self) -> None:
        plan = create_position_plan(self.position, self.snapshot)
        advice = assess_position(self.position, self.snapshot, plan)
        message = format_position_advice(
            self.position, self.snapshot, advice, event="NEW"
        )
        self.assertIn("ПОЗИЦИЯ НАЙДЕНА", message)
        self.assertIn("РЕШЕНИЕ: ДЕРЖАТЬ", message)


class EntryManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        self.now = now
        self.plan = EntryPlan(
            ticker="TEST",
            direction="LONG",
            signal_time=now.isoformat(),
            detected_at=now.isoformat(),
            entry_low=100.0,
            entry_high=100.25,
            stop=99.0,
            target=102.0,
        )

    def test_entry_is_ready_only_inside_zone(self) -> None:
        self.assertEqual(entry_plan_status(self.plan, 100.1, 60, self.now), "READY")
        self.assertEqual(entry_plan_status(self.plan, 100.5, 60, self.now), "WAITING")

    def test_entry_plan_expires_and_cancels(self) -> None:
        later = self.now + timedelta(minutes=60)
        self.assertEqual(entry_plan_status(self.plan, 100.1, 60, later), "EXPIRED")
        self.assertEqual(entry_plan_status(self.plan, 98.9, 60, self.now), "CANCELLED")
        self.assertEqual(entry_plan_status(self.plan, 102.1, 60, self.now), "MISSED")

    def test_ready_message_says_when_to_open(self) -> None:
        message = format_entry_plan(self.plan, 100.1, "READY")
        self.assertIn("МОЖНО ОТКРЫТЬ ЛОНГ", message)
        self.assertIn("ЦЕНА В ЗОНЕ", message)


if __name__ == "__main__":
    unittest.main()
