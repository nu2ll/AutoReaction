from __future__ import annotations

import asyncio
import html

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from app.core import Core
from app.storage import Settings

ACTIVE_STATUSES = frozenset({
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
})


def render_panel(core: Core, main: Bot) -> str:
    s = core.store.settings
    bots = "\n".join(f"• @{bot.username}" for bot in core.bots(main))
    chats = "\n".join(
        f"• {html.escape(title)} <code>{cid}</code>" for cid, title in s.chats.items()
    ) or "• هنوز چتی اضافه نشده"

    return "\n".join([
        "<b>⚙️ پنل ری‌اکشن خودکار</b>",
        "",
        f"وضعیت: {'🟢 فعال' if s.enabled else '🔴 غیرفعال'}",
        f"احتمال ری‌اکشن: <b>{s.chance}%</b>",
        "",
        f"<b>🤖 ربات‌ها ({len(core.pool) + 1})</b>",
        bots,
        "",
        f"<b>📢 چت‌ها ({len(s.chats)})</b>",
        chats,
    ])


def panel_keyboard(s: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 خاموش کن" if s.enabled else "🟢 روشن کن", callback_data="toggle")],
        [
            InlineKeyboardButton("➖ 10%", callback_data="chance:-10"),
            InlineKeyboardButton(f"🎯 {s.chance}%", callback_data="noop"),
            InlineKeyboardButton("➕ 10%", callback_data="chance:10"),
        ],
        [
            InlineKeyboardButton("🔍 بررسی دسترسی‌ها", callback_data="check"),
            InlineKeyboardButton("🔄 بروزرسانی", callback_data="refresh"),
        ],
    ])


async def _member_line(main: Bot, chat_id: int, bot: Bot) -> str:
    try:
        status = (await main.get_chat_member(chat_id, bot.id)).status
    except TelegramError:
        status = "not found"
    mark = "✅" if status in ACTIVE_STATUSES else "❌"
    return f"{mark} @{bot.username}: {status}"


async def render_access_report(core: Core, main: Bot) -> str:
    chats = core.store.settings.chats
    if not chats:
        return "هیچ چتی ثبت نشده."

    bots = core.bots(main)
    sections = []
    for cid, title in chats.items():
        lines = await asyncio.gather(*(_member_line(main, int(cid), bot) for bot in bots))
        sections.append("\n".join([f"<b>{html.escape(title)}</b>", *lines]))
    return "\n\n".join(sections)
