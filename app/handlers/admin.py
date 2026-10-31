from __future__ import annotations

import time

from telegram import InlineKeyboardMarkup as Markup
from telegram import Update

from app import views
from app.handlers.common import Context, answer, core_of, present, prompt, show
from app.handlers.inputs import TOKEN_RE, register_tokens
from app.models import UNLIMITED


# -------------------------------------------------------------------- buttons
async def on_admin(update: Update, context: Context) -> None:
    """a:home | a:helper | a:helperref | a:helperleave | a:helperadd | a:helperdel | a:helperok | a:users | a:svc:<page> | a:svcadd | a:svcdel:<id>[:<page>] | a:svcok:<id>[:<page>] | a:purge | a:public"""
    core = core_of(context)
    if not core.access.is_owner(update.effective_user.id):
        await answer(update, "⛔️ دسترسی نداری", alert=True)
        return

    _, action, *rest = update.callback_query.data.split(":")
    if action != "helperref":  # that one answers the button itself, with a toast
        await answer(update)

    match action:
        case "home":
            await show(update, await views.admin_view(core))
        case "users":
            await show(update, views.users_help_view())
        case "svc":
            await show(update, await views.service_bots_view(core, int(rest[0])))
        case "svcadd":
            await prompt(update, context, {"kind": "svc_bot"}, views.add_bot_prompt("a:svc:0"))
        case "svcdel":
            page = _page(rest)
            record = await core.db.get_bot(int(rest[0]))
            if record is None or record.owner_id is not None:
                await show(update, await views.service_bots_view(core, page))
                return
            await show(update, views.confirm_delete_bot(
                record.bot_id, record.username, f"a:svc:{page}", "a:svcok", suffix=f":{page}"
            ))
        case "svcok":
            record = await core.db.get_bot(int(rest[0]))
            if record is not None and record.owner_id is None:
                await core.remove_bot(record.bot_id)
            await show(update, await views.service_bots_view(core, _page(rest)))
        case "purge":
            removed = await core.db.delete_invalid_service_bots()
            await show(update, views.with_note(
                await views.service_bots_view(core, 0), f"🧹 {removed} ربات نامعتبر حذف شد."
            ))
        case "public":
            await core.access.set_public(not core.access.public)
            await show(update, await views.admin_view(core))
        case "helper":
            await _drop_login(core)
            await show(update, views.helper_view(core))
        case "helperref":
            if core.helper is None:
                await answer(update)
                await show(update, views.helper_view(core))
                return
            try:
                await core.helper.refresh()
            except Exception as exc:  # noqa: BLE001
                await answer(update, f"❌ بروزرسانی نشد: {str(exc)[:150]}", alert=True)
                return
            await answer(update, "✅ وضعیت اکانت بروزرسانی شد")
            await show(update, views.helper_view(core))
        case "helperleave":
            if core.helper is not None:
                await core.set_helper_leave(not core.helper.leave_after)
            await show(update, views.helper_view(core))
        case "helperadd":
            await _drop_login(core)
            cfg = core.config
            if cfg.helper_api_id and cfg.helper_api_hash:
                await prompt(
                    update, context,
                    {"kind": "helper_phone", "api_id": cfg.helper_api_id, "api_hash": cfg.helper_api_hash},
                    views.helper_prompt(views.HELPER_PHONE_PROMPT),
                )
            else:
                await _reply(update, context, "❌ <code>HELPER_API_ID</code> و <code>HELPER_API_HASH</code> توی .env تنظیم نشده.")
        case "helperdel":
            await show(update, views.helper_confirm_delete())
        case "helperok":
            await core.remove_helper()
            await show(update, views.helper_view(core))


def _page(rest: list[str]) -> int:
    """Optional page number after the id in a callback (a:svcdel:<id>:<page>)."""
    try:
        return max(0, int(rest[1]))
    except (IndexError, ValueError):
        return 0


async def _drop_login(core) -> None:
    """Abandon a half-finished phone login (frees its connection)."""
    login, core.helper_login = core.helper_login, None
    if login is not None:
        await login.close()


# ------------------------------------------------------------------- commands
async def _reply(update: Update, context: Context, text: str) -> None:
    """Result of a command: shown on the user's panel message, with a way back."""
    await present(update, context, (text, Markup([views.back("a:home", "🔙 پنل مدیریت")])))


async def cmd_admin(update: Update, context: Context) -> None:
    await present(update, context, await views.admin_view(core_of(context)))


def _uid(context: Context) -> int | None:
    try:
        return int(context.args[0])
    except (IndexError, ValueError):
        return None


async def cmd_grant(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None or len(context.args) < 2:
        await _reply(update, context, "استفاده: <code>/grant ID DAYS|forever [LIMIT]</code>")
        return

    period = context.args[1].lower()
    try:
        limit = int(context.args[2]) if len(context.args) > 2 else 0
        days = None if period in ("forever", "inf") else int(period)
    except ValueError:
        await _reply(update, context, "❌ عدد نامعتبر.")
        return
    if limit < 0 or (days is not None and days <= 0):
        await _reply(update, context, "❌ تعداد روز و محدودیت باید مثبت باشن.")
        return

    user = await core.db.get_user(uid)
    now = int(time.time())
    if days is None:
        until = UNLIMITED
    else:
        base = user.service_until if user and user.service_until > now else now  # extend if still active
        until = base + days * 86400
    user = await core.db.set_service(uid, until, limit)

    await _reply(
        update, context,
        f"✅ سرویس برای <code>{uid}</code> فعال شد.\n{views.service_status(core, uid, user)}",
    )
    await core.notify(uid, f"🎉 سرویس ری‌اکشن برات فعال شد!\n{views.service_status(core, uid, user)}")


async def cmd_revoke(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None:
        await _reply(update, context, "استفاده: <code>/revoke ID</code>")
        return
    await core.db.set_service(uid, 0, 0)
    await _reply(update, context, f"✅ دسترسی سرویس <code>{uid}</code> قطع شد.")


async def _ban(update: Update, context: Context, banned: bool) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None:
        await _reply(update, context, f"استفاده: <code>/{'ban' if banned else 'unban'} ID</code>")
        return
    if core.access.is_owner(uid):
        await _reply(update, context, "❌ مدیر رو نمی‌شه مسدود کرد.")
        return
    await core.db.set_banned(uid, banned)
    await _reply(update, context, f"{'⛔️ مسدود شد' if banned else '✅ آزاد شد'}: <code>{uid}</code>")


async def cmd_ban(update: Update, context: Context) -> None:
    await _ban(update, context, True)


async def cmd_unban(update: Update, context: Context) -> None:
    await _ban(update, context, False)


async def cmd_user(update: Update, context: Context) -> None:
    core = core_of(context)
    uid = _uid(context)
    if uid is None:
        await _reply(update, context, "استفاده: <code>/user ID</code>")
        return
    user = await core.db.get_user(uid)
    if user is None:
        await _reply(update, context, "❌ کاربر پیدا نشد.")
        return
    text = views.user_info(
        core, user, await core.db.count_targets(uid), await core.db.count_bots(uid)
    )
    await _reply(update, context, text)


async def cmd_addbot(update: Update, context: Context) -> None:
    """Service bot(s) via command: /addbot TOKEN [TOKEN ...]"""
    core = core_of(context)
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass
    tokens = TOKEN_RE.findall(" ".join(context.args))
    if not tokens:
        await _reply(update, context, "استفاده: <code>/addbot TOKEN</code>")
        return
    await _reply(update, context, await register_tokens(core, tokens, None))


async def cmd_delbot(update: Update, context: Context) -> None:
    core = core_of(context)
    if not context.args:
        await _reply(update, context, "استفاده: <code>/delbot @username</code>")
        return
    record = await core.db.find_service_bot(context.args[0])
    if record is None:
        await _reply(update, context, "❌ این ربات توی سرویس نیست.")
        return
    await core.remove_bot(record.bot_id)
    await _reply(update, context, "🗑 حذف شد.")
