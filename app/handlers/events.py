from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ChatMemberStatus as S
from telegram.constants import ChatType

from app import chats
from app.handlers.common import Context, core_of

log = logging.getLogger(__name__)
esc = html.escape


async def on_post(update: Update, context: Context) -> None:
    """A new channel post or group message: let the reactor decide who reacts."""
    message, chat = update.effective_message, update.effective_chat
    if not message or not chat:
        return
    await core_of(context).reactor.on_message(
        context.bot, chat.id, message.message_id, message.media_group_id
    )


async def on_my_chat_member(update: Update, context: Context) -> None:
    """The main bot was added to / removed from a chat."""
    event = update.my_chat_member
    if not event or event.chat.type == ChatType.PRIVATE:
        return

    core = core_of(context)
    chat, actor, new = event.chat, event.from_user, event.new_chat_member

    if new.status in (S.LEFT, S.BANNED):
        for target in await core.db.delete_targets_by_chat(chat.id):
            await core.notify(
                target.owner_id,
                f"⚠️ ربات از <b>{esc(chat.title or str(chat.id))}</b> حذف شد؛ این چت از لیستت پاک شد.",
            )
        core.cache.drop(chat.id)
        return

    if chat.type not in chats.SUPPORTED_CHATS or actor.is_bot:
        return

    if new.status == S.ADMINISTRATOR:
        existing = await core.db.get_target_by_chat(chat.id)
        if existing:
            if chat.title and existing.title != chat.title:
                await core.db.update_target(existing.id, title=chat.title)
            return

        user = await core.db.touch_user(actor.id, actor.username, actor.full_name)
        if user.banned and not core.access.is_owner(actor.id):
            return
        try:
            target = await chats.register_chat(core, actor.id, chat)
        except chats.ChatError as exc:
            await core.notify(actor.id, str(exc))
            return

        open_btn = Markup([[Btn("⚙️ تنظیمات چت", callback_data=f"t:open:{target.id}")]])
        await core.notify(actor.id, f"✅ چت <b>{esc(target.title)}</b> ثبت شد.", open_btn)

        if getattr(new, "can_promote_members", False):
            core.spawn(chats.provision_and_notify(core, target))
        else:
            await core.notify(
                actor.id,
                "ℹ️ برای اینکه ربات‌ها خودکار ادمین بشن، دسترسی «افزودن ادمین» (Add New Admins) "
                "رو به ربات بده و بعد توی صفحه‌ی چت «🛡 ادمین‌کردن ربات‌ها» رو بزن.",
            )

    elif new.status == S.MEMBER and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await core.notify(
            actor.id,
            f"ℹ️ ربات توی <b>{esc(chat.title or str(chat.id))}</b> فقط عضوه. برای دیدن همه‌ی پیام‌ها و ثبت چت، ادمینش کن.",
        )
