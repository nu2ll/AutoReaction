from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Sequence

from telegram import Bot, CallbackQuery, ChatFullInfo, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core import Core
from app.panel import panel_keyboard, render_access_report, render_panel

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def core_of(context: ContextTypes.DEFAULT_TYPE) -> Core:
    return context.bot_data["core"]


def parse_ints(args: Sequence[str], count: int) -> list[int] | None:
    try:
        values = [int(a) for a in args[:count]]
    except ValueError:
        return None
    return values if len(values) == count else None


async def resolve_chat(bot: Bot, ref: str) -> ChatFullInfo:
    target: int | str = int(ref) if ref.lstrip("-").isdigit() else ref
    return await bot.get_chat(target)


async def edit_panel(query: CallbackQuery, core: Core, main: Bot) -> None:
    try:
        await query.edit_message_text(
            render_panel(core, main),
            parse_mode=ParseMode.HTML,
            reply_markup=panel_keyboard(core.store.settings),
        )
    except BadRequest as exc:
        if "not modified" not in exc.message.lower():
            raise


async def on_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    settings = core.store.settings
    message, chat = update.effective_message, update.effective_chat

    if not settings.enabled or not message or not chat or str(chat.id) not in settings.chats:
        return

    await core.reactor.handle(
        main=context.bot,
        bots=core.bots(context.bot),
        settings=settings,
        chat_id=chat.id,
        message_id=message.message_id,
        media_group_id=message.media_group_id,
    )


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    await update.message.reply_html(
        render_panel(core, context.bot),
        reply_markup=panel_keyboard(core.store.settings),
    )


async def cmd_addbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    chat = update.effective_chat
    if not context.args:
        await update.message.reply_text("استفاده: /addbot TOKEN")
        return

    token = context.args[0].strip()
    try:
        await update.message.delete()
    except TelegramError:
        pass

    try:
        bot = await core.pool.add(token)
    except TelegramError as exc:
        await chat.send_message(f"❌ توکن نامعتبره: {exc.message}")
        return

    if token not in core.store.settings.workers:
        core.store.settings.workers.append(token)
        await core.store.save()
    await chat.send_message(f"✅ @{bot.username} اضافه شد.")


async def cmd_delbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not context.args:
        await update.message.reply_text("استفاده: /delbot @username")
        return

    token = await core.pool.remove(context.args[0])
    if token is None:
        await update.message.reply_text("❌ این ربات توی لیست نیست.")
        return

    core.store.settings.workers.remove(token)
    await core.store.save()
    await update.message.reply_text("🗑 حذف شد.")


async def cmd_addchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not context.args:
        await update.message.reply_text("استفاده: /addchat @channel")
        return

    try:
        chat = await resolve_chat(context.bot, context.args[0])
    except TelegramError:
        await update.message.reply_text("❌ چت پیدا نشد، ربات اصلی باید داخلش باشه.")
        return

    title = chat.title or chat.username or str(chat.id)
    core.store.settings.chats[str(chat.id)] = title
    await core.store.save()
    core.cache.clear()
    await update.message.reply_html(f"✅ <b>{html.escape(title)}</b> اضافه شد.")


async def cmd_delchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not context.args:
        await update.message.reply_text("استفاده: /delchat @channel")
        return

    key = context.args[0]
    try:
        key = str((await resolve_chat(context.bot, key)).id)
    except TelegramError:
        pass

    if core.store.settings.chats.pop(key, None) is None:
        await update.message.reply_text("❌ این چت توی لیست نیست.")
        return

    await core.store.save()
    await update.message.reply_text("🗑 حذف شد.")


async def cmd_chance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    values = parse_ints(context.args, 1)
    if not values or not 0 <= values[0] <= 100:
        await update.message.reply_text("استفاده: /chance 0..100")
        return

    core.store.settings.chance = values[0]
    await core.store.save()
    await update.message.reply_text(f"🎯 احتمال: {values[0]}%")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(await render_access_report(core_of(context), context.bot))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    core = core_of(context)

    if query.from_user.id not in core.config.owners:
        await query.answer("⛔️ دسترسی نداری", show_alert=True)
        return

    settings = core.store.settings
    action, _, arg = (query.data or "").partition(":")

    match action:
        case "noop":
            await query.answer()
            return
        case "check":
            await query.answer("در حال بررسی...")
            await query.message.reply_html(await render_access_report(core, context.bot))
            return
        case "toggle":
            settings.enabled = not settings.enabled
            await core.store.save()
        case "chance":
            settings.chance = max(0, min(100, settings.chance + int(arg)))
            await core.store.save()
        case "refresh":
            core.cache.clear()

    await query.answer()
    await edit_panel(query, core, context.bot)


COMMANDS: tuple[tuple[str, str, Handler], ...] = (
    ("panel", "پنل مدیریت", cmd_panel),
    ("addbot", "اضافه کردن ربات کمکی", cmd_addbot),
    ("delbot", "حذف ربات کمکی", cmd_delbot),
    ("addchat", "اضافه کردن چنل یا گروه", cmd_addchat),
    ("delchat", "حذف چنل یا گروه", cmd_delchat),
    ("chance", "درصد احتمال ری‌اکشن", cmd_chance),
    ("check", "بررسی دسترسی ربات‌ها", cmd_check),
)


def register(app: Application, core: Core) -> None:
    owner_only = filters.User(user_id=core.config.owners) & filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", cmd_panel, filters=owner_only))
    for name, _, handler in COMMANDS:
        app.add_handler(CommandHandler(name, handler, filters=owner_only))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST | (filters.ChatType.GROUPS & filters.UpdateType.MESSAGE),
        on_post,
    ))
