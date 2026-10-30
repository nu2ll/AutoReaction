"""Screens: each function returns (text, keyboard)."""
from __future__ import annotations

import html
import time

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup

from app.access import MAX_SERVICE_PER_CHAT
from app.core import Core
from app.models import UNLIMITED, Mode, Source, Target, User

esc = html.escape

SOURCE_LABEL = {
    Source.OWN: "🤖 ربات‌های خودم",
    Source.SERVICE: "🏢 ربات‌های سرویس",
    Source.BOTH: "🤖+🏢 هر دو",
}
MODE_LABEL = {Mode.RANDOM: "🎲 رندوم", Mode.CUSTOM: "✍️ لیست من"}
PAGE_SIZE = 20

View = tuple[str, Markup]


def back(to: str = "m:home", label: str = "🔙 برگشت") -> list[Btn]:
    return [Btn(label, callback_data=to)]


def service_status(core: Core, user_id: int, user: User | None) -> str:
    if core.access.is_owner(user_id):
        return "♾ نامحدود (مدیر)"
    if user and user.has_service():
        text = "✅ فعال (بدون انقضا)" if user.service_until == UNLIMITED else f"✅ فعال — {user.days_left()} روز مانده"
        if user.service_limit:
            text += f" · حداکثر {user.service_limit} ربات در هر چت"
        return text
    if core.access.public:
        return "✅ برای همه فعال"
    hint = f"\nبرای فعال‌سازی: {esc(core.config.support)}" if core.config.support else ""
    return f"❌ غیرفعال{hint}"


# ------------------------------------------------------------------------ home
async def home_view(core: Core, user: User) -> View:
    targets = await core.db.count_targets(user.user_id)
    bots = await core.db.count_bots(user.user_id)
    text = (
        f"<b>👋 سلام {esc(user.full_name)}!</b>\n\n"
        "من به پست‌های کانال و گروهت خودکار ری‌اکشن می‌دم.\n\n"
        f"📢 چت‌های من: <b>{targets}</b>\n"
        f"🤖 ربات‌های من: <b>{bots}</b>\n"
        f"🏢 سرویس ما: {service_status(core, user.user_id, user)}"
    )
    rows = [
        [Btn("📢 چت‌های من", callback_data="t:list"), Btn("🤖 ربات‌های من", callback_data="b:list")],
        [Btn("ℹ️ راهنما", callback_data="m:help")],
    ]
    if core.access.is_owner(user.user_id):
        rows.append([Btn("🛠 پنل مدیریت", callback_data="a:home")])
    return text, Markup(rows)


HELP = """<b>📖 راهنما</b>

<b>۱) ثبت چت</b>
ربات رو توی کانال یا گروهت <b>ادمین</b> کن (دسترسی «افزودن ادمین» رو هم بده). چت خودکار ثبت می‌شه. یا از «چت‌های من» یوزرنیم/آیدی عددی رو بفرست.

<b>۲) ربات‌های ری‌اکشن</b>
🤖 <b>ربات خودت:</b> از @BotFather توکن بگیر و توی «ربات‌های من» بفرست.
🏢 <b>سرویس ما:</b> ربات‌های آماده‌ی ما (نیاز به فعال‌سازی).

<b>۳) نوع ری‌اکشن</b>
🎲 رندوم از بین ری‌اکشن‌های مجاز همون چت
✍️ لیست خودت: ایموجی‌ها رو بفرست، ربات‌ها فقط از بینشون انتخاب می‌کنن

<b>۴) ادمین‌کردن ربات‌ها</b>
دکمه‌ی «🛡 ادمین‌کردن ربات‌ها» توی صفحه‌ی چت، همه‌ی ربات‌ها رو خودکار ادمین می‌کنه.
توی کانال‌ها مستقیم کار می‌کنه؛ توی گروه‌ها ربات باید قبلاً عضو باشه.

<b>۵) تنظیمات هر چت</b>
احتمال ری‌اکشن، تعداد ربات‌های سرویس، خاموش/روشن، و بررسی دسترسی‌ها."""


def help_view() -> View:
    return HELP, Markup([back()])


# ---------------------------------------------------------------------- targets
async def targets_view(core: Core, user_id: int) -> View:
    targets = await core.db.list_targets(user_id)
    if targets:
        text = "<b>📢 چت‌های من</b>\n\nیکی رو انتخاب کن:"
    else:
        text = (
            "<b>📢 چت‌های من</b>\n\nهنوز چتی ثبت نکردی.\n"
            "ربات رو با دکمه‌های پایین به کانال/گروهت اضافه کن؛ خودکار ثبت می‌شه."
        )
    rows = [
        [Btn(f"{'🟢' if t.enabled else '⏸'} {t.title[:32]}", callback_data=f"t:open:{t.id}")]
        for t in targets
    ]
    me = core.main.username
    rows.append([
        Btn("➕ کانال", url=f"https://t.me/{me}?startchannel=true&admin=promote_members+manage_chat"),
        Btn("➕ گروه", url=f"https://t.me/{me}?startgroup=true&admin=promote_members+manage_chat"),
    ])
    rows.append([Btn("🔎 افزودن با یوزرنیم / آیدی", callback_data="t:add")])
    rows.append(back())
    return text, Markup(rows)


def add_chat_prompt() -> View:
    text = (
        "یوزرنیم یا آیدی عددی کانال/گروه رو بفرست.\n"
        "مثال: <code>@mychannel</code> یا <code>-1001234567890</code>\n\n"
        "ربات اصلی باید اونجا ادمین باشه و تو هم ادمین اون چت باشی."
    )
    return text, Markup([back("m:cancel:t", "✖️ انصراف")])


async def target_view(core: Core, target: Target) -> View:
    user = await core.db.get_user(target.owner_id)
    assigned = await core.db.assigned_bot_ids(target.id)
    own_n = sum(1 for b in assigned if core.pool.has_owner(b) and core.pool.owner_of(b) == target.owner_id)
    svc_n = sum(1 for b in assigned if core.pool.has_owner(b) and core.pool.owner_of(b) is None)
    allowed = core.access.service_allowed(target.owner_id, user)
    want = core.access.effective_service_count(target, user)

    mode = MODE_LABEL[target.mode]
    if target.mode is Mode.CUSTOM:
        mode += ": " + (" ".join(target.emojis) or "—")

    lines = [
        f"<b>📢 {esc(target.title)}</b>",
        f"<code>{target.chat_id}</code>",
        "",
        f"وضعیت: {'🟢 فعال' if target.enabled else '⏸ متوقف'}",
        f"ربات‌ها: {SOURCE_LABEL[target.source]}",
        f"ری‌اکشن: {mode}",
        f"احتمال: <b>{target.chance}%</b>",
    ]
    active = 0
    if target.uses_own:
        lines.append(f"🤖 ربات‌های خودت توی چت: <b>{own_n}</b>")
        active += own_n
    if target.uses_service:
        if allowed:
            lines.append(f"🏢 ربات‌های سرویس: <b>{min(svc_n, want)}</b> از <b>{want}</b>")
            active += min(svc_n, want)
        else:
            lines.append("🏢 سرویس: ❌ دسترسی نداری")
    lines.append(f"📨 ری‌اکشن‌های ارسال‌شده: <b>{target.reactions_sent:,}</b>")
    if active == 0:
        lines += ["", "⚠️ هنوز رباتی فعال نیست؛ «🛡 ادمین‌کردن ربات‌ها» رو بزن."]

    i = target.id
    rows = [
        [Btn("⏸ متوقف کن" if target.enabled else "▶️ فعال کن", callback_data=f"t:tog:{i}")],
        [Btn(f"ربات‌ها: {SOURCE_LABEL[target.source]}", callback_data=f"t:src:{i}")],
        [
            Btn(f"ری‌اکشن: {MODE_LABEL[target.mode]}", callback_data=f"t:mode:{i}"),
            Btn("✍️ ویرایش لیست", callback_data=f"t:emo:{i}"),
        ],
        [
            Btn("➖ 10%", callback_data=f"t:ch:{i}:-10"),
            Btn(f"🎯 {target.chance}%", callback_data="noop"),
            Btn("➕ 10%", callback_data=f"t:ch:{i}:10"),
        ],
    ]
    if target.uses_service and allowed:
        rows.append([
            Btn("-5", callback_data=f"t:sc:{i}:-5"),
            Btn("-1", callback_data=f"t:sc:{i}:-1"),
            Btn(f"🏢 {target.service_count}", callback_data="noop"),
            Btn("+1", callback_data=f"t:sc:{i}:1"),
            Btn("+5", callback_data=f"t:sc:{i}:5"),
        ])
    rows += [
        [Btn("🛡 ادمین‌کردن ربات‌ها", callback_data=f"t:prov:{i}"), Btn("🔍 بررسی", callback_data=f"t:chk:{i}")],
        [Btn("🗑 حذف چت", callback_data=f"t:del:{i}"), Btn("🔙 برگشت", callback_data="t:list")],
    ]
    return "\n".join(lines), Markup(rows)


def emoji_prompt(target: Target) -> View:
    current = " ".join(target.emojis) or "—"
    text = (
        f"ایموجی‌هایی که می‌خوای ربات‌ها استفاده کنن رو توی یک پیام بفرست.\n"
        f"مثال: 🔥 ❤️ 👍 🎉\n\nلیست فعلی: {current}\n\n"
        "فقط ایموجی‌هایی که تلگرام برای ری‌اکشن مجاز کرده قبول می‌شن (حداکثر ۲۰ تا)."
    )
    return text, Markup([back(f"t:open:{target.id}", "✖️ انصراف")])


def confirm_delete_target(target: Target) -> View:
    text = (
        f"❓ چت <b>{esc(target.title)}</b> حذف بشه؟\n"
        "ربات‌های سرویس از این چت برداشته می‌شن."
    )
    return text, Markup([[
        Btn("✅ بله، حذف کن", callback_data=f"t:delok:{target.id}"),
        Btn("✖️ نه", callback_data=f"t:open:{target.id}"),
    ]])


# -------------------------------------------------------------------- own bots
async def bots_view(core: Core, user_id: int) -> View:
    bots = await core.db.list_bots(user_id)
    limit = "" if core.access.is_owner(user_id) else f" / {core.config.max_bots_per_user}"
    lines = [f"<b>🤖 ربات‌های من</b> ({len(bots)}{limit})", ""]
    if bots:
        lines += [f"• @{esc(b.username)} {'✅' if b.status == 'active' else '⚠️ نامعتبر'}" for b in bots]
    else:
        lines.append("هنوز رباتی اضافه نکردی. با ربات خودت می‌تونی ری‌اکشن بدی.")
    rows = [[Btn(f"🗑 @{b.username}", callback_data=f"b:del:{b.bot_id}")] for b in bots]
    rows.append([Btn("➕ افزودن ربات", callback_data="b:add")])
    rows.append(back())
    return "\n".join(lines), Markup(rows)


def add_bot_prompt(cancel_to: str = "m:cancel:b") -> View:
    text = (
        "توکن ربات رو از @BotFather بگیر و اینجا بفرست (می‌تونی چندتا رو با فاصله یا خط جدید بفرستی).\n\n"
        "🔐 توکن رمزنگاری‌شده ذخیره می‌شه و پیامت همون لحظه پاک می‌شه."
    )
    return text, Markup([back(cancel_to, "✖️ انصراف")])


def confirm_delete_bot(bot_id: int, username: str, back_to: str = "b:list", ok_prefix: str = "b:delok") -> View:
    text = f"❓ ربات @{esc(username)} حذف بشه؟"
    return text, Markup([[
        Btn("✅ بله", callback_data=f"{ok_prefix}:{bot_id}"),
        Btn("✖️ نه", callback_data=back_to),
    ]])


# ------------------------------------------------------------------------ admin
async def admin_view(core: Core) -> View:
    s = await core.db.stats()
    text = (
        "<b>🛠 پنل مدیریت</b>\n\n"
        f"👥 کاربرها: <b>{s['users']}</b> (مسدود: {s['banned']})\n"
        f"📢 چت‌ها: <b>{s['targets']}</b> (فعال: {s['active_targets']})\n"
        f"🏢 ربات‌های سرویس: <b>{s['service_bots']}</b> (نامعتبر: {s['invalid_bots']})\n"
        f"🤖 ربات‌های کاربرها: <b>{s['user_bots']}</b>\n"
        f"📨 کل ری‌اکشن‌ها: <b>{s['reactions']:,}</b>\n"
        f"🌐 سرویس برای همه: {'✅ باز' if core.access.public else '🔒 بسته (فقط کاربران تاییدشده)'}"
    )
    rows = [
        [Btn("🏢 ربات‌های سرویس", callback_data="a:svc:0")],
        [Btn("🔒 ببند" if core.access.public else "🌐 باز کن برای همه", callback_data="a:public")],
        [Btn("👥 دستورات کاربران", callback_data="a:users"), Btn("🔄 بروزرسانی", callback_data="a:home")],
        back("m:home", "🔙 منوی اصلی"),
    ]
    return text, Markup(rows)


USERS_HELP = """<b>👥 مدیریت کاربران</b>

<code>/grant ID DAYS [LIMIT]</code>
فعال‌سازی سرویس. DAYS عدد روز یا <code>forever</code>. LIMIT = حداکثر ربات سرویس در هر چت (اختیاری).
مثال: <code>/grant 123456 30 10</code>

<code>/revoke ID</code> — قطع دسترسی به سرویس
<code>/ban ID</code> · <code>/unban ID</code> — مسدود/آزاد کردن
<code>/user ID</code> — اطلاعات کاربر
<code>/addbot TOKEN</code> — افزودن ربات سرویس
<code>/delbot @username</code> — حذف ربات سرویس"""


def users_help_view() -> View:
    return USERS_HELP, Markup([back("a:home")])


async def service_bots_view(core: Core, page: int) -> View:
    bots = await core.db.list_bots(None)
    pages = max(1, -(-len(bots) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = bots[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"<b>🏢 ربات‌های سرویس</b> ({len(bots)})", ""]
    lines += [
        f"• @{esc(b.username)} {'✅' if b.status == 'active' and core.pool.get(b.bot_id) else '⚠️'}"
        for b in chunk
    ] or ["هنوز رباتی اضافه نشده."]
    lines += ["", f"حداکثر ربات سرویس برای هر چت: {MAX_SERVICE_PER_CHAT}"]

    nav = []
    if page > 0:
        nav.append(Btn("◀️", callback_data=f"a:svc:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("▶️", callback_data=f"a:svc:{page + 1}"))

    rows = [nav, [Btn("➕ افزودن ربات", callback_data="a:svcadd"), Btn("🧹 حذف نامعتبرها", callback_data="a:purge")]]
    rows += [[Btn(f"🗑 @{b.username}", callback_data=f"a:svcdel:{b.bot_id}")] for b in chunk]
    rows.append(back("a:home"))
    return "\n".join(lines), Markup(rows)


def user_info(core: Core, user: User, targets: int, bots: int) -> str:
    name = f"@{user.username}" if user.username else esc(user.full_name or "—")
    return (
        f"<b>👤 {name}</b>\n<code>{user.user_id}</code>\n\n"
        f"وضعیت: {'⛔️ مسدود' if user.banned else '✅ عادی'}\n"
        f"سرویس: {service_status(core, user.user_id, user)}\n"
        f"📢 چت‌ها: {targets}\n🤖 ربات‌ها: {bots}\n"
        f"عضویت: {time.strftime('%Y-%m-%d', time.gmtime(user.created_at))}"
    )
