"""Free-text answers to prompts the bot showed (token, chat id, emoji list)."""
from __future__ import annotations

import re

from telegram import Update
from telegram.error import TelegramError

from app import chats, views
from app.core import BotRegistrationError, Core
from app.emoji import parse_emojis
from app.handlers.common import Context, core_of, show
from app.models import Mode

TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")


async def register_tokens(core: Core, tokens: list[str], owner_id: int | None) -> str:
    """Register every token, return a summary message."""
    lines: list[str] = []
    for token in dict.fromkeys(tokens):  # de-duplicate, keep order
        try:
            bot = await core.register_bot(token, owner_id)
        except BotRegistrationError as exc:
            lines.append(f"{exc}\n<code>{token[:10]}…</code>")
        else:
            lines.append(f"✅ @{bot.username} اضافه شد.")
    return "\n".join(lines)


async def on_text(update: Update, context: Context) -> None:
    state = context.user_data.get("input")
    if not state:
        return

    core = core_of(context)
    message = update.effective_message
    uid = update.effective_user.id
    kind = state["kind"]

    if kind in ("own_bot", "svc_bot"):
        # the message holds secrets: remove it right away
        try:
            await message.delete()
        except TelegramError:
            pass

        tokens = TOKEN_RE.findall(message.text or "")
        if not tokens:
            await message.chat.send_message("❌ توکن معتبری توی پیامت پیدا نکردم. دوباره بفرست یا /cancel.")
            return

        if kind == "svc_bot" and not core.access.is_owner(uid):
            return
        context.user_data.pop("input", None)
        owner = uid if kind == "own_bot" else None
        summary = await register_tokens(core, tokens, owner)
        await message.chat.send_message(summary)

        if kind == "own_bot":
            await _attach_to_existing_chats(core, uid)
            await show(update, await views.bots_view(core, uid))
        else:
            await show(update, await views.service_bots_view(core, 0))
        return

    if kind == "chat_ref":
        ref = chats.parse_chat_ref(message.text or "")
        if ref is None:
            await message.reply_text("❌ فرمت درست نیست. مثل <code>@mychannel</code> یا آیدی عددی بفرست.")
            return
        try:
            chat = await chats.resolve_chat(core, uid, ref)
            target = await chats.register_chat(core, uid, chat)
        except chats.ChatError as exc:
            await message.reply_text(str(exc))
            return

        context.user_data.pop("input", None)
        await message.reply_text("✅ چت ثبت شد. در حال ادمین‌کردن ربات‌ها…")
        core.spawn(chats.provision_and_notify(core, target))
        await show(update, await views.target_view(core, target))
        return

    if kind == "emojis":
        emojis = parse_emojis(message.text or "")
        if not emojis:
            await message.reply_text(
                "❌ ایموجی مجاز پیدا نکردم. فقط ایموجی‌های ری‌اکشن تلگرام قبول می‌شن، مثلاً 🔥 ❤️ 👍"
            )
            return

        target = await core.db.get_target(state["target"])
        if target is None or not core.access.can_manage(uid, target):
            context.user_data.pop("input", None)
            return
        await core.db.update_target(target.id, emojis=emojis, mode=Mode.CUSTOM)
        context.user_data.pop("input", None)
        target = await core.db.get_target(target.id)
        await show(update, await views.target_view(core, target))


async def _attach_to_existing_chats(core: Core, user_id: int) -> None:
    """A new own bot should immediately work in the user's chats that use own bots."""
    for target in await core.db.list_targets(user_id):
        if target.uses_own:
            core.spawn(chats.provision_and_notify(core, target))
