from __future__ import annotations

import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def telegram_call(token: str, method: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, headers={"User-Agent": "futures-monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Telegram вернул ошибку HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Нет связи с Telegram: {error.reason}") from error
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Неизвестная ошибка Telegram"))
    return payload


def find_chat_id(token: str) -> str | None:
    updates = telegram_call(token, "getUpdates").get("result", [])
    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post")
        if message and message.get("chat", {}).get("id") is not None:
            return str(message["chat"]["id"])
    return None


def safe_secret(prompt: str) -> str:
    value = getpass.getpass(prompt).strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Токен не введён или содержит недопустимые символы.")
    return value


def validate_tinvest_token(token: str) -> None:
    """Check the token before saving it, without printing or logging the secret."""
    try:
        from t_tech.invest import Client, InstrumentType

        with Client(token) as client:
            client.instruments.find_instrument(
                query="IMOEXF",
                instrument_kind=InstrumentType.INSTRUMENT_TYPE_FUTURES,
            )
    except Exception as error:
        if error.__class__.__name__ == "UnauthenticatedError":
            raise RuntimeError(
                "T-Invest отклонил токен. Создайте новый токен для обычного "
                "(не песочничного) счёта с доступом только для чтения."
            ) from error
        raise RuntimeError(f"Не удалось проверить токен T-Invest: {error}") from error


def change_tinvest_token() -> int:
    if not ENV_FILE.exists():
        print("Настройки ещё не созданы. Сначала запустите INSTALL_AND_SETUP.cmd.")
        return 1

    try:
        token = safe_secret("Вставьте новый READ-ONLY токен T-Invest: ")
        print("Проверяю токен T-Invest...")
        validate_tinvest_token(token)

        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        updated = False
        for index, line in enumerate(lines):
            if line.startswith("TINVEST_TOKEN="):
                lines[index] = f"TINVEST_TOKEN={token}"
                updated = True
                break
        if not updated:
            lines.insert(0, f"TINVEST_TOKEN={token}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Токен T-Invest работает и сохранён. Telegram не изменён.")
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"\nОшибка: {error}")
        return 1


def main() -> int:
    print("Токены не отображаются при вводе и сохраняются только в локальном .env.\n")
    try:
        tinvest_token = safe_secret("Вставьте READ-ONLY токен T-Invest: ")
        print("Проверяю токен T-Invest...")
        validate_tinvest_token(tinvest_token)
        print("Токен T-Invest работает.")
        telegram_token = safe_secret("Вставьте токен Telegram от BotFather: ")
        bot = telegram_call(telegram_token, "getMe")["result"]
        print(f"Telegram-бот найден: @{bot.get('username', 'без имени')}")

        chat_id = find_chat_id(telegram_token)
        if chat_id is None:
            print("\nОткройте своего бота в Telegram, нажмите Start или отправьте /start.")
            input("После этого нажмите Enter здесь...")
            chat_id = find_chat_id(telegram_token)
        if chat_id is None:
            raise RuntimeError("Не найден чат. Отправьте боту /start и повторите настройку.")

        content = (
            f"TINVEST_TOKEN={tinvest_token}\n"
            f"TELEGRAM_BOT_TOKEN={telegram_token}\n"
            f"TELEGRAM_CHAT_ID={chat_id}\n"
            "TICKERS=BRV6,CRU6,GDU6,IMOEXF,MXU6,NGU6,RIU6,SIU6,SVU6\n"
            "POLL_SECONDS=60\n"
            "ENTRY_PLAN_MINUTES=60\n"
            "ENTRY_REMINDER_MINUTES=15\n"
            "POSITION_HOLD_MINUTES=60\n"
            "EXIT_REMINDER_MINUTES=15\n"
            "SSL_TBANK_VERIFY=True\n"
        )
        ENV_FILE.write_text(content, encoding="utf-8")
        print(f"\nГотово. Чат Telegram найден: {chat_id}")
        print("Настройки сохранены локально. Не отправляйте файл .env другим людям.")
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"\nОшибка: {error}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--tinvest-only":
        sys.exit(change_tinvest_token())
    sys.exit(main())
