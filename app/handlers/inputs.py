"""Free-text answers to prompts the bot showed (token, chat id, emoji list)."""
from __future__ import annotations

import re

from telegram import Update
from telegram.error import TelegramError

from app import chats, views
from app.core import BotRegistrationError, Core
from app.emoji import parse_emojis
from app.handlers.common import Context, core_of, edit_panel
from app.helper import HelperLogin, LoginError
from app.models import Mode

login_factory = HelperLogin  # replaceable in tests
TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")


async def register_tokens(core: Core, tokens: list[str], owner_id: int | None) -> str:
    """Register every token, return a short summary: one line for the successes, one per failure."""
    added: list[str] = []
    errors: list[str] = []
    for token in dict.fromkeys(tokens):  # de-duplicate, keep order
        try:
            bot = await core.register_bot(token, owner_id)
        except BotRegistrationError as exc:
            errors.append(f"{exc}\n<code>{token[:10]}…</code>")
        else:
            added.append(f"@{bot.username}")
    lines: list[str] = []
    if added:
        lines.append(f"✅ {added[0]} اضافه شد." if len(added) == 1 else f"✅ {len(added)} ربات اضافه شد.")
    lines += errors[:6]
    if len(errors) > 6:
        lines.append(f"… و {len(errors) - 6} خطای دیگه")
    return "\n".join(lines)


async def on_text(update: Update, context: Context) -> None:
    state = context.user_data.get("input")
    if not state:
        await _delete(update.effective_message)  # nothing was asked: keep the chat clean
        return

    core = core_of(context)
    message = update.effective_message
    uid = update.effective_user.id
    kind = state["kind"]

    if kind in ("own_bot", "svc_bot"):
        # the message holds secrets: remove it right away
        await _delete(message)

        tokens = TOKEN_RE.findall(message.text or "")
        if not tokens:
            await _error(update, state, "❌ توکن معتبری توی پیامت پیدا نکردم.\nدوباره بفرست یا /cancel.")
            return

        if kind == "svc_bot" and not core.access.is_owner(uid):
            return
        context.user_data.pop("input", None)
        owner = uid if kind == "own_bot" else None
        summary = await register_tokens(core, tokens, owner)

        if kind == "own_bot":
            targets = [t for t in await core.db.list_targets(uid) if t.uses_own]
            view = await views.bots_view(core, uid)
            if targets:
                summary += "\n⏳ در حال ادمین‌کردن ربات توی چت‌هات…"
        else:
            targets, view = [], await views.service_bots_view(core, 0)
        panel = await edit_panel(update, state.get("panel"), views.with_note(view, summary))
        if targets:
            chat_id = getattr(message, "chat_id", None)
            core.spawn(chats.provision_many(core, targets, (chat_id, panel) if chat_id and panel else None, uid))
        return

    if kind.startswith("helper_"):
        if core.access.is_owner(uid):
            await _helper_step(core, context, update, state)
        else:
            context.user_data.pop("input", None)
        return

    if kind == "chat_ref":
        await _delete(message)
        ref = chats.parse_chat_ref(message.text or "")
        if ref is None:
            await _error(update, state, "❌ فرمت درست نیست.\nمثل <code>@mychat</code> یا آیدی عددی بفرست.")
            return
        try:
            chat = await chats.resolve_chat(core, uid, ref)
            target = await chats.register_chat(core, uid, chat)
        except chats.ChatError as exc:
            await _error(update, state, str(exc))
            return

        context.user_data.pop("input", None)
        view = views.with_note(await views.target_view(core, target), "✅ چت ثبت شد. در حال ادمین‌کردن ربات‌ها…")
        panel = await edit_panel(update, state.get("panel"), view)
        chat_id = getattr(message, "chat_id", None)
        # the report later replaces this same panel message
        core.spawn(chats.provision_and_notify(core, target, (chat_id, panel) if chat_id and panel else None))
        return

    if kind == "emojis":
        await _delete(message)
        emojis = parse_emojis(message.text or "")
        if not emojis:
            await _error(
                update, state,
                "❌ ایموجی مجاز پیدا نکردم.\nفقط ایموجی‌های ری‌اکشن تلگرام قبول می‌شن،\nمثلاً 🔥 ❤️ 👍",
            )
            return

        target = await core.db.get_target(state["target"])
        if target is None or not core.access.can_manage(uid, target):
            context.user_data.pop("input", None)
            return
        await core.db.update_target(target.id, emojis=emojis, mode=Mode.CUSTOM)
        context.user_data.pop("input", None)
        target = await core.db.get_target(target.id)
        view = views.with_note(await views.target_view(core, target), f"✅ ایموجی‌ها اضافه شد: {' '.join(emojis)}")
        await edit_panel(update, state.get("panel"), view)


async def _error(update: Update, state: dict, text: str) -> None:
    """A wrong answer: the prompt message itself turns into the error (with the cancel button)."""
    panel = await edit_panel(update, state.get("panel"), (text, views.cancel_markup(state)))
    if panel:
        state["panel"] = panel


async def on_other(update: Update, context: Context) -> None:
    """Anything else typed in the private chat (stickers, photos, unknown commands): removed."""
    await _delete(update.effective_message)


async def _delete(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


# ------------------------------------------------------- helper account (admin)
async def _helper_step(core: Core, context: Context, update: Update, state: dict) -> None:
    """phone -> code -> (2FA password); then the account is connected."""
    message = update.effective_message
    kind = state["kind"]
    text = (message.text or "").strip()

    async def note(text: str) -> None:
        await _error(update, state, text)

    await _delete(message)  # every answer is removed (most of them are secrets)

    async def ask(next_kind: str, prompt: str, **data) -> None:
        """Turn the previous prompt into the next one (same message)."""
        new_state = {"kind": next_kind, **data}
        panel = await edit_panel(update, state.get("panel"), (prompt, views.cancel_markup(new_state)))
        if panel:
            new_state["panel"] = panel
        context.user_data["input"] = new_state

    async def fail(exc: LoginError) -> None:
        if exc.retry:
            await note(str(exc))
            return
        context.user_data.pop("input", None)
        await _close_login(core)
        await edit_panel(update, state.get("panel"), views.helper_prompt(str(exc)))

    if kind == "helper_phone":
        await _close_login(core)
        login = login_factory(state["api_id"], state["api_hash"])
        try:
            await login.send_code(text.replace(" ", ""))
        except LoginError as exc:
            if exc.retry:
                await note(str(exc))
            else:
                await fail(exc)
            return
        core.helper_login = login
        await ask(
            "helper_code",
            "💬 تلگرام یه کد به اون اکانت فرستاد. کد رو بفرست،\n"
            "<b>ولی رقم‌ها رو با فاصله یا خط تیره جدا کن</b>\n"
            "(مثل <code>1-2-3-4-5</code>)، وگرنه تلگرام کد رو باطل می‌کنه.",
        )
        return

    login = core.helper_login
    if login is None:
        context.user_data.pop("input", None)
        await note("❌ نشست ورود منقضی شد.\nاز 👤 اکانت کمکی دوباره شروع کن.")
        return

    try:
        if kind == "helper_code":
            digits = re.sub(r"\D", "", text)
            if not digits:
                await note("❌ کد رو بفرست (مثل 1-2-3-4-5).")
                return
            if await login.sign_in_code(digits):
                await ask("helper_pass", "🔐 این اکانت رمز دو مرحله‌ای داره.\nرمز رو بفرست (پیامت پاک می‌شه).")
                return
        else:  # helper_pass
            await login.sign_in_password(text)
    except LoginError as exc:
        await fail(exc)
        return

    context.user_data.pop("input", None)
    session = login.session_string()
    api_id, api_hash = login.api_id, login.api_hash
    await _close_login(core)
    try:
        helper = await core.set_helper(api_id, api_hash, session)
    except Exception as exc:  # noqa: BLE001
        await note(f"❌ اتصال اکانت نشد: {str(exc)[:100]}")
        return
    await edit_panel(
        update, state.get("panel"),
        views.with_note(views.helper_view(core), f"✅ اکانت {views.esc(helper.label)} متصل شد."),
    )


async def _close_login(core: Core) -> None:
    login, core.helper_login = core.helper_login, None
    if login is not None:
        await login.close()
