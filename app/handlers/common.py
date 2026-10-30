from __future__ import annotations

import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.core import Core

log = logging.getLogger(__name__)

Context = ContextTypes.DEFAULT_TYPE


def core_of(context: Context) -> Core:
    return context.application.bot_data["core"]


async def respond(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message a button was pressed on, or reply to a command."""
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in exc.message.lower():
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show(update: Update, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await respond(update, *view)


async def answer(update: Update, text: str | None = None, *, alert: bool = False) -> None:
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=alert)


async def gatekeeper(update: Update, context: Context) -> None:
    """Runs before every handler: records users, blocks banned ones, drops stale input state."""
    user, chat = update.effective_user, update.effective_chat
    if not user or user.is_bot or not chat or chat.type != ChatType.PRIVATE:
        return
    core = core_of(context)

    # Pressing a button or sending a command abandons whatever input we were waiting for.
    message = update.effective_message
    if update.callback_query or (message and message.text and message.text.startswith("/")):
        context.user_data.pop("input", None)

    record = await core.db.touch_user(user.id, user.username, user.full_name)
    if record.banned and not core.access.is_owner(user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔️ دسترسی شما مسدود شده.", show_alert=True)
        elif message:
            await message.reply_text("⛔️ دسترسی شما مسدود شده.")
        raise ApplicationHandlerStop


async def noop(update: Update, context: Context) -> None:
    await answer(update)


async def on_error(update: object, context: Context) -> None:
    log.error("unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer("⚠️ خطایی رخ داد، دوباره تلاش کن.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
