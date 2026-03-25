"""Блокировка пользователей при включённых техработах (кроме админов и белого списка)."""
from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware

from database import get_maintenance_reply_text, user_passes_maintenance


class MaintenanceMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        user = message.from_user
        if not user:
            return
        if await user_passes_maintenance(user.id, user.username):
            return
        text = await get_maintenance_reply_text()
        await message.answer(text)
        raise CancelHandler()

    async def on_pre_process_callback_query(self, callback_query: types.CallbackQuery, data: dict):
        user = callback_query.from_user
        if not user:
            return
        if await user_passes_maintenance(user.id, user.username):
            return
        text = await get_maintenance_reply_text()
        await callback_query.answer(text[:200], show_alert=True)
        raise CancelHandler()
