from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.core import Core
from app.handlers import admin, bots, common, events, inputs, menu, targets

# (command, description) shown in the Telegram menu
USER_COMMANDS = (
    ("start", "منوی اصلی"),
    ("menu", "منوی اصلی"),
    ("help", "راهنما"),
    ("cancel", "لغو عملیات"),
)
ADMIN_COMMANDS = (
    ("admin", "پنل مدیریت"),
    ("grant", "فعال‌سازی سرویس برای کاربر"),
    ("revoke", "قطع دسترسی سرویس"),
    ("ban", "مسدود کردن کاربر"),
    ("unban", "آزاد کردن کاربر"),
    ("user", "اطلاعات کاربر"),
    ("addbot", "افزودن ربات سرویس"),
    ("delbot", "حذف ربات سرویس"),
)

ALLOWED_UPDATES = ["message", "channel_post", "callback_query", "my_chat_member"]


def register(app: Application, core: Core) -> None:
    private = filters.ChatType.PRIVATE
    owner = private & filters.User(user_id=core.config.owners)

    # group -1: runs first for every update (records users, blocks banned ones)
    app.add_handler(TypeHandler(Update, common.gatekeeper), group=-1)

    app.add_handler(CommandHandler(["start", "menu"], menu.cmd_start, filters=private))
    app.add_handler(CommandHandler("help", menu.cmd_help, filters=private))
    app.add_handler(CommandHandler("cancel", menu.cmd_cancel, filters=private))

    for name, handler in (
        ("admin", admin.cmd_admin),
        ("grant", admin.cmd_grant),
        ("revoke", admin.cmd_revoke),
        ("ban", admin.cmd_ban),
        ("unban", admin.cmd_unban),
        ("user", admin.cmd_user),
        ("addbot", admin.cmd_addbot),
        ("delbot", admin.cmd_delbot),
    ):
        app.add_handler(CommandHandler(name, handler, filters=owner))

    app.add_handler(CallbackQueryHandler(menu.on_menu, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(targets.on_target, pattern=r"^t:"))
    app.add_handler(CallbackQueryHandler(bots.on_bots, pattern=r"^b:"))
    app.add_handler(CallbackQueryHandler(admin.on_admin, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(common.noop, pattern=r"^noop$"))

    app.add_handler(ChatMemberHandler(events.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, inputs.on_text))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST | (filters.ChatType.GROUPS & filters.UpdateType.MESSAGE),
            events.on_post,
        )
    )
    app.add_error_handler(common.on_error)
