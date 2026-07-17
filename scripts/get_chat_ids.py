"""Показать chat_id каналов/групп, которые «видит» Telegram-бот.

Как пользоваться:
1) Добавь бота админом в нужный канал/группу (в канал — с правом «Публикация сообщений»).
2) Отправь любое сообщение в группе и опубликуй любой пост в канале.
3) Запусти: python scripts/get_chat_ids.py
   Скрипт выведет id, тип и название каждого чата.

Работает через getUpdates (без webhook). Бот-админ получает и сообщения групп, и посты каналов.
"""
from __future__ import annotations

import httpx

from aialarm.config import get_settings


def main() -> None:
    token = get_settings().secrets.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")

    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                  params={"limit": 100, "timeout": 0}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise SystemExit(f"Ошибка API: {data}")

    seen: dict[int, dict] = {}
    for upd in data["result"]:
        for key in ("message", "channel_post", "edited_message", "edited_channel_post",
                    "my_chat_member", "chat_member"):
            obj = upd.get(key)
            if obj and "chat" in obj:
                chat = obj["chat"]
                seen[chat["id"]] = chat

    if not seen:
        print("Пока ничего не найдено.")
        print("→ Опубликуй пост в канале и напиши сообщение в группе, затем запусти снова.")
        return

    print(f"Найдено чатов: {len(seen)}\n")
    for cid, chat in seen.items():
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        print(f"  chat_id = {cid:<16} | type = {chat.get('type'):<10} | {title}")
    print("\nКанал (type=channel) -> config.yaml: channels.telegram")
    print("Группа модерации (type=group/supergroup) -> config.yaml: moderation.admin_chat_id")


if __name__ == "__main__":
    main()
