import asyncio, os, sys, tempfile, json, random
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent / "stubs")); sys.path.insert(0, str(Path(__file__).parent.parent))

from telegram import Bot, ReactionTypeEmoji, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest, InvalidToken
from app.config import Config
from app.core import Core, BotRegistrationError
from app.emoji import parse_emojis
from app.models import Source, Mode, UNLIMITED
from app import chats, views

def tok(i): return f"{i}:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

class Main(Bot):
    """Fake main bot with a fake chat membership table."""
    def __init__(self):
        super().__init__(tok(1)); self.members = {}; self.promoted = []; self.can_promote = True
        self.notes = []; self.unbanned = []; self.links = []; self.fail_ids = set()
    async def unban_chat_member(self, chat_id, uid, only_if_banned=False):
        self.unbanned.append(uid); self.members.pop((chat_id, uid), None)
    async def create_chat_invite_link(self, chat_id, **kw):
        if not self.can_promote: raise BadRequest("not enough rights")
        self.links.append(kw); return NS(invite_link="https://t.me/+abc123")
    async def get_chat_member(self, chat_id, uid):
        st = self.members.get((chat_id, uid))
        if st is None: raise BadRequest("user not found")
        return NS(status=st, can_promote_members=self.can_promote)
    async def promote_chat_member(self, chat_id, uid, **kw):
        if not self.can_promote: raise BadRequest("Not enough rights")
        if uid == 999 or uid in self.fail_ids: raise BadRequest("USER_NOT_PARTICIPANT")
        if kw.get("can_manage_chat"): self.members[(chat_id, uid)] = "administrator"; self.promoted.append(uid)
        else: self.members.pop((chat_id, uid), None)
    async def get_chat(self, ref):
        return NS(id=ref, type="channel", title="Chan", username=None, available_reactions=None)
    async def send_message(self, uid, text, reply_markup=None): self.notes.append((uid, text))

def mk_config(d):
    return Config(token=tok(1), owners=frozenset({10}), data_dir=Path(d), secret_key=None,
        max_concurrency=5,
        max_targets_per_user=2, max_bots_per_user=2, service_public=False, support="@sup", log_level="INFO")

async def main():
    # emoji parser
    assert parse_emojis("🔥 ❤️ 👨‍💻 ❤‍🔥 x 🔥 😀") == ["🔥", "❤", "👨‍💻", "❤‍🔥"], parse_emojis("🔥 ❤️ 👨‍💻 ❤‍🔥")
    print("emoji ok")

    with tempfile.TemporaryDirectory() as d:
        core = Core.build(mk_config(d)); main_bot = Main(); await core.start(main_bot)
        await core.db.ensure_user(10)
        for n in (201, 202): await core.register_bot(tok(n), None)
        t = await core.db.create_target(-1001, "Old", "", 10, source=Source.SERVICE, service_count=2, chance=80, enabled=True)
        for n in (201, 202): await core.db.assign(t.id, n)
        assert len(await core.db.assigned_bot_ids(t.id)) == 2 and len(core.pool.service()) == 2
        # token stored encrypted
        rec = await core.db.get_bot(201); assert tok(201) not in rec.token and core.vault.decrypt(rec.token) == tok(201)
        print("crypto ok")

        # user registers own bots (limit 2), bad token, duplicate owner
        u = await core.db.touch_user(50, "bob", "Bob")
        await core.register_bot(tok(301), 50); await core.register_bot(tok(302), 50)
        for bad, msg in ((tok(303), "سقف"), ("5:BAD", "نامعتبر"), (tok(201), "قبلاً")):
            try: await core.register_bot(bad if bad.endswith("BAD") else bad, 50); assert False
            except BotRegistrationError as e: assert msg in str(e), (bad, str(e))
        # re-register same (token rotation) allowed
        await core.register_bot(tok(301), 50)
        assert len(core.pool.of_user(50)) == 2
        print("register ok")

        # register chat: permissions
        main_bot.members[(-500, 1)] = "administrator"; main_bot.members[(-500, 50)] = "administrator"
        main_bot.members[(-600, 1)] = "administrator"; main_bot.members[(-600, 51)] = "member"
        chat = await chats.resolve_chat(core, 50, -500)
        target = await chats.register_chat(core, 50, chat)
        try: await chats.resolve_chat(core, 51, -600); assert False
        except chats.ChatError: pass
        try: await chats.register_chat(core, 77, chat); assert False
        except chats.ChatError as e: assert "قبلاً" in str(e)
        assert chats.parse_chat_ref("https://t.me/abcd_ef") == "@abcd_ef" and chats.parse_chat_ref("-1001234567") == -1001234567
        print("chat registration ok")

        # provision: user has no service -> only own bots
        rep = await chats.provision(core, target); text = rep.render()
        assert sorted(main_bot.promoted) == [301, 302], main_bot.promoted
        assert len(await core.db.assigned_bot_ids(target.id)) == 2
        # grant service with limit 1 -> only 1 service bot assigned
        await core.db.set_service(50, UNLIMITED, 1)
        target = await core.db.get_target(target.id)
        rep = await chats.provision(core, target)
        assert len(await core.db.assigned_bot_ids(target.id)) == 3, rep.render()
        print(rep.render().replace("\n", " | "))
        # lowering to 0 releases
        await core.db.update_target(target.id, service_count=0)
        target = await core.db.get_target(target.id)
        await chats.provision(core, target); await asyncio.sleep(0.2)
        assert len(await core.db.assigned_bot_ids(target.id)) == 2
        # main without promote right
        main_bot.can_promote = False
        rep = await chats.provision(core, target); assert "دسترسی" in rep.render()
        main_bot.can_promote = True
        # check_target
        out = await chats.check_target(core, target); assert "301" in out or "bot301" in out
        print("provision/check ok")

        # helper account: main bot has no rights, the helper adds the bots and leaves
        class FakeHelper:
            label = "@helper"; mention = "@helper"; leave_after = True
            def __init__(self, state): self.state = state; self.left = []; self.calls = []
            async def status(self, chat_id): return self.state
            async def promote(self, chat_id, username, bot_id):
                self.calls.append(bot_id)
                if bot_id == 302: return "fail", "USER_BOT"
                main_bot.members[(chat_id, bot_id)] = "administrator"; return "ok", ""
            async def leave(self, chat_id): self.left.append(chat_id); return True
        for bid in (301, 302): main_bot.members.pop((-500, bid), None)
        await core.db.unassign(target.id, 301); await core.db.unassign(target.id, 302)
        main_bot.can_promote = False
        fh = FakeHelper("ready"); core.helper = fh
        rep = await chats.provision(core, target); text = rep.render()
        assert sorted(fh.calls) == [301, 302] and fh.left == [-500], (fh.calls, fh.left)
        assert main_bot.members[(-500, 301)] == "administrator" and (-500, 302) not in main_bot.members
        assert "خارج شد" in text and "❌" in text, text
        # helper not in the chat yet -> hint to add it, nothing promoted by it
        fh = FakeHelper("absent"); core.helper = fh
        rep = await chats.provision(core, target); text = rep.render()
        assert not fh.calls and not fh.left and "@helper" in text, text
        assert "💡 اکانت @helper رو توی این چت ادمین کن\n(با دسترسی «افزودن ادمین») و دوباره 🛡 رو بزن.\nخودش ربات‌ها رو اضافه و ادمین می‌کنه\nو بعدش خارج می‌شه." in text, text
        # Telegram "fresh admin" limit: stop at the first error and stay in the chat
        class FreshHelper(FakeHelper):
            async def promote(self, chat_id, username, bot_id):
                self.calls.append(bot_id); return "fatal", "⏳ fresh"
        fh = FreshHelper("ready"); core.helper = fh
        rep = await chats.provision(core, target); text = rep.render()
        assert len(fh.calls) == 1 and not fh.left and "⏳" in text and "خارج شد" not in text, (fh.calls, fh.left, text)
        # the main bot brings the helper in by itself: unban -> join by invite link -> make it admin
        class AutoHelper(FakeHelper):
            user_id = 777
            def __init__(s): super().__init__("absent"); s.joined = []
            async def status(s, chat_id):
                return "ready" if main_bot.members.get((chat_id, 777)) == "administrator" and s.joined else "absent"
            async def join(s, link):
                if main_bot.members.get((-500, 777)) == "kicked": return False, "banned"
                s.joined.append(link); main_bot.members[(-500, 777)] = "member"; return True, ""
            async def promote(s, chat_id, username, bot_id):
                s.calls.append(bot_id); main_bot.members[(chat_id, bot_id)] = "administrator"; return "ok", ""
        for bid in (301, 302): main_bot.members.pop((-500, bid), None)
        await core.db.unassign(target.id, 301); await core.db.unassign(target.id, 302)
        main_bot.can_promote = True; main_bot.fail_ids = {301, 302}
        main_bot.members[(-500, 777)] = "kicked"            # the helper is banned from the chat
        ah_ = AutoHelper(); core.helper = ah_
        rep = await chats.provision(core, target); text = rep.render()
        assert main_bot.unbanned == [777] and ah_.joined == ["https://t.me/+abc123"], (main_bot.unbanned, ah_.joined)
        assert main_bot.links and main_bot.links[0].get("member_limit") == 1                # one-use link
        assert 777 in main_bot.promoted and sorted(ah_.calls) == [301, 302], (main_bot.promoted, ah_.calls)
        assert ah_.left == [-500] and "خارج شد" in text and "💡" not in text, text
        # 🛡 (always_helper): the helper joins and becomes admin even when nothing needs it
        for bid in (301, 302): main_bot.members[(-500, bid)] = "administrator"
        main_bot.members.pop((-500, 777), None); main_bot.members[(-500, 777)] = "kicked"
        main_bot.unbanned.clear(); main_bot.fail_ids = set()
        ah_ = AutoHelper(); core.helper = ah_
        rep_ = await chats.provision(core, target, always_helper=True)
        assert ah_.joined and main_bot.unbanned == [777] and ah_.left == [-500], (ah_.joined, main_bot.unbanned, ah_.left)
        ah_ = AutoHelper(); core.helper = ah_; main_bot.members.pop((-500, 777), None)
        await chats.provision(core, target)             # automatic runs only bring it when needed
        assert not ah_.joined
        main_bot.fail_ids = {301, 302}
        # the main bot may not add admins -> no automatic way in: say why + the manual hint
        for bid in (301, 302, 777): main_bot.members.pop((-500, bid), None)
        await core.db.unassign(target.id, 301); await core.db.unassign(target.id, 302)
        main_bot.can_promote = False
        ah_ = AutoHelper(); core.helper = ah_
        text = (await chats.provision(core, target)).render()
        assert not ah_.joined and "⚠️ ربات اصلی دسترسی" in text and "💡 اکانت" in text, text
        main_bot.fail_ids = set()
        # admin limit: look at how many admins the chat has and add only as many bots as fit
        async def _ga0(chat_id): return [NS(user=NS(is_bot=False)) for _ in range(4)]
        main_bot.get_chat_administrators = _ga0
        tv = (await views.target_view(core, await core.db.get_target(target.id)))[0]
        assert "👮 ادمین‌ها:" in tv and "از <b>50</b>" in tv, tv
        del main_bot.get_chat_administrators
        def humans(n): return [NS(user=NS(is_bot=False)) for _ in range(n)] + [NS(user=NS(is_bot=True))]
        async def _ga(n, chat_id): return humans(n)
        core.helper = None; main_bot.can_promote = True
        for bid in (301, 302): main_bot.members.pop((-500, bid), None)
        await core.db.unassign(target.id, 301); await core.db.unassign(target.id, 302)
        main_bot.get_chat_administrators = lambda chat_id: _ga(49, chat_id)      # 49 humans + main bot = 50
        text = (await chats.provision(core, await core.db.get_target(target.id))).render()
        assert (-500, 301) not in main_bot.members and (-500, 302) not in main_bot.members and "50" in text, text
        main_bot.get_chat_administrators = lambda chat_id: _ga(47, chat_id)      # room for 2
        before_n = len(main_bot.promoted)
        text = (await chats.provision(core, await core.db.get_target(target.id))).render()
        assert len(main_bot.promoted) - before_n == 2, (len(main_bot.promoted) - before_n, text)
        del main_bot.get_chat_administrators                                     # unknown count: old behaviour
        for bid in (301, 302): main_bot.members.pop((-500, bid), None)
        await core.db.unassign(target.id, 301); await core.db.unassign(target.id, 302)
        core.helper = None; main_bot.can_promote = True
        main_bot.members[(-500, 302)] = "administrator"
        await chats.provision(core, target)
        print("helper ok")

        # reactor selection + dispatch
        await core.db.update_target(target.id, service_count=1, source=Source.BOTH, mode=Mode.CUSTOM, emojis=["🔥", "👍"])
        await chats.provision(core, await core.db.get_target(target.id))
        await core.reactor.on_message(main_bot, -500, 1, None)
        await asyncio.sleep(0.2)
        bots = [core.pool.get(i) for i in (301, 302)] + core.pool.service()
        got = [(b.id, r[2].emoji) for b in bots for r in b.reactions]
        assert len(got) == 3 and all(e in ("🔥", "👍") for _, e in got), got
        t2 = await core.db.get_target(target.id); assert t2.reactions_sent == 3
        # album guard
        await core.reactor.on_message(main_bot, -500, 2, "g1"); await core.reactor.on_message(main_bot, -500, 3, "g1")
        await asyncio.sleep(0.2); assert sum(len(b.reactions) for b in bots) == 6
        # service revoked -> only own
        await core.db.set_service(50, 0, 0)
        before = sum(len(b.reactions) for b in bots)
        await core.reactor.on_message(main_bot, -500, 4, None); await asyncio.sleep(0.2)
        assert sum(len(b.reactions) for b in bots) == before + 2
        # banned owner -> nothing
        await core.db.set_banned(50, True); before += 2
        await core.reactor.on_message(main_bot, -500, 5, None); await asyncio.sleep(0.2)
        assert sum(len(b.reactions) for b in bots) == before
        # dead token
        dead = core.pool.get(301)
        async def boom(*a, **k): raise InvalidToken("x")
        dead.set_message_reaction = boom
        await core.db.set_banned(50, False)
        await core.reactor.on_message(main_bot, -500, 6, None); await asyncio.sleep(0.3)
        assert core.pool.get(301) is None and (await core.db.get_bot(301)).status == "invalid"
        assert any(uid == 50 and "301" in t or "bot301" in t for uid, t in main_bot.notes)
        print("reactor ok")

        # views render
        for v in (await views.home_view(core, u), await views.targets_view(core, 50), await views.target_view(core, t2),
                  await views.bots_view(core, 50), await views.admin_view(core), await views.service_bots_view(core, 0),
                  views.help_view(), views.emoji_prompt(t2), views.add_chat_prompt()):
            assert isinstance(v[0], str) and isinstance(v[1], InlineKeyboardMarkup)
            for row in v[1].rows:
                for b in row:
                    assert b.callback_data is None or len(b.callback_data.encode()) <= 64
        print("views ok")

        # handler flows with fake updates
        from app.handlers import targets as th, inputs as ih, bots as bh, admin as ah
        class Q:
            def __init__(s, data, uid):
                s.data=data; s.from_user=NS(id=uid); s.sent=[]; s.edits=[]; s.message = NS(reply_text=s._reply)
            async def answer(s, text=None, show_alert=False): s.last = text
            async def edit_message_text(s, text, reply_markup=None): s.edits.append(text)
            async def _reply(s, text, **k): s.sent.append(text)
        def upd(q, uid): return NS(callback_query=q, effective_user=NS(id=uid, username="u", full_name="U"), effective_message=None)
        ctx = NS(application=NS(bot_data={"core": core}), user_data={})
        async def press(data, uid=50):
            q = Q(data, uid); await th.on_target(upd(q, uid), ctx) if data.startswith("t:") else (await bh.on_bots(upd(q, uid), ctx) if data.startswith("b:") else await ah.on_admin(upd(q, uid), ctx)); return q
        tid = target.id
        await core.db.set_service(50, UNLIMITED, 0)
        for data in (f"t:open:{tid}", f"t:tog:{tid}", f"t:tog:{tid}", f"t:src:{tid}", f"t:mode:{tid}", f"t:mode:{tid}", f"t:ch:{tid}:-10", f"t:sc:{tid}:5",
                     f"t:emo:{tid}", f"t:chk:{tid}", f"t:prov:{tid}", f"t:del:{tid}", "t:list", "t:add", "b:list", "b:add"):
            q = await press(data); assert q.edits or q.sent, data
        assert (await core.db.get_target(tid)).chance == 90
        q = await press(f"t:open:{tid}", uid=51); assert "پیدا نشد" in q.last   # not yours
        q = await press("a:home", uid=50); assert "دسترسی" in q.last
        for data in ("a:home", "a:users", "a:svc:0", "a:svcadd", "a:public", "a:purge", "a:svcdel:201", "a:svcok:201"):
            q = await press(data, uid=10); assert q.edits or q.sent, data
        assert core.pool.get(201) is None
        # text inputs
        class Msg:
            def __init__(s, text): s.text=text; s.chat=NS(send_message=s._send); s.out=[]
            async def delete(s): pass
            async def reply_text(s, t, **k): s.out.append(t)
            async def _send(s, t, **k): s.out.append(t)
        def tupd(m, uid): return NS(effective_message=m, callback_query=None, effective_user=NS(id=uid, username="u", full_name="U"))
        ctx.user_data["input"] = {"kind": "emojis", "target": tid}
        m = Msg("🔥 ❤️ hello"); await ih.on_text(tupd(m, 50), ctx)
        assert (await core.db.get_target(tid)).emojis == ["🔥", "❤"] and "input" not in ctx.user_data
        ctx.user_data["input"] = {"kind": "emojis", "target": tid}
        m = Msg("hello"); await ih.on_text(tupd(m, 50), ctx); assert "input" in ctx.user_data and "❌" in m.out[0]
        ctx.user_data["input"] = {"kind": "own_bot"}
        m = Msg(f"{tok(39999)}"); await ih.on_text(tupd(m, 50), ctx)   # limit reached (2)
        assert any("سقف" in o for o in m.out), m.out
        ctx.user_data["input"] = {"kind": "svc_bot"}
        m = Msg(f"{tok(40001)} {tok(40002)}"); await ih.on_text(tupd(m, 10), ctx)
        assert len(core.pool.service()) == 3, m.out
        ctx.user_data["input"] = {"kind": "chat_ref"}
        m = Msg("not a ref!!"); await ih.on_text(tupd(m, 50), ctx); assert "فرمت" in m.out[0]
        # admin commands
        def upd_c(uid, args):
            m0 = Msg(""); return NS(message=m0, effective_message=m0, effective_chat=NS(send_message=lambda *a, **k: asyncio.sleep(0)), effective_user=NS(id=uid)), NS(application=ctx.application, args=args, user_data={})
        u_, c_ = upd_c(10, ["77", "30", "5"]); await ah.cmd_grant(u_, c_); usr = await core.db.get_user(77)
        assert usr.has_service() and usr.service_limit == 5 and usr.days_left() in (30, 31), usr
        u_, c_ = upd_c(10, ["77"]); await ah.cmd_ban(u_, c_); assert (await core.db.get_user(77)).banned
        u_, c_ = upd_c(10, ["10"]); await ah.cmd_ban(u_, c_); assert not (await core.db.get_user(10) or NS(banned=False)).banned
        u_, c_ = upd_c(10, ["77"]); await ah.cmd_revoke(u_, c_); assert not (await core.db.get_user(77)).has_service()
        print("handlers ok")

        # helper account from the admin panel
        import app.core as core_mod
        class FakeH:
            started = []
            def __init__(s, api_id, api_hash, session, leave_after=True):
                s.api_id, s.api_hash, s.session, s.leave_after = api_id, api_hash, session, leave_after
                s.user_id = 777; s.username = "helperacc"; s.name = "H"; s.out = False
            label = property(lambda s: f"@{s.username}")
            async def refresh(s):
                if s.session == "REVOKED": raise RuntimeError("revoked")
                s.username = "renamed"
            async def start(s):
                if s.session == "BAD": raise RuntimeError("not logged in")
                FakeH.started.append(s.session)
            async def close(s): pass
            async def logout(s): s.out = True
        class FakeLogin:
            def __init__(s, api_id, api_hash): s.api_id, s.api_hash = api_id, api_hash; s.closed = False
            async def send_code(s, phone):
                assert phone == "+989120000000", phone
            async def sign_in_code(s, code):
                from app.helper import LoginError
                if code == "11111": raise LoginError("❌ کد اشتباهه.", retry=True)
                return code == "22222"          # 22222 -> needs 2FA
            async def sign_in_password(s, pw):
                from app.helper import LoginError
                if pw != "pw": raise LoginError("❌ رمز اشتباهه.", retry=True)
            def session_string(s): return "SESSION-STR"
            async def close(s): s.closed = True
        core_mod.Helper = FakeH; ih.login_factory = FakeLogin
        q = await press("a:helper", uid=10); assert "تنظیم نشده" in q.edits[-1]
        q = await press("a:helper", uid=50); assert "دسترسی" in q.last            # not an owner
        import dataclasses
        q = await press("a:helperadd", uid=10); assert ctx.user_data.get("input", {}).get("kind") != "helper_phone"
        core.config = dataclasses.replace(core.config, helper_api_id=12345, helper_api_hash="a" * 32)
        q = await press("a:helperadd", uid=10); assert ctx.user_data["input"]["kind"] == "helper_phone"
        m = Msg("+98 912 000 0000"); await ih.on_text(tupd(m, 10), ctx); assert ctx.user_data["input"]["kind"] == "helper_code"
        m = Msg("1-1-1-1-1"); await ih.on_text(tupd(m, 10), ctx); assert "اشتباه" in m.out[0] and ctx.user_data["input"]["kind"] == "helper_code"
        m = Msg("2 2 2 2 2"); await ih.on_text(tupd(m, 10), ctx); assert ctx.user_data["input"]["kind"] == "helper_pass"
        m = Msg("wrong"); await ih.on_text(tupd(m, 10), ctx); assert "اشتباه" in m.out[0] and ctx.user_data["input"]["kind"] == "helper_pass"
        m = Msg("pw"); await ih.on_text(tupd(m, 10), ctx)
        assert core.helper and core.helper.session == "SESSION-STR" and core.helper.api_id == 12345 and "input" not in ctx.user_data, m.out
        assert core.helper_login is None
        raw = await core.db.kv_get("helper_session"); assert raw != "SESSION-STR" and core.vault.decrypt(raw) == "SESSION-STR"
        assert "✅" in (await views.admin_view(core))[1].rows[0][1].text
        # api_id/api_hash already configured -> the flow starts at the phone number
        core.config = dataclasses.replace(core.config, helper_api_id=999, helper_api_hash="b" * 32)
        q = await press("a:helperadd", uid=10); st = ctx.user_data["input"]
        assert st["kind"] == "helper_phone" and st["api_id"] == 999 and st["api_hash"] == "b" * 32, st
        m = Msg("+989120000000"); await ih.on_text(tupd(m, 10), ctx); assert ctx.user_data["input"]["kind"] == "helper_code"
        m = Msg("3-3-3-3-3"); await ih.on_text(tupd(m, 10), ctx)
        assert core.helper.api_id == 999 and "input" not in ctx.user_data
        core.config = dataclasses.replace(core.config, helper_api_id=0, helper_api_hash="")
        # non-owners cannot drive the flow
        ctx.user_data["input"] = {"kind": "helper_phone"}; m = Msg("1"); await ih.on_text(tupd(m, 50), ctx); assert "input" not in ctx.user_data
        # restart loads it from the database (encrypted), delete removes it for good
        await core.close()
        core = Core.build(mk_config(d)); await core.start(Main()); ctx.application.bot_data["core"] = core
        assert core.helper and core.helper.api_id == 999
        h = core.helper
        q = await press("a:helperdel", uid=10); assert "حذف" in q.edits[-1]
        q = await press("a:helperok", uid=10); assert core.helper is None and h.out and "تنظیم نشده" in q.edits[-1]
        assert await core.db.kv_get("helper_session") is None
        await core.close()
        core = Core.build(mk_config(d)); await core.start(Main()); ctx.application.bot_data["core"] = core; assert core.helper is None
        core_mod.Helper = __import__("app.helper", fromlist=["Helper"]).Helper
        # refresh button re-reads name/username
        core_mod.Helper = FakeH
        await core.set_helper(1, "c" * 32, "SES")
        q = await press("a:helper", uid=10); assert "helperacc" in q.edits[-1]
        btns = [b.callback_data for row in views.helper_view(core)[1].rows for b in row]
        assert "a:helperref" in btns, btns
        q = await press("a:helperref", uid=10); assert "بروزرسانی" in q.last and "renamed" in q.edits[-1], (q.last, q.edits)
        # the leave-after-work switch is a button, remembered in the database
        assert "a:helperleave" in [b.callback_data for row in views.helper_view(core)[1].rows for b in row]
        was = core.helper.leave_after
        q = await press("a:helperleave", uid=10); assert core.helper.leave_after is (not was) and q.edits
        assert await core._helper_leave() is (not was)
        q = await press("a:helperleave", uid=10); assert core.helper.leave_after is was
        core.helper.session = "REVOKED"
        q = await press("a:helperref", uid=10); assert "❌" in q.last, q.last
        q = await press("a:helperref", uid=50); assert "دسترسی" in q.last
        await core.remove_helper()
        q = await press("a:helperref", uid=10); assert "تنظیم نشده" in q.edits[-1]
        print("helper panel ok")

        # mention: @username, or a clickable name + numeric id when there is no username
        from app.helper import Helper as RealHelper
        hh = RealHelper(1, "x", "s"); hh.user_id, hh.username, hh.name = 5, None, "Julian <b>"
        assert hh.mention == '<a href="tg://user?id=5">Julian &lt;b&gt;</a> (<code>5</code>)', hh.mention
        hh.username = "julian"; assert hh.mention == "@julian"

        # /user: usage only without an id, "not found" only for an unknown id
        mm = Msg(""); u_, c_ = upd_c(10, []); u_.message = u_.effective_message = mm; await ah.cmd_user(u_, c_)
        assert mm.out == ["استفاده: <code>/user ID</code>"], mm.out
        mm = Msg(""); u_, c_ = upd_c(10, ["abc"]); u_.message = u_.effective_message = mm; await ah.cmd_user(u_, c_)
        assert mm.out == ["استفاده: <code>/user ID</code>"], mm.out
        mm = Msg(""); u_, c_ = upd_c(10, ["424242"]); u_.message = u_.effective_message = mm; await ah.cmd_user(u_, c_)
        assert mm.out == ["❌ کاربر پیدا نشد."], mm.out

        # lists longer than 10 are split into pages (service bots and the user's own bots)
        before_ids = {b.id for b in core.pool.service()}; before = len(before_ids)
        ctx.user_data["input"] = {"kind": "svc_bot"}
        m = Msg(" ".join(tok(41000 + i) for i in range(12))); await ih.on_text(tupd(m, 10), ctx)
        text, kb = await views.service_bots_view(core, 0)
        assert len(core.pool.service()) == before + 12
        navs = [b.text for row in kb.rows for b in row if b.callback_data == "noop"]
        dels = [b.callback_data for row in kb.rows for b in row if b.callback_data.startswith("a:svcdel:")]
        assert navs == ["1/2"] and len(dels) == 10 and text.count("• @") == 10, (navs, len(dels))
        assert all(d.endswith(":0") for d in dels)
        text, kb = await views.service_bots_view(core, 1)
        dels = [b.callback_data for row in kb.rows for b in row if b.callback_data.startswith("a:svcdel:")]
        assert [b.text for row in kb.rows for b in row if b.callback_data == "noop"] == ["2/2"] and len(dels) == before + 12 - 10
        assert dels[0].endswith(":1")
        q = await press("a:svc:1", uid=10); assert q.edits, q.edits
        q = await press(dels[0], uid=10); assert q.edits   # confirm keeps the page
        assert dels[0].rsplit(":", 1)[1] == "1"
        for b in [b for b in core.pool.service() if b.id not in before_ids]:   # leave the pool as it was
            await core.remove_bot(b.id)
        assert len(core.pool.service()) == before
        # one page only -> no pager at all
        text, kb = await views.bots_view(core, 50)
        assert not any(b.callback_data == "noop" for row in kb.rows for b in row)

        # self-deleting notices + retry on timeout
        from app.handlers.common import flash
        gone = []
        class Sent:
            async def delete(s): gone.append(1)
        async def send(t): return Sent()
        await flash(core, send, "x", seconds=0.05); assert not gone
        await asyncio.sleep(0.2); assert gone == [1]
        n = {"c": 0}
        from telegram.error import TimedOut
        orig = core_mod.Core.build  # keep reference to silence linters
        class Flaky(Bot):
            async def initialize(s):
                n["c"] += 1
                if n["c"] < 3: raise TimedOut("Timed out")
        import app.pool as pool_mod
        old_bot = pool_mod.Bot; pool_mod.Bot = Flaky
        try:
            b = await core.pool.connect(tok(7777)); assert n["c"] == 3
        finally: pool_mod.Bot = old_bot
        # results and errors edit the panel message itself; no new message is sent
        edited = []
        class Bt:
            async def edit_message_text(s, text, chat_id=None, message_id=None, reply_markup=None):
                edited.append((chat_id, message_id, text))
        class PMsg(Msg):
            chat_id = 42
            def get_bot(s): return Bt()
        class PQ(Q):
            def __init__(s, data, uid): super().__init__(data, uid); s.message = NS(reply_text=s._reply, message_id=77)
        q = PQ(f"t:emo:{tid}", 50); await th.on_target(upd(q, 50), ctx)
        assert ctx.user_data["input"]["panel"] == 77 and ctx.user_data["input"]["kind"] == "emojis", ctx.user_data
        m = PMsg("🔥 👍"); await ih.on_text(tupd(m, 50), ctx)
        assert not m.out and len(edited) == 1 and edited[0][:2] == (42, 77) and "✅ ایموجی‌ها اضافه شد" in edited[0][2], (edited, m.out)
        assert "input" not in ctx.user_data
        # wrong answer: the panel turns into the error, input stays active
        edited.clear(); ctx.user_data["input"] = {"kind": "emojis", "target": tid, "panel": 78}
        m = PMsg("nope"); await ih.on_text(tupd(m, 50), ctx)
        assert not m.out and edited[0][:2] == (42, 78) and "❌" in edited[0][2] and ctx.user_data["input"]["panel"] == 78
        ctx.user_data.pop("input", None)
        # report buttons edit the panel too (prov / chk): ⏳ first, then the report + back button
        q = PQ(f"t:prov:{tid}", 50); await th.on_target(upd(q, 50), ctx)
        assert not q.sent and len(q.edits) == 2 and "⏳" in q.edits[0] and "🛡" in q.edits[1], (q.sent, q.edits)
        q = PQ(f"t:chk:{tid}", 50); await th.on_target(upd(q, 50), ctx)
        assert not q.sent and len(q.edits) == 2 and "⏳" in q.edits[0], (q.sent, q.edits)
        # typed commands: the command is removed, a fresh panel is sent and the old panel is removed
        edited.clear(); mm = PMsg("/grant"); mm.deleted = False
        async def _d(): mm.deleted = True
        mm.delete = _d
        gone_ids = []
        class Bt2(Bt):
            async def delete_message(s, chat_id=None, message_id=None): gone_ids.append((chat_id, message_id))
        mm.get_bot = lambda: Bt2()
        mm.message_id = 56
        u_, c_ = upd_c(10, ["424242", "x"]); u_.message = u_.effective_message = mm; c_.user_data["panel"] = 55
        await ah.cmd_grant(u_, c_)
        assert mm.deleted and mm.out and "❌" in mm.out[0] and gone_ids == [(42, 55)] and not edited, (edited, mm.out, gone_ids)
        # own bot added: chats are provisioned together and reported on the same panel
        edited.clear(); ctx.user_data["input"] = {"kind": "own_bot", "panel": 60}
        core.main.edits = []
        async def _edit(text, chat_id=None, message_id=None, reply_markup=None): core.main.edits.append((chat_id, message_id, text))
        core.main.edit_message_text = _edit
        m = PMsg(tok(39998)); await ih.on_text(tupd(m, 50), ctx)
        await asyncio.sleep(0.3)
        assert not m.out and edited and edited[0][:2] == (42, 60), (edited, m.out)
        assert not core.main.edits or core.main.edits[0][:2] == (42, 60), core.main.edits
        core.main.edits.clear(); await chats.provision_many(core, [await core.db.get_target(tid)], (42, 61), 50)
        assert len(core.main.edits) == 1 and core.main.edits[0][:2] == (42, 61) and "📢" in core.main.edits[0][2], core.main.edits
        # a message nobody asked for is removed
        class DMsg(Msg):
            async def delete(s): s.gone = True
        ctx.user_data.pop("input", None); m = DMsg("random"); m.gone = False
        await ih.on_text(tupd(m, 50), ctx); assert m.gone and not m.out
        m = DMsg(""); m.gone = False; await ih.on_other(tupd(m, 50), ctx); assert m.gone
        # group commands addressed to this bot are removed silently; everything else is untouched
        from app.handlers import events as ev
        class GMsg(Msg):
            message_id = 5; media_group_id = None
            async def delete(s):
                if s.fail: raise RuntimeError("no rights")
                s.gone = True
        def gm(text, fail=False):
            m = GMsg(text); m.text, m.gone, m.fail = text, False, fail; return m
        gctx = NS(bot=NS(username="MainBot"), application=ctx.application)
        gchat = NS(id=-900, type="supergroup")
        for text, gone in (("/start@MainBot", True), ("/grant@mainbot 1 2", True), ("/start", False), ("/start@OtherBot", False), ("hello", False)):
            m = gm(text); await ev.on_post(NS(effective_message=m, effective_chat=gchat), gctx)
            assert m.gone == gone, (text, m.gone)
        m = gm("/start@MainBot", fail=True); await ev.on_post(NS(effective_message=m, effective_chat=gchat), gctx)  # no rights: silent
        assert not m.gone and not m.out
        # helper login steps also reuse one panel message
        from app.helper import LoginError
        class BadPhone:
            def __init__(s, *a): pass
            async def send_code(s, phone): raise LoginError("❌ شماره نامعتبره. با کد کشور بفرست، مثل +98...", retry=True)
            async def close(s): pass
        class OkPhone(BadPhone):
            async def send_code(s, phone): pass
        old_factory = ih.login_factory
        edited.clear(); ctx.user_data["input"] = {"kind": "helper_phone", "api_id": 1, "api_hash": "a" * 32, "panel": 90}
        ih.login_factory = BadPhone; m = PMsg("123"); await ih.on_text(tupd(m, 10), ctx)
        assert not m.out and edited == [(42, 90, "❌ شماره نامعتبره. با کد کشور بفرست، مثل +98...")] and ctx.user_data["input"]["kind"] == "helper_phone"
        edited.clear(); ih.login_factory = OkPhone; m = PMsg("+989120000000"); await ih.on_text(tupd(m, 10), ctx)
        assert not m.out and edited[0][:2] == (42, 90) and ctx.user_data["input"] == {"kind": "helper_code", "panel": 90}, (edited, ctx.user_data)
        ih.login_factory = old_factory; ctx.user_data.pop("input", None); await ih._close_login(core)
        print("notices ok")

        # restart: pool reloads from DB with decryption
        await core.close()
        core2 = Core.build(mk_config(d)); await core2.start(Main())
        assert len(core2.pool.service()) == 3 and len(core2.pool.of_user(50)) == 1, (len(core2.pool.service()), len(core2.pool.of_user(50)))
        await core2.close()
        print("restart ok")
asyncio.run(main()); print("ALL OK")
