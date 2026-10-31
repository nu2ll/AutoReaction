from __future__ import annotations

from telegram import InlineKeyboardMarkup as Markup
from telegram import Update

from app import chats, views
from app.handlers.common import Context, answer, core_of, prompt, respond, show
from app.access import MAX_SERVICE_PER_CHAT
from app.models import Mode, Source, Target


async def _load(update: Update, context: Context, target_id: int) -> Target | None:
    core = core_of(context)
    target = await core.db.get_target(target_id)
    if target is None or not core.access.can_manage(update.effective_user.id, target):
        await answer(update, "❌ این چت پیدا نشد یا دسترسی نداری.", alert=True)
        return None
    return target


async def _show_target(update: Update, context: Context, target_id: int) -> None:
    core = core_of(context)
    target = await core.db.get_target(target_id)
    if target:
        await show(update, await views.target_view(core, target))


async def on_target(update: Update, context: Context) -> None:
    """t:<action>[:<target id>[:<arg>]]"""
    core = core_of(context)
    query = update.callback_query
    uid = update.effective_user.id
    _, action, *rest = query.data.split(":")

    if action == "list":
        await answer(update)
        await show(update, await views.targets_view(core, uid))
        return

    if action == "add":
        await answer(update)
        await prompt(update, context, {"kind": "chat_ref"}, views.add_chat_prompt())
        return

    target = await _load(update, context, int(rest[0]))
    if target is None:
        return
    arg = rest[1] if len(rest) > 1 else None

    match action:
        case "open":
            await answer(update)

        case "tog":
            await core.db.update_target(target.id, enabled=not target.enabled)
            await answer(update)

        case "src":
            user = await core.db.get_user(target.owner_id)
            options = (
                [Source.BOTH, Source.OWN, Source.SERVICE]
                if core.access.service_allowed(target.owner_id, user)
                else [Source.OWN]
            )
            nxt = options[(options.index(target.source) + 1) % len(options)] if target.source in options else options[0]
            await core.db.update_target(target.id, source=nxt)
            await answer(update)

        case "mode":
            if target.mode is Mode.CUSTOM:
                await core.db.update_target(target.id, mode=Mode.RANDOM)
            elif target.emojis:
                await core.db.update_target(target.id, mode=Mode.CUSTOM)
            else:
                await answer(update)
                await prompt(update, context, {"kind": "emojis", "target": target.id}, views.emoji_prompt(target))
                return
            await answer(update)

        case "emo":
            await answer(update)
            await prompt(update, context, {"kind": "emojis", "target": target.id}, views.emoji_prompt(target))
            return

        case "ch":
            value = max(0, min(100, target.chance + int(arg)))
            await core.db.update_target(target.id, chance=value)
            await answer(update)

        case "sc":
            value = max(0, min(MAX_SERVICE_PER_CHAT, target.service_count + int(arg)))
            await core.db.update_target(target.id, service_count=value)
            await answer(update)

        case "prov":
            await answer(update)
            await respond(update, "⏳ در حال ادمین‌کردن ربات‌ها…")
            report = await chats.provision(core, target, always_helper=True)
            await show(update, (report.render(), Markup([views.back(f"t:open:{target.id}")])))
            return

        case "chk":
            await answer(update)
            await respond(update, "⏳ در حال بررسی…")
            text = await chats.check_target(core, target)
            await show(update, (text, Markup([views.back(f"t:open:{target.id}")])))
            return

        case "del":
            await answer(update)
            await show(update, views.confirm_delete_target(target))
            return

        case "delok":
            assigned = await core.db.assigned_bot_ids(target.id)
            service_ids = [b for b in assigned if core.pool.has_owner(b) and core.pool.owner_of(b) is None]
            await core.db.delete_target(target.id)
            if service_ids:
                core.spawn(chats.release_bots(core, target.chat_id, service_ids))
            await answer(update, "🗑 حذف شد.")
            await show(update, await views.targets_view(core, uid))
            return

        case _:
            await answer(update)
            return

    await _show_target(update, context, target.id)
