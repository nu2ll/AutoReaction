from __future__ import annotations

import time

from telegram import Update

from app import views
from app.handlers.common import Context, answer, core_of, show
from app.handlers.inputs import TOKEN_RE, register_tokens
from app.models import UNLIMITED


# -------------------------------------------------------------------- buttons
async def on_admin(update: Update, context: Context) -> None:
    """a:home | a:users | a:svc:<page> | a:svcadd | a:svcdel:<id> | a:svcok:<id> | a:purge | a:public"""
    core = core_of(context)
    if not core.access.is_owner(update.effective_user.id):
        await answer(update, "⛔️ دسترسی نداری", alert=True)
        return

    _, action, *rest = update.callback_query.data.split(":")
    await answer(update)

    match action:
        case "home":
            await show(update, await views.admin_view(core))
        case "users":
            await show(update, views.users_help_view())
        case "svc":
            await show(update, await views.service_bots_view(core, int(rest[0])))
        case "svcadd":
            context.user_data["input"] = {"kind": "svc_bot"}
            await show(update, views.add_bot_prompt("a:svc:0"))
        case "svcdel":
            record = await core.db.get_bot(int(rest[0]))
            if record is None or record.owner_id is not None:
                await show(update, await views.service_bots_view(core, 0))
                return
            await show(update, views.confirm_delete_bot(record.bot_id, record.username, "a:svc:0", "a:svcok"))
        case "svcok":
            record = await core.db.get_bot(int(rest[0]))
            if record is not None and record.owner_id is None:
                await core.remove_bot(record.bot_id)
            await show(update, await views.service_bots_view(core, 0))
        case "purge":
            removed = await core.db.delete_invalid_service_bots()
            await update.callback_query.message.reply_text(f"🧹 {removed} ربات نامعتبر حذف شد.")
            await show(update, await views.service_bots_view(core, 0))
        case "public":
            await core.access.set_public(not core.access.public)
            await show(update, await views.admin_view(core))


# ------------------------------------------------------------------- commands
async def cmd_admin(update: Update, context: Context) -> None:
    await show(update, await views.admin_view(core_of(context)))


def _uid(context: Context) -> int | None:
    try:
        return int(context.args[0])
    except (IndexError, ValueError):
        return None


async def cmd_grant(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None or len(context.args) < 2:
        await update.message.reply_text("استفاده: <code>/grant ID DAYS|forever [LIMIT]</code>")
        return

    period = context.args[1].lower()
    try:
        limit = int(context.args[2]) if len(context.args) > 2 else 0
        days = None if period in ("forever", "inf") else int(period)
    except ValueError:
        await update.message.reply_text("❌ عدد نامعتبر.")
        return
    if limit < 0 or (days is not None and days <= 0):
        await update.message.reply_text("❌ تعداد روز و محدودیت باید مثبت باشن.")
        return

    user = await core.db.get_user(uid)
    now = int(time.time())
    if days is None:
        until = UNLIMITED
    else:
        base = user.service_until if user and user.service_until > now else now  # extend if still active
        until = base + days * 86400
    user = await core.db.set_service(uid, until, limit)

    await update.message.reply_text(
        f"✅ سرویس برای <code>{uid}</code> فعال شد.\n{views.service_status(core, uid, user)}"
    )
    await core.notify(uid, f"🎉 سرویس ری‌اکشن برات فعال شد!\n{views.service_status(core, uid, user)}")


async def cmd_revoke(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None:
        await update.message.reply_text("استفاده: <code>/revoke ID</code>")
        return
    await core.db.set_service(uid, 0, 0)
    await update.message.reply_text(f"✅ دسترسی سرویس <code>{uid}</code> قطع شد.")


async def _ban(update: Update, context: Context, banned: bool) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None:
        await update.message.reply_text(f"استفاده: <code>/{'ban' if banned else 'unban'} ID</code>")
        return
    if core.access.is_owner(uid):
        await update.message.reply_text("❌ مدیر رو نمی‌شه مسدود کرد.")
        return
    await core.db.set_banned(uid, banned)
    await update.message.reply_text(f"{'⛔️ مسدود شد' if banned else '✅ آزاد شد'}: <code>{uid}</code>")


async def cmd_ban(update: Update, context: Context) -> None:
    await _ban(update, context, True)


async def cmd_unban(update: Update, context: Context) -> None:
    await _ban(update, context, False)


async def cmd_user(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    user = await core.db.get_user(uid) if uid is not None else None
    if user is None:
        await update.message.reply_text("❌ کاربر پیدا نشد. استفاده: <code>/user ID</code>")
        return
    text = views.user_info(
        core, user, await core.db.count_targets(uid), await core.db.count_bots(uid)
    )
    await update.message.reply_text(text)


async def cmd_addbot(update: Update, context: Context) -> None:
    """Service bot(s) via command: /addbot TOKEN [TOKEN ...]"""
    core = core_of(context)
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass
    tokens = TOKEN_RE.findall(" ".join(context.args))
    if not tokens:
        await update.effective_chat.send_message("استفاده: <code>/addbot TOKEN</code>")
        return
    await update.effective_chat.send_message(await register_tokens(core, tokens, None))


async def cmd_delbot(update: Update, context: Context) -> None:
    core = core_of(context)
    if not context.args:
        await update.message.reply_text("استفاده: <code>/delbot @username</code>")
        return
    record = await core.db.find_service_bot(context.args[0])
    if record is None:
        await update.message.reply_text("❌ این ربات توی سرویس نیست.")
        return
    await core.remove_bot(record.bot_id)
    await update.message.reply_text("🗑 حذف شد.")
