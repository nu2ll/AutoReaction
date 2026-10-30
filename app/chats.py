"""Registering chats and getting the reaction bots into them (make them admins)."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from dataclasses import dataclass, field
from enum import Enum

from telegram import Bot, Chat
from telegram.constants import ChatMemberStatus as S
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.core import Core
from app.emoji import norm
from app.models import Mode, Target
from app.utils import seconds

log = logging.getLogger(__name__)

ACTIVE = frozenset({S.OWNER, S.ADMINISTRATOR, S.MEMBER})
ADMINS = frozenset({S.OWNER, S.ADMINISTRATOR})
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
    core.cache.drop(chat.id)
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
        return Outcome.FAIL, "ربات عضو چت نیست (توی گروه باید اول اضافه بشه)."
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


async def _ensure(core: Core, target: Target, bot: Bot, can_promote: bool) -> tuple[Outcome, str]:
    """Make sure `bot` is in the chat (promoting it if needed) and assign it to the target."""
    assert core.main is not None
    if await _status(core.main, target.chat_id, bot.id) in ACTIVE:
        await core.db.assign(target.id, bot.id)
        return Outcome.OK, ""
    if not can_promote:
        return Outcome.FAIL, "باید دستی ادمین بشه"

    outcome, why = await _promote(core.main, target.chat_id, bot.id)
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

    def render(self) -> str:
        out = [f"🛡 <b>{esc(self.title)}</b>", ""]
        out += self.lines
        if self.fatal:
            out += ["", f"⛔️ {self.fatal}"]
        return "\n".join(out)


def _tally(label: str, ok: int, total: int, failures: list[str]) -> list[str]:
    lines = [f"{label}: ✅ <b>{ok}</b> از <b>{total}</b>"]
    lines += [f"   ❌ {f}" for f in failures[:8]]
    if len(failures) > 8:
        lines.append(f"   … و {len(failures) - 8} مورد دیگه")
    return lines


async def provision(core: Core, target: Target) -> Report:
    """Put every bot this chat should use into it, as admin, and record the assignment."""
    assert core.main is not None
    main = core.main
    report = Report(f"ادمین‌کردن ربات‌ها — {target.title}")

    try:
        me = await _call(lambda: main.get_chat_member(target.chat_id, main.id))
    except TelegramError:
        report.fatal = "ربات اصلی توی این چت نیست."
        return report
    can_promote = me.status in ADMINS and bool(getattr(me, "can_promote_members", False))
    if not can_promote:
        report.lines.append(
            "ℹ️ ربات اصلی دسترسی «افزودن ادمین» نداره؛ فقط رباتی که خودت اضافه کرده باشی شناسایی می‌شه."
        )

    user = await core.db.get_user(target.owner_id)
    candidates = 0

    # ---- the user's own bots
    if target.uses_own:
        own = core.pool.of_user(target.owner_id)
        candidates += len(own)
        ok, failures = 0, []
        for bot in own:
            outcome, why = await _ensure(core, target, bot, can_promote)
            if outcome is Outcome.OK:
                ok += 1
            else:
                failures.append(f"@{bot.username}: {esc(why)}")
                if outcome is Outcome.FATAL:
                    report.fatal = why
                    break
            await asyncio.sleep(0.05)
        if own:
            report.lines += _tally("🤖 ربات‌های خودت", ok, len(own), failures)

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
                outcome, why = await _ensure(core, target, bot, can_promote)
                if outcome is Outcome.OK:
                    added += 1
                else:
                    failures.append(f"@{bot.username}: {esc(why)}")
                    if outcome is Outcome.FATAL:
                        report.fatal = why
                        break
                await asyncio.sleep(0.05)
        elif need > 0:
            failures.append("برای اضافه‌کردن ربات سرویس، دسترسی «افزودن ادمین» لازمه.")

        if want:
            report.lines += _tally("🏢 ربات‌های سرویس", len(assigned) + added, want, failures)
            if need - added > 0 and len(pool_ids) < want:
                report.lines.append(f"   ⚠️ فقط {len(pool_ids)} ربات توی سرویس موجوده.")

    if candidates == 0 and not report.fatal:
        report.lines.append(
            "رباتی برای اضافه‌کردن نیست. یا ربات خودت رو توی «🤖 ربات‌های من» اضافه کن، "
            "یا سرویس رو فعال کن."
        )
    return report


async def provision_and_notify(core: Core, target: Target) -> None:
    report = await provision(core, target)
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
        allowed = await core.cache.get(main, target.chat_id)
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
