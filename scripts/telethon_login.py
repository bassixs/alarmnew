"""Одноразовая авторизация Telethon-сессии для чтения публичных каналов-источников.

Запуск: python scripts/telethon_login.py
Введите номер телефона и код из Telegram. Создаётся файл сессии (имя из .env
TELETHON_SESSION), после чего TelegramCollector сможет читать каналы без повторного входа.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from aialarm.config import get_settings


async def main() -> None:
    s = get_settings().secrets
    if not s.telegram_api_id or not s.telegram_api_hash:
        raise SystemExit("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
    client = TelegramClient(s.telethon_session, s.telegram_api_id, s.telegram_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Авторизовано как: {me.username or me.id}. Сессия сохранена: {s.telethon_session}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
