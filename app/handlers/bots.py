from __future__ import annotations

from telegram import Update

from app import views
from app.handlers.common import Context, answer, core_of, prompt, show


def _page(rest: list[str], index: int) -> int:
    try:
        return max(0, int(rest[index]))
    except (IndexError, ValueError):
        return 0


async def on_bots(update: Update, context: Context) -> None:
    """b:list[:<page>] | b:add | b:del:<bot id>[:<page>] | b:delok:<bot id>[:<page>]"""
    core = core_of(context)
    uid = update.effective_user.id
    _, action, *rest = update.callback_query.data.split(":")

    if action == "list":
        await answer(update)
        await show(update, await views.bots_view(core, uid, _page(rest, 0)))
        return

    if action == "add":
        await answer(update)
        await prompt(update, context, {"kind": "own_bot"}, views.add_bot_prompt("m:cancel:b"))
        return

    record = await core.db.get_bot(int(rest[0]))
    if record is None or record.owner_id != uid:
        await answer(update, "❌ این ربات پیدا نشد.", alert=True)
        return

    page = _page(rest, 1)
    if action == "del":
        await answer(update)
        await show(update, views.confirm_delete_bot(
            record.bot_id, record.username, f"b:list:{page}", suffix=f":{page}"
        ))
    elif action == "delok":
        # also forget it in every chat of this user (rows cascade in the DB)
        await core.remove_bot(record.bot_id)
        await answer(update, "🗑 حذف شد.")
        await show(update, await views.bots_view(core, uid, page))
