"""Telegram moderation: visual choice, decisions and text revisions."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from aialarm.config import get_settings
from aialarm.control import get_pipeline_state
from aialarm.logging import get_logger
from aialarm.moderation import service
from aialarm.moderation.feedback import REJECTION_REASONS, reason_buttons
from aialarm.moderation.notify import _keyboard, _preview_keyboard_tg, send_card
from aialarm.publishers.service import publish_post_id_sync

log = get_logger(__name__)


class EditState(StatesGroup):
    waiting_text = State()


async def _refresh_card(cq: CallbackQuery, post_id: int) -> None:
    # Keep old controls if sending the replacement fails.
    await asyncio.to_thread(send_card, post_id)
    try:
        await cq.message.delete()
    except Exception:  # noqa: BLE001
        pass


def _reason_keyboard(action: str, object_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button["text"], callback_data=button["payload"])
        for button in row
    ] for row in reason_buttons("reason", action, object_id)])


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.callback_query.filter(F.message.chat.id == get_settings().project.moderation.admin_chat_id)
    inflight: set[int] = set()

    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            f"aialarm модератор. Ваш chat_id: {message.chat.id}\n"
            "Впишите его в config.yaml -> moderation.admin_chat_id."
        )

    @dp.callback_query(F.data.startswith("reason:"))
    async def on_reason(cq: CallbackQuery) -> None:
        _, action, object_id_s, code = cq.data.split(":")
        object_id = int(object_id_s)
        if action not in {"cancel", "reject"}:
            return
        if code == "back":
            keyboard = (_preview_keyboard_tg(object_id) if action == "cancel"
                        else _keyboard(object_id, service.get_pending(object_id)))
            await cq.message.edit_reply_markup(reply_markup=keyboard)
            await cq.answer()
            return
        if code not in REJECTION_REASONS:
            return
        operation = service.cancel_preview if action == "cancel" else service.reject
        ok = operation(object_id, reason=REJECTION_REASONS[code],
                       editor_id=cq.from_user.id, platform="telegram")
        await cq.answer("Отклонено" if ok else "Карточка уже обработана")
        if ok:
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.message.answer(f"❌ {REJECTION_REASONS[code]}")

    @dp.callback_query(F.data.startswith("pre:"))
    async def on_preview(cq: CallbackQuery) -> None:
        _, action, raw_id_s = cq.data.split(":")
        raw_id = int(raw_id_s)
        if action == "cancel":
            await cq.answer("Выберите причину")
            await cq.message.edit_reply_markup(reply_markup=_reason_keyboard("cancel", raw_id))
        elif action == "rewrite":
            if not get_pipeline_state().active:
                await cq.answer("Помощник сейчас вне смены", show_alert=True)
                return
            key = -raw_id
            if key in inflight:
                await cq.answer("Уже переписываю…")
                return
            inflight.add(key)
            try:
                await cq.answer("Переписываю…")
                post_id = await asyncio.to_thread(service.rewrite_and_get, raw_id,
                                                  editor_id=cq.from_user.id, platform="telegram")
                if post_id:
                    await _refresh_card(cq, post_id)
            finally:
                inflight.discard(key)

    @dp.callback_query(F.data.startswith("mod:"))
    async def on_action(cq: CallbackQuery, state: FSMContext) -> None:
        _, action, post_id_s = cq.data.split(":")
        post_id = int(post_id_s)
        if action == "reject":
            await cq.answer("Выберите причину")
            await cq.message.edit_reply_markup(reply_markup=_reason_keyboard("reject", post_id))
            return
        if not get_pipeline_state().active:
            await cq.answer("Помощник сейчас вне смены", show_alert=True)
            return
        if post_id in inflight:
            await cq.answer("Уже обрабатываю…")
            return
        inflight.add(post_id)
        try:
            if action == "media":
                await cq.answer("Выберите картинку")
                await cq.message.edit_reply_markup(
                    reply_markup=_keyboard(post_id, service.get_pending(post_id), visual=True))
            elif action in {"original", "none", "generate"}:
                await cq.answer("Генерирую…" if action == "generate" else "Выбираю визуал…")
                try:
                    ok = (await asyncio.to_thread(service.generate_post_visual, post_id)
                          if action == "generate" else service.select_media(post_id, action))
                except Exception as exc:  # noqa: BLE001
                    log.warning("telegram_visual_failed", post_id=post_id, error=str(exc))
                    ok = False
                if ok:
                    await _refresh_card(cq, post_id)
                else:
                    await cq.message.answer("Не удалось выбрать этот визуал. Выберите другой вариант.")
            elif action == "approve":
                if not service.media_is_selected(post_id):
                    await cq.answer("Сначала выберите картинку", show_alert=True)
                    return
                await cq.answer("Проверяю очередь публикации…")
                post = service.get_pending(post_id)
                if post and post["status"] in {"moderation", "approved"}:
                    ok = await asyncio.to_thread(publish_post_id_sync, post_id, approve=True,
                                                editor_id=cq.from_user.id, moderation_platform="telegram")
                    await cq.message.edit_reply_markup(reply_markup=None)
                    await cq.message.answer("✅ Опубликовано" if ok else
                        "⏳ Пост в очереди: публикация повторится с учётом лимитов и доступности площадок.")
                else:
                    await cq.message.answer("Карточка уже обработана")
            elif action == "edit":
                await cq.answer()
                await state.set_state(EditState.waiting_text)
                await state.update_data(post_id=post_id)
                await cq.message.answer("✏️ Пришлите исправленный текст поста одним сообщением.")
        finally:
            inflight.discard(post_id)

    @dp.message(EditState.waiting_text)
    async def on_edit_text(message: Message, state: FSMContext) -> None:
        if message.chat.id != get_settings().project.moderation.admin_chat_id:
            return
        if not message.text or not message.text.strip():
            await message.answer("Пришлите непустой текст.")
            return
        if not get_pipeline_state().active:
            await message.answer("Помощник сейчас вне смены.")
            return
        data = await state.get_data()
        post_id = int(data["post_id"])
        saved = await asyncio.to_thread(service.apply_edit, post_id, message.text,
                                        editor_id=message.from_user.id, platform="telegram")
        await state.clear()
        if not saved:
            await message.answer("Не удалось сохранить правку. Откройте карточку ещё раз.")
            return
        await asyncio.to_thread(send_card, post_id)

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
