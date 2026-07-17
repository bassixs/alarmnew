"""Long-running бот-модератор (aiogram polling).

Обрабатывает кнопки карточки: ✅ Опубликовать / ✏️ Править / ❌ Отклонить.
«Править» переводит в состояние ожидания нового текста (FSM в памяти); следующее
сообщение администратора становится новым текстом поста, после чего он одобряется.

Запуск: python -m aialarm.cli bot
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from aialarm.config import get_settings
from aialarm.logging import get_logger
from aialarm.moderation import service
from aialarm.publishers.service import publish_post_id_sync

log = get_logger(__name__)


class EditState(StatesGroup):
    waiting_text = State()


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            f"aialarm модератор. Ваш chat_id: {message.chat.id}\n"
            "Впишите его в config.yaml -> moderation.admin_chat_id."
        )

    @dp.callback_query(F.data.startswith("pre:"))
    async def on_preview(cq: CallbackQuery) -> None:
        _, action, raw_id_s = cq.data.split(":")
        raw_id = int(raw_id_s)
        if action == "rewrite":
            await cq.answer("Переписываю…")
            post_id = await asyncio.to_thread(service.rewrite_and_get, raw_id)
            try:
                await cq.message.delete()
            except Exception:  # noqa: BLE001
                pass
            if post_id:
                from aialarm.moderation.notify import send_card

                await asyncio.to_thread(send_card, post_id)
        elif action == "cancel":
            service.cancel_preview(raw_id)
            try:
                await cq.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await cq.answer("Отменено")

    @dp.callback_query(F.data.startswith("mod:"))
    async def on_action(cq: CallbackQuery, state: FSMContext) -> None:
        _, action, post_id_s = cq.data.split(":")
        post_id = int(post_id_s)

        if action == "approve":
            if service.approve(post_id):
                ok = await asyncio.to_thread(publish_post_id_sync, post_id)
                await cq.message.answer(
                    "✅ Опубликовано" if ok else "✅ Одобрено, но публикация не удалась (см. логи)"
                )
            else:
                await cq.message.answer("Не найдено")
        elif action == "reject":
            service.reject(post_id)
            await cq.message.answer("❌ Отклонено")
        elif action == "edit":
            await state.set_state(EditState.waiting_text)
            await state.update_data(post_id=post_id)
            await cq.message.answer("✏️ Пришлите исправленный текст поста одним сообщением.")
        await cq.answer()

    @dp.message(EditState.waiting_text)
    async def on_edit_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        post_id = int(data["post_id"])
        service.apply_edit(post_id, message.text or "")
        await state.clear()
        ok = await asyncio.to_thread(publish_post_id_sync, post_id)
        await message.answer(
            "✅ Исправлено и опубликовано" if ok else "✅ Исправлено. Публикация не удалась (см. логи)"
        )

    return dp


async def run_bot() -> None:
    token = get_settings().secrets.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    bot = Bot(token)
    dp = build_dispatcher()
    log.info("moderation_bot_start")
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(run_bot())
