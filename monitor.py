from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


# The pinned SDK marks its stable compatibility wrapper as deprecated even
# though it is still the documented interface used by this small monitor.
warnings.filterwarnings(
    "ignore",
    message=r".*is deprecated as of 1\.0\.0.*",
    category=DeprecationWarning,
)
from statistics import fmean
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "state.json"
LOG_FILE = ROOT / "monitor.log"


@dataclass(frozen=True)
class CandlePoint:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    is_complete: bool


@dataclass(frozen=True)
class Snapshot:
    ticker: str
    time: datetime
    price: float
    change_5m_pct: float
    volume_ratio: float
    rsi: float
    ema_fast: float
    ema_slow: float
    prior_high: float
    prior_low: float
    candle_complete: bool
    alerts: tuple[str, ...]
    decision: str
    atr: float
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    target: float | None


@dataclass(frozen=True)
class OpenPosition:
    key: str
    account_name: str
    ticker: str
    instrument_id: str
    quantity: float
    average_price: float

    @property
    def direction(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"


@dataclass(frozen=True)
class PositionPlan:
    direction: str
    entry: float
    atr: float
    stop: float
    target: float
    stage: str = "INITIAL"


@dataclass(frozen=True)
class PositionAdvice:
    action: str
    plan: PositionPlan
    reason: str


@dataclass(frozen=True)
class EntryPlan:
    ticker: str
    direction: str
    signal_time: str
    detected_at: str
    entry_low: float
    entry_high: float
    stop: float
    target: float


def load_env(path: Path = ENV_FILE) -> None:
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
        return

    required = ("TINVEST_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    if not all(os.getenv(name) for name in required):
        raise RuntimeError(
            "Не найден .env и не заданы облачные секреты. "
            "Сначала выполните настройку."
        )


def quotation_to_float(value: object) -> float:
    units = int(getattr(value, "units", 0))
    nano = int(getattr(value, "nano", 0))
    return units + nano / 1_000_000_000


def ema(values: Sequence[float], period: int) -> float:
    if not values:
        raise ValueError("Для EMA нужны значения.")
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    changes = [right - left for left, right in zip(values[-period - 1 : -1], values[-period:])]
    average_gain = fmean(max(change, 0.0) for change in changes)
    average_loss = fmean(max(-change, 0.0) for change in changes)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    strength = average_gain / average_loss
    return 100 - 100 / (1 + strength)


def atr(candles: Sequence[CandlePoint], period: int = 14) -> float:
    if len(candles) < 2:
        raise ValueError("Для ATR нужны минимум две свечи.")
    ranges: list[float] = []
    start = max(1, len(candles) - period)
    for index in range(start, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return fmean(ranges)


def analyze(ticker: str, candles: Sequence[CandlePoint], live_price: float | None = None) -> Snapshot:
    completed = [candle for candle in candles if candle.is_complete]
    if len(completed) < 22:
        raise ValueError(
            f"{ticker}: нужно минимум 22 закрытые свечи, получено {len(completed)}"
        )

    signal_candle = completed[-1]
    history = list(completed[-21:-1])
    closes = [item.close for item in completed]
    price = live_price if live_price is not None and live_price > 0 else signal_candle.close
    previous_close = history[-1].close
    change_pct = (
        (signal_candle.close / previous_close - 1) * 100 if previous_close else 0.0
    )
    average_volume = fmean(max(item.volume, 0) for item in history[-20:])
    volume_ratio = signal_candle.volume / average_volume if average_volume else 0.0
    prior_high = max(item.high for item in history[-20:])
    prior_low = min(item.low for item in history[-20:])
    ema_fast = ema(closes[-30:], 9)
    ema_slow = ema(closes[-30:], 21)
    current_rsi = rsi(closes)
    current_atr = atr(completed, 14)

    alerts: list[str] = []
    if signal_candle.close > prior_high:
        alerts.append("ПРОБОЙ ВВЕРХ")
    if signal_candle.close < prior_low:
        alerts.append("ПРОБОЙ ВНИЗ")
    if volume_ratio >= 2.5:
        alerts.append("АНОМАЛЬНЫЙ ОБЪЁМ")
    if abs(change_pct) >= 0.5 and volume_ratio >= 1.2:
        alerts.append("РЕЗКОЕ ДВИЖЕНИЕ")

    long_setup = (
        signal_candle.close > prior_high
        and ema_fast > ema_slow
        and volume_ratio >= 1.5
        and 50 <= current_rsi <= 70
    )
    short_setup = (
        signal_candle.close < prior_low
        and ema_fast < ema_slow
        and volume_ratio >= 1.5
        and 30 <= current_rsi <= 50
    )

    decision = "WAIT"
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    target: float | None = None
    if long_setup:
        decision = "LONG"
        entry_low = prior_high
        entry_high = prior_high + current_atr * 0.25
        stop = prior_high - current_atr * 0.75
        target = prior_high + current_atr * 1.75
    elif short_setup:
        decision = "SHORT"
        entry_low = prior_low - current_atr * 0.25
        entry_high = prior_low
        stop = prior_low + current_atr * 0.75
        target = prior_low - current_atr * 1.75

    return Snapshot(
        ticker=ticker,
        time=signal_candle.time,
        price=price,
        change_5m_pct=change_pct,
        volume_ratio=volume_ratio,
        rsi=current_rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        prior_high=prior_high,
        prior_low=prior_low,
        candle_complete=signal_candle.is_complete,
        alerts=tuple(alerts),
        decision=decision,
        atr=current_atr,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target=target,
    )


def format_price(value: float) -> str:
    absolute = abs(value)
    if absolute >= 10_000:
        return f"{value:.0f}"
    if absolute >= 100:
        return f"{value:.1f}"
    if absolute >= 10:
        return f"{value:.2f}"
    if absolute >= 1:
        return f"{value:.3f}"
    return f"{value:.4f}"


def format_snapshot(snapshot: Snapshot, test_message: bool = False) -> str:
    if snapshot.decision == "WAIT":
        return (
            f"⚪ ЖДАТЬ — {snapshot.ticker}\n\n"
            "Подтверждённого входа нет.\n"
            f"Цена: {format_price(snapshot.price)}\n"
            f"Объём: x{snapshot.volume_ratio:.2f} от среднего\n"
            f"RSI: {snapshot.rsi:.1f}\n\n"
            "Покупать или шортить только из-за одного индикатора не нужно."
        )

    assert snapshot.entry_low is not None
    assert snapshot.entry_high is not None
    assert snapshot.stop is not None
    assert snapshot.target is not None
    is_long = snapshot.decision == "LONG"
    heading = "🟢 ЛОНГ-КАНДИДАТ" if is_long else "🔴 ШОРТ-КАНДИДАТ"
    direction = "выше" if is_long else "ниже"
    action = "покупку" if is_long else "шорт"
    stop_direction = "ниже" if is_long else "выше"
    return (
        f"{heading} — {snapshot.ticker}\n\n"
        f"Закрытая свеча закрепилась {direction} диапазона.\n"
        f"Объём: x{snapshot.volume_ratio:.2f} от среднего\n"
        f"RSI: {snapshot.rsi:.1f}; EMA подтверждает направление\n\n"
        "ПЛАН:\n"
        f"• Ждать возврата цены в зону: "
        f"{format_price(snapshot.entry_low)} — {format_price(snapshot.entry_high)}\n"
        f"• Отмена сценария / стоп {stop_direction}: {format_price(snapshot.stop)}\n"
        f"• Ориентир цели: {format_price(snapshot.target)}\n"
        f"• Текущая цена: {format_price(snapshot.price)}\n\n"
        f"Не открывайте {action}, если цена не вернулась в зону или уже дошла до цели. "
        "Это технический сценарий, а не гарантия прибыли."
    )


def entry_plan_from_snapshot(snapshot: Snapshot) -> EntryPlan:
    if snapshot.decision not in {"LONG", "SHORT"}:
        raise ValueError("Для плана входа нужен подтверждённый LONG или SHORT.")
    assert snapshot.entry_low is not None
    assert snapshot.entry_high is not None
    assert snapshot.stop is not None
    assert snapshot.target is not None
    return EntryPlan(
        ticker=snapshot.ticker,
        direction=snapshot.decision,
        signal_time=snapshot.time.isoformat(),
        detected_at=datetime.now(timezone.utc).isoformat(),
        entry_low=snapshot.entry_low,
        entry_high=snapshot.entry_high,
        stop=snapshot.stop,
        target=snapshot.target,
    )


def entry_plan_status(
    plan: EntryPlan,
    price: float,
    lifetime_minutes: int,
    now: datetime | None = None,
) -> str:
    current_time = now or datetime.now(timezone.utc)
    try:
        detected_at = datetime.fromisoformat(plan.detected_at)
    except ValueError:
        return "EXPIRED"
    if current_time - detected_at >= timedelta(minutes=lifetime_minutes):
        return "EXPIRED"

    if plan.direction == "LONG":
        if price <= plan.stop:
            return "CANCELLED"
        if price >= plan.target:
            return "MISSED"
    else:
        if price >= plan.stop:
            return "CANCELLED"
        if price <= plan.target:
            return "MISSED"
    if plan.entry_low <= price <= plan.entry_high:
        return "READY"
    return "WAITING"


def format_entry_plan(plan: EntryPlan, price: float, status: str) -> str:
    is_long = plan.direction == "LONG"
    direction_name = "ЛОНГ" if is_long else "ШОРТ"
    action_name = "ЛОНГ" if is_long else "ШОРТ"
    if status == "READY":
        heading = (
            f"🟢 МОЖНО ОТКРЫТЬ {action_name} — {plan.ticker}"
            if is_long
            else f"🔴 МОЖНО ОТКРЫТЬ {action_name} — {plan.ticker}"
        )
        decision = f"ЦЕНА В ЗОНЕ — ОТКРЫТЬ {action_name}"
        explanation = "Подтверждённый сценарий дождался нужной цены."
    else:
        heading = f"👀 {direction_name}-СЦЕНАРИЙ — {plan.ticker}"
        decision = "ПОКА НЕ ВХОДИТЬ"
        explanation = "Движение подтверждено, но цена ещё не находится в зоне входа."
    return (
        f"{heading}\n\n"
        f"РЕШЕНИЕ: {decision}\n"
        f"{explanation}\n\n"
        f"Зона входа: {format_price(plan.entry_low)} — {format_price(plan.entry_high)}\n"
        f"Цена сейчас: {format_price(price)}\n"
        f"Защитный стоп: {format_price(plan.stop)}\n"
        f"Цель: {format_price(plan.target)}\n\n"
        "Если к моменту открытия цена уже вышла из зоны — пропустите вход. "
        "Бот не выставляет заявку автоматически."
    )


def create_position_plan(position: OpenPosition, snapshot: Snapshot) -> PositionPlan:
    entry = position.average_price if position.average_price > 0 else snapshot.price
    risk = max(snapshot.atr * 0.75, abs(entry) * 0.001)
    if position.direction == "LONG":
        stop = entry - risk
        target = entry + risk * 2
    else:
        stop = entry + risk
        target = entry - risk * 2
    return PositionPlan(
        direction=position.direction,
        entry=entry,
        atr=max(snapshot.atr, risk),
        stop=stop,
        target=target,
    )


def assess_position(
    position: OpenPosition,
    snapshot: Snapshot,
    plan: PositionPlan,
) -> PositionAdvice:
    is_long = position.direction == "LONG"
    price = snapshot.price
    hit_target = price >= plan.target if is_long else price <= plan.target
    hit_stop = price <= plan.stop if is_long else price >= plan.stop
    reversal = (
        snapshot.ema_fast < snapshot.ema_slow and snapshot.rsi <= 45
        if is_long
        else snapshot.ema_fast > snapshot.ema_slow and snapshot.rsi >= 55
    )

    if hit_target:
        return PositionAdvice("EXIT_TARGET", plan, "достигнута расчётная цель")
    if hit_stop:
        return PositionAdvice("EXIT_STOP", plan, "достигнут защитный стоп")
    if reversal:
        return PositionAdvice(
            "EXIT_REVERSAL",
            plan,
            "EMA и RSI подтвердили разворот против позиции",
        )

    favorable_move = price - plan.entry if is_long else plan.entry - price
    risk = abs(plan.entry - (plan.stop if plan.stage == "INITIAL" else plan.entry))
    if risk <= 0:
        risk = max(plan.atr * 0.75, abs(plan.entry) * 0.001)

    if favorable_move >= risk * 1.5 and plan.stage != "TRAILING":
        protected_profit = risk * 0.5
        new_stop = (
            plan.entry + protected_profit if is_long else plan.entry - protected_profit
        )
        updated = PositionPlan(
            direction=plan.direction,
            entry=plan.entry,
            atr=plan.atr,
            stop=new_stop,
            target=plan.target,
            stage="TRAILING",
        )
        return PositionAdvice(
            "MOVE_STOP",
            updated,
            "цена прошла полторы величины первоначального риска",
        )

    if favorable_move >= risk and plan.stage == "INITIAL":
        updated = PositionPlan(
            direction=plan.direction,
            entry=plan.entry,
            atr=plan.atr,
            stop=plan.entry,
            target=plan.target,
            stage="BREAKEVEN",
        )
        return PositionAdvice(
            "MOVE_STOP",
            updated,
            "цена прошла одну величину первоначального риска",
        )

    return PositionAdvice("HOLD", plan, "условий для выхода пока нет")


def format_position_advice(
    position: OpenPosition,
    snapshot: Snapshot,
    advice: PositionAdvice,
    event: str,
) -> str:
    direction_name = "ЛОНГ" if position.direction == "LONG" else "ШОРТ"
    close_name = "лонг" if position.direction == "LONG" else "шорт"
    direction_multiplier = 1 if position.direction == "LONG" else -1
    move_pct = (
        (snapshot.price / advice.plan.entry - 1) * 100 * direction_multiplier
        if advice.plan.entry
        else 0.0
    )

    if advice.action == "EXIT_TARGET":
        heading = f"✅ ЦЕЛЬ ДОСТИГНУТА — {position.ticker}"
        decision = f"ЗАКРЫТЬ {close_name.upper()} И ЗАФИКСИРОВАТЬ РЕЗУЛЬТАТ"
    elif advice.action == "EXIT_STOP":
        heading = f"🛑 СТОП ДОСТИГНУТ — {position.ticker}"
        decision = f"ЗАКРЫТЬ {close_name.upper()}, НЕ ЖДАТЬ ОТСКОКА"
    elif advice.action == "EXIT_REVERSAL":
        heading = f"🔄 РАЗВОРОТ ПРОТИВ ПОЗИЦИИ — {position.ticker}"
        decision = f"ЗАКРЫТЬ {close_name.upper()}"
    elif advice.action == "MOVE_STOP":
        heading = f"🟡 ЗАЩИТИТЬ ПОЗИЦИЮ — {position.ticker}"
        decision = f"ПЕРЕНЕСТИ СТОП НА {format_price(advice.plan.stop)}"
    elif event == "NEW":
        heading = f"📌 ПОЗИЦИЯ НАЙДЕНА — {position.ticker}"
        decision = (
            f"ДЕРЖАТЬ; ЕСЛИ СТОП ЕЩЁ НЕ СТОИТ — ПОСТАВИТЬ НА "
            f"{format_price(advice.plan.stop)}"
        )
    else:
        heading = f"🟡 ПОЗИЦИЯ ПОД КОНТРОЛЕМ — {position.ticker}"
        decision = "ДЕРЖАТЬ"

    quantity = abs(position.quantity)
    quantity_text = f"{quantity:.0f}" if quantity.is_integer() else f"{quantity:g}"
    return (
        f"{heading}\n\n"
        f"Направление: {direction_name}\n"
        f"Количество: {quantity_text}\n"
        f"Средняя цена: {format_price(advice.plan.entry)}\n"
        f"Сейчас: {format_price(snapshot.price)}\n"
        f"Движение от входа: {move_pct:+.2f}%\n\n"
        f"РЕШЕНИЕ: {decision}\n"
        f"Причина: {advice.reason}.\n\n"
        f"Защитный стоп: {format_price(advice.plan.stop)}\n"
        f"Цель: {format_price(advice.plan.target)}\n\n"
        "Бот не выставляет заявку: действие нужно выполнить в Т‑Инвестициях."
    )


def parse_chat_ids(value: str) -> list[str]:
    chat_ids = list(
        dict.fromkeys(
            item.strip()
            for item in value.replace(";", ",").split(",")
            if item.strip()
        )
    )
    if not chat_ids:
        raise RuntimeError("Список получателей Telegram пуст.")
    if any(not item.lstrip("-").isdigit() for item in chat_ids):
        raise RuntimeError("Chat ID Telegram должны быть числами через запятую.")
    return chat_ids


def telegram_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    errors: list[Exception] = []
    for recipient in parse_chat_ids(chat_id):
        payload = urllib.parse.urlencode(
            {"chat_id": recipient, "text": text}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description", "Telegram не принял сообщение")
                )
        except (urllib.error.URLError, RuntimeError) as error:
            errors.append(error)
    if errors:
        raise RuntimeError(
            f"Telegram не доставил сообщение получателям: {len(errors)}."
        ) from errors[0]


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_open_positions(
    client: object,
    instruments: dict[str, str],
) -> dict[str, list[OpenPosition]]:
    ids_to_tickers = {instrument_id: ticker for ticker, instrument_id in instruments.items()}
    watched_tickers = set(instruments)
    accounts = list(client.users.get_accounts().accounts)
    positions_by_ticker: dict[str, list[OpenPosition]] = {}
    successful_accounts = 0

    for account in accounts:
        status = getattr(getattr(account, "status", None), "name", str(getattr(account, "status", "")))
        if "CLOSED" in status:
            continue
        try:
            portfolio = client.operations.get_portfolio(account_id=account.id)
            successful_accounts += 1
        except Exception:
            logging.exception("Не удалось прочитать портфель одного из счетов")
            continue

        account_hash = hashlib.sha256(account.id.encode("utf-8")).hexdigest()[:12]
        account_name = getattr(account, "name", "") or "Брокерский счёт"
        for item in portfolio.positions:
            item_uid = getattr(item, "instrument_uid", "")
            item_figi = getattr(item, "figi", "")
            item_ticker = str(getattr(item, "ticker", "")).upper()
            ticker = (
                ids_to_tickers.get(item_uid)
                or ids_to_tickers.get(item_figi)
                or (item_ticker if item_ticker in watched_tickers else None)
            )
            if not ticker:
                continue
            quantity = quotation_to_float(item.quantity)
            if abs(quantity) < 0.000001:
                continue
            position = OpenPosition(
                key=f"{account_hash}:{ticker}",
                account_name=account_name,
                ticker=ticker,
                instrument_id=instruments[ticker],
                quantity=quantity,
                average_price=quotation_to_float(item.average_position_price),
            )
            positions_by_ticker.setdefault(ticker, []).append(position)

    if accounts and successful_accounts == 0:
        raise RuntimeError("Не удалось прочитать ни один доступный портфель T-Invest.")
    return positions_by_ticker


def load_position_plan(
    state: dict[str, str],
    position: OpenPosition,
    snapshot: Snapshot,
) -> tuple[PositionPlan, bool]:
    state_key = f"position_plan:{position.key}"
    raw_plan = state.get(state_key)
    if raw_plan:
        try:
            plan = PositionPlan(**json.loads(raw_plan))
            expected_entry = (
                position.average_price if position.average_price > 0 else plan.entry
            )
            tolerance = max(snapshot.atr * 0.25, abs(plan.entry) * 0.001)
            if (
                plan.direction == position.direction
                and abs(plan.entry - expected_entry) <= tolerance
            ):
                return plan, False
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return create_position_plan(position, snapshot), True


def should_send_position_notice(
    state: dict[str, str],
    position: OpenPosition,
    event: str,
    hold_minutes: int,
    exit_minutes: int,
) -> bool:
    raw_notice = state.get(f"position_notice:{position.key}")
    if not raw_notice:
        return True
    try:
        notice = json.loads(raw_notice)
        previous_event = str(notice["event"])
        previous_time = datetime.fromisoformat(str(notice["time"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True

    elapsed = datetime.now(timezone.utc) - previous_time
    if event == "HOLD":
        return elapsed >= timedelta(minutes=hold_minutes)
    if event.startswith("MOVE_STOP"):
        return previous_event != event
    if previous_event != event:
        return True
    interval = exit_minutes if event.startswith("EXIT_") else hold_minutes
    return elapsed >= timedelta(minutes=interval)


def mark_position_notice(
    state: dict[str, str],
    position: OpenPosition,
    event: str,
) -> None:
    state[f"position_notice:{position.key}"] = json.dumps(
        {"event": event, "time": datetime.now(timezone.utc).isoformat()}
    )


def load_active_positions(state: dict[str, str]) -> dict[str, str]:
    try:
        value = json.loads(state.get("active_positions", "{}"))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def clear_position_state(state: dict[str, str], position_key: str) -> None:
    state.pop(f"position_plan:{position_key}", None)
    state.pop(f"position_notice:{position_key}", None)


def load_entry_plan(state: dict[str, str], ticker: str) -> EntryPlan | None:
    raw_plan = state.get(f"entry_plan:{ticker}")
    if not raw_plan:
        return None
    try:
        return EntryPlan(**json.loads(raw_plan))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def store_entry_plan(state: dict[str, str], plan: EntryPlan) -> None:
    state[f"entry_plan:{plan.ticker}"] = json.dumps(
        asdict(plan), ensure_ascii=False
    )


def clear_entry_plan(state: dict[str, str], ticker: str) -> None:
    state.pop(f"entry_plan:{ticker}", None)
    state.pop(f"entry_notice:{ticker}", None)


def update_entry_plan(
    state: dict[str, str],
    snapshot: Snapshot,
) -> tuple[EntryPlan | None, bool]:
    current = load_entry_plan(state, snapshot.ticker)
    if snapshot.decision not in {"LONG", "SHORT"}:
        return current, False
    if (
        current
        and current.signal_time == snapshot.time.isoformat()
        and current.direction == snapshot.decision
    ):
        return current, False
    new_plan = entry_plan_from_snapshot(snapshot)
    store_entry_plan(state, new_plan)
    state.pop(f"entry_notice:{snapshot.ticker}", None)
    return new_plan, True


def should_send_entry_notice(
    state: dict[str, str],
    plan: EntryPlan,
    event: str,
    reminder_minutes: int,
) -> bool:
    raw_notice = state.get(f"entry_notice:{plan.ticker}")
    if not raw_notice:
        return True
    try:
        notice = json.loads(raw_notice)
        previous_signal = str(notice["signal_time"])
        previous_event = str(notice["event"])
        previous_time = datetime.fromisoformat(str(notice["time"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True
    if previous_signal != plan.signal_time or previous_event != event:
        return True
    if event != "READY":
        return False
    return datetime.now(timezone.utc) - previous_time >= timedelta(
        minutes=reminder_minutes
    )


def mark_entry_notice(state: dict[str, str], plan: EntryPlan, event: str) -> None:
    state[f"entry_notice:{plan.ticker}"] = json.dumps(
        {
            "signal_time": plan.signal_time,
            "event": event,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


def resolve_instruments(client: object, tickers: Iterable[str]) -> dict[str, str]:
    from t_tech.invest import InstrumentType

    resolved: dict[str, str] = {}
    for ticker in tickers:
        response = client.instruments.find_instrument(
            query=ticker,
            instrument_kind=InstrumentType.INSTRUMENT_TYPE_FUTURES,
        )
        exact = [item for item in response.instruments if item.ticker.upper() == ticker.upper()]
        if not exact:
            raise RuntimeError(f"T-Invest не нашёл фьючерс с тикером {ticker}.")
        exact.sort(
            key=lambda item: (
                item.class_code.upper() == "SPBFUT",
                bool(item.api_trade_available_flag),
            ),
            reverse=True,
        )
        resolved[ticker] = exact[0].uid or exact[0].figi
    return resolved


def fetch_candles(client: object, instrument_id: str) -> list[CandlePoint]:
    from t_tech.invest import CandleInterval

    now = datetime.now(timezone.utc)
    def request(window: timedelta, limit: int | None) -> list[object]:
        response = client.market_data.get_candles(
            instrument_id=instrument_id,
            from_=now - window,
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
            limit=limit,
        )
        return list(response.candles)

    raw_candles = request(timedelta(hours=8), 100)
    if len(raw_candles) < 23:
        raw_candles = request(timedelta(days=3), None)

    return [
        CandlePoint(
            time=item.time,
            open=quotation_to_float(item.open),
            high=quotation_to_float(item.high),
            low=quotation_to_float(item.low),
            close=quotation_to_float(item.close),
            volume=item.volume,
            is_complete=item.is_complete,
        )
        for item in raw_candles
    ]


def run_cycle(
    client: object,
    instruments: dict[str, str],
    telegram_token: str,
    chat_id: str,
    state: dict[str, str],
    entry_plan_minutes: int,
    entry_reminder_minutes: int,
    position_hold_minutes: int,
    exit_reminder_minutes: int,
    notify_always: bool,
) -> int:
    price_response = client.market_data.get_last_prices(
        instrument_id=list(instruments.values())
    )
    prices = {
        item.instrument_uid: quotation_to_float(item.price)
        for item in price_response.last_prices
    }
    positions_by_ticker = fetch_open_positions(client, instruments)
    current_active = {
        position.key: position.ticker
        for positions in positions_by_ticker.values()
        for position in positions
    }
    previous_active = load_active_positions(state)
    active_for_state = dict(current_active)
    sent = 0
    for ticker, instrument_id in instruments.items():
        try:
            candles = fetch_candles(client, instrument_id)
            snapshot = analyze(ticker, candles, prices.get(instrument_id))
            logging.info("snapshot=%s", json.dumps(asdict(snapshot), default=str, ensure_ascii=False))

            open_positions = positions_by_ticker.get(ticker, [])
            if open_positions:
                clear_entry_plan(state, ticker)
                for position in open_positions:
                    plan, is_new = load_position_plan(state, position, snapshot)
                    advice = assess_position(position, snapshot, plan)
                    if advice.action.startswith("EXIT_"):
                        event = advice.action
                    elif advice.action == "MOVE_STOP":
                        event = f"MOVE_STOP:{advice.plan.stage}"
                    elif is_new:
                        event = "NEW"
                    else:
                        event = "HOLD"

                    if notify_always or should_send_position_notice(
                        state,
                        position,
                        event,
                        position_hold_minutes,
                        exit_reminder_minutes,
                    ):
                        telegram_send(
                            telegram_token,
                            chat_id,
                            format_position_advice(position, snapshot, advice, event),
                        )
                        mark_position_notice(state, position, event)
                        state[f"position_plan:{position.key}"] = json.dumps(
                            asdict(advice.plan), ensure_ascii=False
                        )
                        sent += 1
                continue

            entry_plan, _ = update_entry_plan(state, snapshot)
            if entry_plan:
                entry_status = entry_plan_status(
                    entry_plan,
                    snapshot.price,
                    entry_plan_minutes,
                )
                if entry_status in {"EXPIRED", "CANCELLED", "MISSED"}:
                    clear_entry_plan(state, ticker)
                else:
                    if notify_always or should_send_entry_notice(
                        state,
                        entry_plan,
                        entry_status,
                        entry_reminder_minutes,
                    ):
                        telegram_send(
                            telegram_token,
                            chat_id,
                            format_entry_plan(
                                entry_plan,
                                snapshot.price,
                                entry_status,
                            ),
                        )
                        mark_entry_notice(state, entry_plan, entry_status)
                        sent += 1
                    continue

            if notify_always:
                telegram_send(
                    telegram_token,
                    chat_id,
                    format_snapshot(snapshot, test_message=notify_always and not snapshot.alerts),
                )
                sent += 1
        except Exception:
            logging.exception("Не удалось обработать %s", ticker)

    for missing_key, missing_ticker in previous_active.items():
        if missing_key in current_active:
            continue
        try:
            telegram_send(
                telegram_token,
                chat_id,
                f"✅ ПОЗИЦИЯ БОЛЬШЕ НЕ НАЙДЕНА — {missing_ticker}\n\n"
                "Сопровождение этой позиции остановлено. Бот снова будет искать "
                "новый вход по инструменту.",
            )
            clear_position_state(state, missing_key)
            sent += 1
        except Exception:
            logging.exception("Не удалось сообщить о закрытии позиции %s", missing_ticker)
            active_for_state[missing_key] = missing_ticker

    state["active_positions"] = json.dumps(active_for_state, ensure_ascii=False)
    save_state(state)
    return sent


def parse_positive_int(name: str, default: int, minimum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} должен быть целым числом.") from error
    if value < minimum:
        raise RuntimeError(f"{name} должен быть не меньше {minimum}.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Монитор фьючерсов без автоматической торговли")
    parser.add_argument("--once", action="store_true", help="выполнить один цикл")
    parser.add_argument("--notify-always", action="store_true", help="прислать тест даже без сигнала")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )
    try:
        load_env()
        tinvest_token = os.environ["TINVEST_TOKEN"]
        telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        tickers = [item.strip().upper() for item in os.getenv("TICKERS", "").split(",") if item.strip()]
        if not tickers:
            raise RuntimeError("Список TICKERS пуст.")
        poll_seconds = parse_positive_int("POLL_SECONDS", 60, 30)
        entry_plan_minutes = parse_positive_int("ENTRY_PLAN_MINUTES", 60, 5)
        entry_reminder_minutes = parse_positive_int("ENTRY_REMINDER_MINUTES", 15, 5)
        position_hold_minutes = parse_positive_int("POSITION_HOLD_MINUTES", 60, 5)
        exit_reminder_minutes = parse_positive_int("EXIT_REMINDER_MINUTES", 15, 5)
    except (KeyError, RuntimeError) as error:
        print(f"Ошибка настроек: {error}")
        return 1

    from t_tech.invest import Client

    state = load_state()
    try:
        with Client(tinvest_token, app_name="futures-monitor-read-only") as client:
            instruments = resolve_instruments(client, tickers)
            logging.info("Найдены инструменты: %s", ", ".join(instruments))
            while True:
                run_cycle(
                    client,
                    instruments,
                    telegram_token,
                    chat_id,
                    state,
                    entry_plan_minutes,
                    entry_reminder_minutes,
                    position_hold_minutes,
                    exit_reminder_minutes,
                    args.notify_always,
                )
                if args.once:
                    break
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем")
    except Exception:
        logging.exception("Монитор остановлен из-за ошибки")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
