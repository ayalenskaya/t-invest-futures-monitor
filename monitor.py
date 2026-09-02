from __future__ import annotations

import argparse
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


def telegram_send(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"Telegram недоступен: {error.reason}") from error
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram не принял сообщение"))


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


def should_send(snapshot: Snapshot, state: dict[str, str], cooldown_minutes: int) -> bool:
    if snapshot.decision == "WAIT":
        return False
    seen_key = f"last_seen:{snapshot.ticker}"
    fingerprint = f"{snapshot.decision}:{snapshot.time.isoformat()}"
    if state.get(seen_key) == fingerprint:
        return False
    state[seen_key] = fingerprint

    cooldown_key = f"last_sent:{snapshot.ticker}:{snapshot.decision}"
    previous = state.get(cooldown_key)
    now = datetime.now(timezone.utc)
    if previous:
        try:
            last_sent = datetime.fromisoformat(previous)
            if now - last_sent < timedelta(minutes=cooldown_minutes):
                return False
        except ValueError:
            pass
    state[cooldown_key] = now.isoformat()
    return True


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
    cooldown_minutes: int,
    notify_always: bool,
) -> int:
    price_response = client.market_data.get_last_prices(
        instrument_id=list(instruments.values())
    )
    prices = {
        item.instrument_uid: quotation_to_float(item.price)
        for item in price_response.last_prices
    }
    sent = 0
    for ticker, instrument_id in instruments.items():
        try:
            candles = fetch_candles(client, instrument_id)
            snapshot = analyze(ticker, candles, prices.get(instrument_id))
            logging.info("snapshot=%s", json.dumps(asdict(snapshot), default=str, ensure_ascii=False))
            if notify_always or should_send(snapshot, state, cooldown_minutes):
                telegram_send(
                    telegram_token,
                    chat_id,
                    format_snapshot(snapshot, test_message=notify_always and not snapshot.alerts),
                )
                sent += 1
        except Exception:
            logging.exception("Не удалось обработать %s", ticker)
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
        cooldown_minutes = parse_positive_int("COOLDOWN_MINUTES", 30, 1)
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
                    cooldown_minutes,
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
