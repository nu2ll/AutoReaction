from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
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


async def edit_panel(update: Update, panel: int | None, view: tuple[str, InlineKeyboardMarkup]) -> int | None:
    """Show `view` by editing the panel message (the one with the buttons) instead of sending a new one.

    Falls back to a new message only when there is no panel to edit. Returns the id of the message
    that now holds the view."""
    message = update.effective_message
    if message is None:
        return panel
    text, markup = view
    if panel:
        try:
            await message.get_bot().edit_message_text(
                text, chat_id=message.chat_id, message_id=panel, reply_markup=markup
            )
            return panel
        except BadRequest as exc:
            if "not modified" in exc.message.lower():
                return panel
        except (TelegramError, AttributeError):
            pass
    try:
        sent = await message.chat.send_message(text, reply_markup=markup)
    except TelegramError:
        return panel
    return getattr(sent, "message_id", None) or panel


async def present(update: Update, context: Context, view: tuple[str, InlineKeyboardMarkup]) -> None:
    """Answer a typed command with a fresh panel at the bottom of the chat.

    The command message and the previous panel are removed, so there is still just one bot message.
    (Editing the old panel is not safe here: after the user clears the chat history it still exists
    for Telegram, so an edit would succeed on a message nobody can see.)"""
    message = update.effective_message
    if message is None:
        return
    try:
        await message.delete()
    except (TelegramError, AttributeError):
        pass
    text, markup = view
    try:
        sent = await message.chat.send_message(text, reply_markup=markup)
    except TelegramError:
        return
    new = getattr(sent, "message_id", None)
    old = context.user_data.get("panel")
    if old and old != new:
        try:
            await message.get_bot().delete_message(chat_id=message.chat_id, message_id=old)
        except (TelegramError, AttributeError):
            pass
    if new:
        context.user_data["panel"] = new


async def answer(update: Update, text: str | None = None, *, alert: bool = False) -> None:
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=alert)


async def prompt(update: Update, context: Context, state: dict, view: tuple[str, InlineKeyboardMarkup]) -> None:
    """Show an input prompt and remember its message, so it can be removed once answered."""
    query = update.callback_query
    panel = getattr(getattr(query, "message", None), "message_id", None) if query else None
    context.user_data["input"] = {**state, "panel": panel} if panel else dict(state)
    await show(update, view)


async def close_panel(update: Update, state: dict) -> None:
    """Delete the prompt message that asked for the input we just received."""
    panel, message = state.get("panel"), update.effective_message
    if not panel or message is None:
        return
    try:
        await message.get_bot().delete_message(message.chat_id, panel)
    except (TelegramError, AttributeError):
        pass


async def flash(core: Core, send, text: str, seconds: float | None = None) -> None:
    """Show a short notice that removes itself.

    Telegram only has real pop-up toasts for button presses (those use `answer`); for the result
    of a typed message the closest thing is a message that deletes itself after a few seconds.
    `send` is e.g. message.chat.send_message or message.reply_text."""
    try:
        sent = await send(text)
    except TelegramError:
        return
    delete = getattr(sent, "delete", None)
    if delete is None:
        return
    delay = seconds if seconds is not None else max(5.0, min(15.0, len(text) / 12))

    async def later() -> None:
        await asyncio.sleep(delay)
        try:
            await delete()
        except TelegramError:
            pass

    core.spawn(later())


async def gatekeeper(update: Update, context: Context) -> None:
    """Runs before every handler: records users, blocks banned ones, drops stale input state."""
    user, chat = update.effective_user, update.effective_chat
    if not user or user.is_bot or not chat or chat.type != ChatType.PRIVATE:
        return
    core = core_of(context)

    # Remember the panel message the user is working in, so commands can edit it later.
    query_message = getattr(update.callback_query, "message", None)
    if query_message is not None and getattr(query_message, "message_id", None):
        context.user_data["panel"] = query_message.message_id

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
