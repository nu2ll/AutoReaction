from __future__ import annotations

from telegram import Update

from app import views
from app.handlers.common import Context, answer, core_of, show


async def on_bots(update: Update, context: Context) -> None:
    """b:list | b:add | b:del:<bot id> | b:delok:<bot id>"""
    core = core_of(context)
    uid = update.effective_user.id
    _, action, *rest = update.callback_query.data.split(":")

    if action == "list":
        await answer(update)
        await show(update, await views.bots_view(core, uid))
        return

    if action == "add":
        await answer(update)
        context.user_data["input"] = {"kind": "own_bot"}
        await show(update, views.add_bot_prompt("m:cancel:b"))
        return

    record = await core.db.get_bot(int(rest[0]))
    if record is None or record.owner_id != uid:
        await answer(update, "❌ این ربات پیدا نشد.", alert=True)
        return

    if action == "del":
        await answer(update)
        await show(update, views.confirm_delete_bot(record.bot_id, record.username))
    elif action == "delok":
        # also forget it in every chat of this user (rows cascade in the DB)
        await core.remove_bot(record.bot_id)
        await answer(update, "🗑 حذف شد.")
        await show(update, await views.bots_view(core, uid))
