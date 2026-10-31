from __future__ import annotations

from telegram import InlineKeyboardMarkup as Markup
from telegram import Update

from app import views
from app.handlers.common import Context, answer, core_of, present, show


async def _home(update: Update, context: Context) -> None:
    core = core_of(context)
    user = await core.db.touch_user(
        update.effective_user.id, update.effective_user.username, update.effective_user.full_name
    )
    view = await views.home_view(core, user)
    if update.callback_query:
        await show(update, view)
    else:
        await present(update, context, view)


async def cmd_start(update: Update, context: Context) -> None:
    await _home(update, context)


async def cmd_help(update: Update, context: Context) -> None:
    await present(update, context, views.help_view())


async def cmd_cancel(update: Update, context: Context) -> None:
    context.user_data.pop("input", None)
    await present(update, context, ("✖️ لغو شد.", Markup([views.back("m:home", "🔙 منوی اصلی")])))


async def on_menu(update: Update, context: Context) -> None:
    """m:home | m:help | m:cancel:<t|b>"""
    action, *rest = update.callback_query.data.split(":")[1:]
    await answer(update)
    core = core_of(context)

    if action == "help":
        await show(update, views.help_view())
    elif action == "cancel":
        context.user_data.pop("input", None)
        if rest and rest[0] == "t":
            await show(update, await views.targets_view(core, update.effective_user.id))
        elif rest and rest[0] == "b":
            await show(update, await views.bots_view(core, update.effective_user.id))
        else:
            await _home(update, context)
    else:
        await _home(update, context)
