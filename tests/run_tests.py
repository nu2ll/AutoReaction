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
        self.notes = []
    async def get_chat_member(self, chat_id, uid):
        st = self.members.get((chat_id, uid))
        if st is None: raise BadRequest("user not found")
        return NS(status=st, can_promote_members=self.can_promote)
    async def promote_chat_member(self, chat_id, uid, **kw):
        if not self.can_promote: raise BadRequest("Not enough rights")
        if uid == 999: raise BadRequest("USER_NOT_PARTICIPANT")
        if kw.get("can_manage_chat"): self.members[(chat_id, uid)] = "administrator"; self.promoted.append(uid)
        else: self.members.pop((chat_id, uid), None)
    async def get_chat(self, ref):
        return NS(id=ref, type="channel", title="Chan", username=None, available_reactions=None)
    async def send_message(self, uid, text, reply_markup=None): self.notes.append((uid, text))

def mk_config(d):
    return Config(token=tok(1), owners=frozenset({10}), data_dir=Path(d), secret_key=None,
        legacy_state_file=Path(d)/"state.json", reactions_ttl=300, max_concurrency=5,
        max_targets_per_user=2, max_bots_per_user=2, service_public=False, support="@sup", log_level="INFO")

async def main():
    # emoji parser
    assert parse_emojis("🔥 ❤️ 👨‍💻 ❤‍🔥 x 🔥 😀") == ["🔥", "❤", "👨‍💻", "❤‍🔥"], parse_emojis("🔥 ❤️ 👨‍💻 ❤‍🔥")
    print("emoji ok")

    with tempfile.TemporaryDirectory() as d:
        # legacy state to import
        json.dump({"workers": [tok(201), tok(202)], "chats": {"-1001": "Old"}, "chance": 80, "enabled": True},
                  open(Path(d)/"state.json","w"))
        core = Core.build(mk_config(d)); main_bot = Main(); await core.start(main_bot)
        # legacy import
        t = await core.db.get_target_by_chat(-1001)
        assert t and t.chance == 80 and t.owner_id == 10 and t.source is Source.SERVICE, t
        assert len(await core.db.assigned_bot_ids(t.id)) == 2 and len(core.pool.service()) == 2
        # token stored encrypted
        rec = await core.db.get_bot(201); assert tok(201) not in rec.token and core.vault.decrypt(rec.token) == tok(201)
        print("legacy+crypto ok")

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
        upd_c = lambda uid, args: (NS(message=Msg(""), effective_chat=NS(send_message=lambda *a, **k: asyncio.sleep(0)), effective_user=NS(id=uid)), NS(application=ctx.application, args=args, user_data={}))
        u_, c_ = upd_c(10, ["77", "30", "5"]); await ah.cmd_grant(u_, c_); usr = await core.db.get_user(77)
        assert usr.has_service() and usr.service_limit == 5 and usr.days_left() in (30, 31), usr
        u_, c_ = upd_c(10, ["77"]); await ah.cmd_ban(u_, c_); assert (await core.db.get_user(77)).banned
        u_, c_ = upd_c(10, ["10"]); await ah.cmd_ban(u_, c_); assert not (await core.db.get_user(10) or NS(banned=False)).banned
        u_, c_ = upd_c(10, ["77"]); await ah.cmd_revoke(u_, c_); assert not (await core.db.get_user(77)).has_service()
        print("handlers ok")

        # restart: pool reloads from DB with decryption
        await core.close()
        core2 = Core.build(mk_config(d)); await core2.start(Main())
        assert len(core2.pool.service()) == 3 and len(core2.pool.of_user(50)) == 1, (len(core2.pool.service()), len(core2.pool.of_user(50)))
        await core2.close()
        print("restart ok")
asyncio.run(main()); print("ALL OK")
