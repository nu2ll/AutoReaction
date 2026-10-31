"""Registering chats and getting the reaction bots into them (make them admins)."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from telegram import Bot, Chat
from telegram.constants import ChatMemberStatus as S
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.core import Core
from app.emoji import norm
from app.helper import ABSENT, FRESH_MARK, NO_RIGHTS, READY
from app.models import Mode, Target
from app.reactor import chat_reactions
from app.utils import seconds

log = logging.getLogger(__name__)

ACTIVE = frozenset({S.OWNER, S.ADMINISTRATOR, S.MEMBER})
ADMINS = frozenset({S.OWNER, S.ADMINISTRATOR})
ADMIN_LIMIT = 50  # Telegram: at most 50 admins per chat, bots included
SUPPORTED_CHATS = frozenset({ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP})
esc = html.escape


class ChatError(Exception):
    """Raised with a user-facing (Persian) message."""


class Outcome(Enum):
    OK = "ok"
    FAIL = "fail"  # this bot can't be added; try another one
    FATAL = "fatal"  # nothing will work (no rights / admin limit); stop


# ------------------------------------------------------------------ registration
def parse_chat_ref(text: str) -> int | str | None:
    text = text.strip()
    if re.fullmatch(r"-?\d{5,}", text):
        return int(text)
    m = re.fullmatch(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})/?", text)
    if m:
        return f"@{m.group(1)}"
    m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,})", text)
    if m:
        return f"@{m.group(1)}"
    return None


async def resolve_chat(core: Core, user_id: int, ref: int | str) -> Chat:
    """Find the chat and make sure the main bot is admin there and the user manages it."""
    main = core.main
    assert main is not None
    try:
        chat = await main.get_chat(ref)
    except TelegramError as exc:
        raise ChatError(
            "❌ چت پیدا نشد. ربات اصلی باید داخلش ادمین باشه.\n"
            "برای چت خصوصی آیدی عددی (مثل -100123...) بفرست."
        ) from exc

    if chat.type not in SUPPORTED_CHATS:
        raise ChatError("❌ فقط کانال و گروه پشتیبانی می‌شه.")

    try:
        me = await main.get_chat_member(chat.id, main.id)
    except TelegramError as exc:
        raise ChatError("❌ ربات اصلی توی این چت نیست.") from exc
    if me.status not in ADMINS:
        raise ChatError("❌ اول ربات اصلی رو توی اون چت ادمین کن.")

    if not core.access.is_owner(user_id):
        try:
            member = await main.get_chat_member(chat.id, user_id)
        except TelegramError as exc:
            raise ChatError("❌ نتونستم دسترسی تو رو توی اون چت بررسی کنم.") from exc
        if member.status not in ADMINS:
            raise ChatError("❌ فقط ادمین‌های اون چت می‌تونن ثبتش کنن.")
    return chat


async def register_chat(core: Core, owner_id: int, chat: Chat) -> Target:
    existing = await core.db.get_target_by_chat(chat.id)
    if existing:
        if existing.owner_id == owner_id:
            return existing
        raise ChatError("❌ این چت قبلاً توسط کاربر دیگه‌ای ثبت شده.")

    if (
        not core.access.is_owner(owner_id)
        and await core.db.count_targets(owner_id) >= core.config.max_targets_per_user
    ):
        raise ChatError(f"❌ سقف تعداد چت ({core.config.max_targets_per_user}) پر شده.")

    title = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
    return await core.db.create_target(chat.id, title, str(chat.type), owner_id)


# ---------------------------------------------------------------- telegram calls
async def _call(factory, retries: int = 2):
    """Run a Telegram call, waiting out flood limits."""
    for attempt in range(retries):
        try:
            return await factory()
        except RetryAfter as exc:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(seconds(exc.retry_after) + 1)


async def _status(main: Bot, chat_id: int, user_id: int) -> str:
    try:
        member = await _call(lambda: main.get_chat_member(chat_id, user_id))
    except TelegramError:
        return "unknown"
    return str(member.status)


def classify(message: str) -> tuple[Outcome, str]:
    m = message.lower()
    if any(k in m for k in ("not enough rights", "chat_admin_required", "administrator rights")):
        return Outcome.FATAL, "ربات اصلی دسترسی «افزودن ادمین» نداره."
    if any(k in m for k in ("admins_too_much", "too many admin")):
        return Outcome.FATAL, "سقف ادمین‌های این چت پر شده."
    if any(k in m for k in ("participant", "user not found", "member")):
        return Outcome.FAIL, "ربات عضو چت نیست"
    return Outcome.FAIL, message


async def _promote(main: Bot, chat_id: int, bot_id: int) -> tuple[Outcome, str]:
    try:
        # can_manage_chat is the lightest right that still makes the bot an admin.
        await _call(lambda: main.promote_chat_member(chat_id, bot_id, can_manage_chat=True))
    except (Forbidden, BadRequest) as exc:
        return classify(exc.message)
    except TelegramError as exc:
        return Outcome.FAIL, str(exc)
    return Outcome.OK, ""


async def _ensure(core: Core, target: Target, bot: Bot, promote) -> tuple[Outcome, str]:
    """Make sure `bot` is in the chat (promoting it if needed) and assign it to the target.

    `promote` is an async callable (bot) -> (Outcome, reason), or None if nobody can promote."""
    assert core.main is not None
    if await _status(core.main, target.chat_id, bot.id) in ACTIVE:
        await core.db.assign(target.id, bot.id)
        return Outcome.OK, ""
    if promote is None:
        return Outcome.FAIL, "باید دستی ادمین بشه"

    outcome, why = await promote(bot)
    if outcome is Outcome.OK:
        await core.db.assign(target.id, bot.id)
    return outcome, why


async def release_bots(core: Core, chat_id: int, bot_ids: list[int]) -> None:
    """Best effort: take admin rights back from service bots we added to a chat."""
    assert core.main is not None
    for bot_id in bot_ids:
        try:
            await _call(lambda b=bot_id: core.main.promote_chat_member(chat_id, b, can_manage_chat=False))
        except TelegramError as exc:
            log.debug("could not demote %s in %s: %s", bot_id, chat_id, exc)
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------- provisioning
@dataclass(slots=True)
class Report:
    title: str
    lines: list[str] = field(default_factory=list)
    fatal: str | None = None
    needs_helper: bool = False  # some bot could not be added
    keep_helper: bool = False  # Telegram's "fresh admin" limit: do not leave, retry later

    def render(self) -> str:
        out = [f"🛡 <b>{esc(self.title)}</b>", ""]
        out += self.lines
        if self.fatal:
            out += ["", f"⛔️ {self.fatal}"]
        return "\n".join(out)


def _tally(label: str, ok: int, total: int, failures: list[str]) -> list[str]:
    lines = [f"{label}: ✅ <b>{ok}</b> از <b>{total}</b>"]
    if failures:
        lines.append("")
        lines += [f"   ❌ {f}" for f in failures[:8]]
        if len(failures) > 8:
            lines.append(f"   … و {len(failures) - 8} مورد دیگه")
    return lines


async def admins_in_chat(core: Core, target: Target) -> int | None:
    """Estimated admin count seen by the main bot: the human admins + itself + the bots we put there.
    (Bots someone else made admin are not visible to it.) None when it can't be told."""
    try:
        admins = await _call(lambda: core.main.get_chat_administrators(target.chat_id))
        humans = sum(1 for m in admins if not m.user.is_bot)  # the bot list is not reliable here
        return humans + 1 + len(await core.db.assigned_bot_ids(target.id))
    except Exception as exc:  # noqa: BLE001
        log.debug("could not count the admins of %s: %s", target.chat_id, exc)
        return None


async def _free_admin_slots(core: Core, target: Target, helper) -> int | None:
    """How many more admins the chat can take (50 minus the current ones, bots included).
    None when the number can't be found out."""
    if helper is not None:  # the helper account sees every admin, bots too
        try:
            count = await helper.admin_count(target.chat_id)
        except Exception:  # noqa: BLE001
            count = None
        if count is not None:
            return max(0, ADMIN_LIMIT - count)
    count = await admins_in_chat(core, target)
    return None if count is None else max(0, ADMIN_LIMIT - count)


async def _bring_helper(core: Core, target: Target, helper) -> tuple[str, str | None, bool]:
    """Get the helper account into the chat and make it an admin - all done by the main bot:
    unban it if it was banned, let it join through a one-use invite link, then promote it.

    Returns (helper state, problem or None, whether the account is now in the chat)."""
    assert core.main is not None
    main, chat_id = core.main, target.chat_id
    try:
        me = await _call(lambda: main.get_chat_member(chat_id, main.id))
    except TelegramError:
        return ABSENT, None, False
    if me.status not in ADMINS:
        return ABSENT, "ربات اصلی توی چت ادمین نیست؛\nنمی‌تونه اکانت کمکی رو وارد کنه.", False
    if not getattr(me, "can_promote_members", False):
        return ABSENT, "ربات اصلی دسترسی «افزودن ادمین» نداره؛\nنمی‌تونه اکانت کمکی رو ادمین کنه.", False

    try:
        member = await _call(lambda: main.get_chat_member(chat_id, helper.user_id))
    except TelegramError:
        member = None
    status = getattr(member, "status", None)
    in_chat = status in ACTIVE or bool(status == "restricted" and getattr(member, "is_member", False))

    if status == S.BANNED:  # banned from this chat: let it back in
        try:
            await _call(lambda: main.unban_chat_member(chat_id, helper.user_id, only_if_banned=True))
        except TelegramError as exc:
            log.debug("could not unban the helper in %s: %s", chat_id, exc)
            return ABSENT, "اکانت کمکی توی چت مسدوده\nو ربات اصلی نتونست آزادش کنه.", False

    if not in_chat:
        try:
            link = await _call(
                lambda: main.create_chat_invite_link(chat_id, member_limit=1, expire_date=int(time.time()) + 600)
            )
        except TelegramError as exc:
            log.debug("no invite link for %s: %s", chat_id, exc)
            return ABSENT, "ربات اصلی نتونست لینک دعوت بسازه\n(دسترسی «دعوت کاربران» لازمه).", False
        ok, why = await helper.join(link.invite_link)
        if not ok:
            return ABSENT, f"اکانت کمکی نتونست وارد چت بشه ({why}).", False

    if not (status == S.ADMINISTRATOR and getattr(member, "can_promote_members", False)):
        free = await _free_admin_slots(core, target, None)
        if free is not None and free < 1:
            return NO_RIGHTS, f"سقف {ADMIN_LIMIT} ادمین این چت پر شده؛\nجایی برای اکانت کمکی نیست.", True
        try:
            await _call(
                lambda: main.promote_chat_member(
                    chat_id, helper.user_id, can_manage_chat=True, can_promote_members=True
                )
            )
        except TelegramError as exc:
            log.debug("could not promote the helper in %s: %s", chat_id, exc)
            return NO_RIGHTS, "ربات اصلی نتونست اکانت کمکی رو ادمین کنه.", True

    state = ABSENT
    for _ in range(3):  # the new rights can take a moment to show up
        state = await helper.status(chat_id)
        if state == READY:
            break
        await asyncio.sleep(1)
    return state, None, True


async def provision(core: Core, target: Target, always_helper: bool = False) -> Report:
    """Put every bot this chat should use into it, as admin, and record the assignment.

    A helper account (if configured) does the promoting for bots that are not members yet. The main
    bot brings it into the chat and makes it admin - when `always_helper` is set (the 🛡 button) at
    the start of every run, otherwise only when some bot could not be added without it. It leaves
    the chat again afterwards."""
    helper = core.helper
    helper_state = await helper.status(target.chat_id) if helper else None
    helper_ready = helper_state == READY
    in_chat = helper_state in (READY, NO_RIGHTS)  # it is in the chat already
    note = None
    tried = False
    report = None
    try:
        if helper and not helper_ready and always_helper:
            tried = True
            helper_state, note, joined = await _bring_helper(core, target, helper)
            in_chat = in_chat or joined
            if helper_state == READY:
                helper_ready, note = True, None
        report = await _provision(core, target, helper_ready)
        if helper and not helper_ready and not tried and report.needs_helper:
            helper_state, note, joined = await _bring_helper(core, target, helper)
            in_chat = in_chat or joined
            if helper_state == READY:
                helper_ready, note = True, None
                report = await _provision(core, target, True)  # again, now with the helper's help
    finally:
        keep = report is not None and report.keep_helper
        left = bool(in_chat and helper and helper.leave_after and not keep and await helper.leave(target.chat_id))
    if left:
        report.lines += ["", "👋 اکانت کمکی کارش تموم شد و از چت خارج شد."]
    elif helper and not helper_ready and report.needs_helper:
        if note:
            report.lines += ["", f"⚠️ {note}"]
        report.lines += [
            "",
            f"💡 اکانت {helper.mention} رو توی این چت ادمین کن",
            "(با دسترسی «افزودن ادمین») و دوباره 🛡 رو بزن.",
            "خودش ربات‌ها رو اضافه و ادمین می‌کنه",
            "و بعدش خارج می‌شه.",
        ]
        if helper_state == NO_RIGHTS:
            report.lines.append("(الان توی چت هست ولی دسترسی «افزودن ادمین» نداره.)")
    return report


async def _provision(core: Core, target: Target, helper_ready: bool) -> Report:
    assert core.main is not None
    main = core.main
    helper = core.helper
    report = Report(f"ادمین‌کردن ربات‌ها — {target.title}")

    try:
        me = await _call(lambda: main.get_chat_member(target.chat_id, main.id))
    except TelegramError:
        report.fatal = "ربات اصلی توی این چت نیست."
        return report
    main_can = me.status in ADMINS and bool(getattr(me, "can_promote_members", False))
    can_promote = main_can or helper_ready
    promote = None
    if helper_ready:
        async def promote(bot: Bot) -> tuple[Outcome, str]:
            kind, why = await helper.promote(target.chat_id, bot.username, bot.id)
            return Outcome(kind), why
    elif main_can:
        async def promote(bot: Bot) -> tuple[Outcome, str]:
            return await _promote(main, target.chat_id, bot.id)
    else:
        if target.chat_type == ChatType.CHANNEL:
            report.lines.append(
                "ℹ️ ربات اصلی دسترسی «افزودن ادمین» نداره؛\nفقط رباتی که خودت اضافه کرده باشی شناسایی می‌شه."
            )
        else:
            report.lines.append(
                "ℹ️ توی گروه فقط اکانت کمکی می‌تونه ربات‌ها رو عضو کنه؛ فقط رباتی که خودت عضو کرده باشی شناسایی می‌شه."
            )

    if promote is not None:
        # add only as many bots as the chat can still take
        free = await _free_admin_slots(core, target, helper if helper_ready else None)
        if free is not None:
            real_promote, room = promote, [free]

            async def promote(bot: Bot) -> tuple[Outcome, str]:
                if room[0] <= 0:
                    return Outcome.FATAL, f"سقف {ADMIN_LIMIT} ادمین این چت پر شده"
                outcome, why = await real_promote(bot)
                if outcome is Outcome.OK:
                    room[0] -= 1
                return outcome, why

    user = await core.db.get_user(target.owner_id)
    candidates = 0

    # ---- the user's own bots
    if target.uses_own:
        own = core.pool.of_user(target.owner_id)
        candidates += len(own)
        ok, failures = 0, []
        for bot in own:
            outcome, why = await _ensure(core, target, bot, promote)
            if outcome is Outcome.OK:
                ok += 1
            else:
                failures.append(f"@{bot.username}: ({esc(why)})")
                if outcome is Outcome.FATAL:
                    report.fatal = why
                    report.keep_helper = why.startswith(FRESH_MARK)
                    break
            await asyncio.sleep(0.05)
        if own:
            report.lines += _tally("🤖 ربات‌های خودت", ok, len(own), failures)
            report.needs_helper |= bool(failures)

    # ---- the service bots
    if target.uses_service and core.access.service_allowed(target.owner_id, user) and not report.fatal:
        want = core.access.effective_service_count(target, user)
        pool_ids = {b.id: b for b in core.pool.service()}
        assigned = [bid for bid in await core.db.assigned_bot_ids(target.id) if bid in pool_ids]
        candidates += want

        extra = len(assigned) - want
        if extra > 0:  # the user lowered the number: give the surplus back
            surplus = random.sample(assigned, extra)
            for bot_id in surplus:
                await core.db.unassign(target.id, bot_id)
            core.spawn(release_bots(core, target.chat_id, surplus))
            assigned = [b for b in assigned if b not in surplus]

        need = want - len(assigned)
        failures: list[str] = []
        added = 0
        if need > 0 and can_promote:
            spare = [b for bid, b in pool_ids.items() if bid not in assigned]
            random.shuffle(spare)
            for bot in spare:
                if added >= need:
                    break
                outcome, why = await _ensure(core, target, bot, promote)
                if outcome is Outcome.OK:
                    added += 1
                else:
                    failures.append(f"@{bot.username}: ({esc(why)})")
                    if outcome is Outcome.FATAL:
                        report.fatal = why
                        report.keep_helper = why.startswith(FRESH_MARK)
                        break
                await asyncio.sleep(0.05)
        elif need > 0:
            if target.chat_type == ChatType.CHANNEL:
                failures.append("برای اضافه‌کردن ربات سرویس،\nدسترسی «افزودن ادمین» لازمه.")
            else:
                failures.append("برای اضافه‌کردن ربات سرویس، اکانت کمکی لازمه.")
            report.needs_helper = True

        if want:
            report.lines += _tally("🏢 ربات‌های سرویس", len(assigned) + added, want, failures)
            report.needs_helper |= bool(failures)
            if need - added > 0 and len(pool_ids) < want:
                report.lines.append(f"   ⚠️ فقط {len(pool_ids)} ربات توی سرویس موجوده.")

    if candidates == 0 and not report.fatal:
        report.lines.append(
            "رباتی برای اضافه‌کردن نیست. یا ربات خودت رو توی\n«🤖 ربات‌های من» اضافه کن، "
            "یا سرویس رو فعال کن."
        )
    return report


async def provision_and_notify(core: Core, target: Target, panel: tuple[int, int] | None = None) -> None:
    """Run the provisioning in the background. With `panel` = (chat id, message id) the report
    replaces that panel message; otherwise it is sent as a notification."""
    report = await provision(core, target)
    if panel is not None:
        from telegram import InlineKeyboardButton as Btn
        from telegram import InlineKeyboardMarkup as Markup

        open_btn = Markup([[Btn("⚙️ تنظیمات چت", callback_data=f"t:open:{target.id}")]])
        try:
            await core.main.edit_message_text(
                report.render(), chat_id=panel[0], message_id=panel[1], reply_markup=open_btn
            )
            return
        except TelegramError as exc:
            log.debug("could not edit the panel with the report: %s", exc)
    await core.notify(target.owner_id, report.render())


# ------------------------------------------------------------------------- check
async def check_target(core: Core, target: Target) -> str:
    """Report who is really in the chat and sync the assignments with reality."""
    assert core.main is not None
    main = core.main
    lines = [f"🔍 <b>{esc(target.title)}</b>", ""]

    try:
        me = await _call(lambda: main.get_chat_member(target.chat_id, main.id))
        rights = "✅" if getattr(me, "can_promote_members", False) else "❌"
        lines.append(f"ربات اصلی: {'✅' if me.status in ADMINS else '❌'} {me.status} · افزودن ادمین: {rights}")
    except TelegramError:
        lines.append("ربات اصلی: ❌ توی چت نیست")

    user = await core.db.get_user(target.owner_id)
    assigned = await core.db.assigned_bot_ids(target.id)
    own = core.pool.of_user(target.owner_id) if target.uses_own else []
    service = [b for b in core.pool.service() if b.id in assigned]

    gate = asyncio.Semaphore(8)

    async def probe(bot: Bot) -> tuple[Bot, str]:
        async with gate:
            return bot, await _status(main, target.chat_id, bot.id)

    own_res = await asyncio.gather(*(probe(b) for b in own))
    svc_res = await asyncio.gather(*(probe(b) for b in service))

    for bot, status in (*own_res, *svc_res):
        if status in ACTIVE:
            await core.db.assign(target.id, bot.id)
        else:
            await core.db.unassign(target.id, bot.id)

    if own_res:
        lines += ["", "<b>🤖 ربات‌های خودت</b>"]
        lines += [f"{'✅' if s in ACTIVE else '❌'} @{b.username}: {s}" for b, s in own_res]

    if target.uses_service:
        if not core.access.service_allowed(target.owner_id, user):
            lines += ["", "🏢 سرویس: ❌ دسترسی نداری"]
        else:
            good = sum(1 for _, s in svc_res if s in ACTIVE)
            want = core.access.effective_service_count(target, user)
            lines += ["", f"🏢 ربات‌های سرویس: ✅ <b>{good}</b> از <b>{want}</b>"]
            lines += [f"❌ @{b.username}: {s}" for b, s in svc_res if s not in ACTIVE][:8]

    try:
        allowed = await chat_reactions(main, target.chat_id)
    except TelegramError:
        allowed = ()
    if not allowed:
        lines += ["", "⚠️ ری‌اکشن توی این چت غیرفعاله یا لیستش خالیه."]
    elif target.mode is Mode.CUSTOM:
        have = {norm(getattr(r, "emoji", "")) for r in allowed}
        missing = [e for e in target.emojis if norm(e) not in have]
        if missing:
            lines += ["", f"⚠️ این ایموجی‌ها توی این چت مجاز نیستن: {' '.join(missing)}"]

    return "\n".join(lines)


async def provision_many(core: Core, targets: list[Target], panel: tuple[int, int] | None, owner_id: int) -> None:
    """Provision several chats one after another and report them together (edit the panel if given)."""
    blocks: list[str] = []
    for target in targets:
        report = await provision(core, target)
        lines = [ln for ln in report.lines if ln.startswith(("🤖", "🏢"))]
        if report.fatal:
            lines.append(f"⛔️ {report.fatal}")
        blocks.append("\n".join([f"📢 <b>{esc(target.title)}</b>", *lines]))
    if not blocks:
        return
    text = "🛡 <b>ادمین‌کردن ربات‌ها</b>\n\n" + "\n\n".join(blocks)
    if len(text) > 3800:
        text = text[:3800] + "…"
    if panel is not None:
        from telegram import InlineKeyboardButton as Btn
        from telegram import InlineKeyboardMarkup as Markup

        try:
            await core.main.edit_message_text(
                text, chat_id=panel[0], message_id=panel[1],
                reply_markup=Markup([[Btn("📢 چت‌های من", callback_data="t:list")]]),
            )
            return
        except TelegramError as exc:
            log.debug("could not edit the panel with the reports: %s", exc)
    await core.notify(owner_id, text)
