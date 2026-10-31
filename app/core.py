from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import InvalidToken, TelegramError

from app.access import Access
from app.config import Config
from app.crypto import Vault
from app.db import Database
from app.helper import Helper
from app.pool import BotPool
from app.reactor import Reactor

log = logging.getLogger(__name__)


class BotRegistrationError(Exception):
    """Raised with a user-facing (Persian) message."""


@dataclass(slots=True)
class Core:
    config: Config
    db: Database
    vault: Vault
    pool: BotPool
    access: Access
    reactor: Reactor
    main: Bot | None = None
    helper: Helper | None = None
    helper_login: Any = None  # phone login in progress (admin panel)
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @classmethod
    def build(cls, config: Config) -> Core:
        db = Database(config.db_path)
        vault = Vault.load(config.secret_key, config.data_dir / "secret.key")
        pool = BotPool()
        access = Access(config, db)
        reactor = Reactor(db, pool, access, config.max_concurrency)
        core = cls(config, db, vault, pool, access, reactor)
        reactor.on_dead = core.handle_dead_bot
        return core

    # ---------------------------------------------------------------- lifecycle
    async def start(self, main: Bot) -> None:
        self.main = main
        await self._start_helper()
        await self.access.load()
        await self._load_bots()

    async def _start_helper(self) -> None:
        """The panel-managed account (database) wins; .env values are only the default."""
        cfg = self.config
        if await self.db.kv_get("helper_managed"):
            raw_session = await self.db.kv_get("helper_session")
            if not raw_session:
                return  # removed from the panel
            try:
                api_id = int(await self.db.kv_get("helper_api_id") or 0)
                api_hash = self.vault.decrypt(await self.db.kv_get("helper_api_hash") or "")
                session = self.vault.decrypt(raw_session)
            except Exception:  # noqa: BLE001
                log.error("cannot decrypt the helper account (wrong SECRET_KEY?)")
                return
        elif cfg.helper_enabled:
            api_id, api_hash, session = cfg.helper_api_id, cfg.helper_api_hash, cfg.helper_session
        else:
            return
        helper = Helper(api_id, api_hash, session, await self._helper_leave())
        try:
            await helper.start()
        except Exception:  # noqa: BLE001 - the bot works without it
            log.exception("helper account disabled")
            return
        self.helper = helper

    async def _helper_leave(self) -> bool:
        """Leave the chat after the work? The panel's choice wins over HELPER_LEAVE in .env."""
        saved = await self.db.kv_get("helper_leave")
        return self.config.helper_leave if saved is None else saved == "1"

    async def set_helper_leave(self, leave: bool) -> None:
        await self.db.kv_set("helper_leave", "1" if leave else "0")
        if self.helper is not None:
            self.helper.leave_after = leave

    async def set_helper(self, api_id: int, api_hash: str, session: str) -> Helper:
        """Connect a (new) helper account, keep it encrypted in the database. Raises on failure."""
        helper = Helper(api_id, api_hash, session, await self._helper_leave())
        await helper.start()
        old, self.helper = self.helper, helper
        if old is not None:
            await old.close()
        await self.db.kv_set("helper_api_id", str(api_id))
        await self.db.kv_set("helper_api_hash", self.vault.encrypt(api_hash))
        await self.db.kv_set("helper_session", self.vault.encrypt(session))
        await self.db.kv_set("helper_managed", "1")
        return helper

    async def remove_helper(self) -> bool:
        """Forget the helper account and terminate its session. False if there was none."""
        helper, self.helper = self.helper, None
        for key in ("helper_api_id", "helper_api_hash", "helper_session"):
            await self.db.kv_delete(key)
        await self.db.kv_set("helper_managed", "1")  # .env must not bring it back
        if helper is None:
            return False
        await helper.logout()
        return True

    async def close(self) -> None:
        if self.helper_login is not None:
            await self.helper_login.close()
        if self.helper is not None:
            await self.helper.close()
        await self.reactor.close()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.pool.close()
        await self.db.close()

    async def _load_bots(self) -> None:
        records = await self.db.list_active_bots()
        gate = asyncio.Semaphore(10)

        async def load(rec) -> bool:
            async with gate:
                try:
                    token = self.vault.decrypt(rec.token)
                except Exception:  # noqa: BLE001
                    log.error("cannot decrypt token of @%s (wrong SECRET_KEY?)", rec.username)
                    return False
                try:
                    bot = await self.pool.connect(token)
                except InvalidToken:
                    await self.db.set_bot_status(rec.bot_id, "invalid")
                    log.warning("@%s: token is no longer valid", rec.username)
                    return False
                except TelegramError as exc:
                    log.warning("@%s skipped: %s", rec.username, exc)
                    return False
                self.pool.attach(bot, rec.owner_id)
                return True

        results = await asyncio.gather(*(load(r) for r in records))
        log.info("loaded %d/%d bots", sum(results), len(records))

    # -------------------------------------------------------------------- tasks
    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    async def notify(
        self, user_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        assert self.main is not None
        try:
            await self.main.send_message(user_id, text, reply_markup=markup)
            return True
        except TelegramError as exc:
            log.debug("cannot notify %s: %s", user_id, exc)
            return False

    # --------------------------------------------------------------------- bots
    async def register_bot(self, token: str, owner_id: int | None) -> Bot:
        """Validate a token, store it encrypted and make the bot live. owner_id None = service."""
        try:
            bot = await self.pool.connect(token)
        except InvalidToken as exc:
            raise BotRegistrationError("❌ توکن نامعتبره.") from exc
        except TelegramError as exc:
            raise BotRegistrationError(f"❌ خطا در اتصال به تلگرام: {html.escape(str(exc))}") from exc

        try:
            if self.main is not None and bot.id == self.main.id:
                raise BotRegistrationError("❌ ربات اصلی رو نمی‌شه به‌عنوان ربات ری‌اکشن اضافه کرد.")

            existing = await self.db.get_bot(bot.id)
            if existing and existing.owner_id != owner_id:
                raise BotRegistrationError("❌ این ربات قبلاً توسط حساب دیگه‌ای ثبت شده.")

            if (
                existing is None
                and owner_id is not None
                and not self.access.is_owner(owner_id)
                and await self.db.count_bots(owner_id) >= self.config.max_bots_per_user
            ):
                raise BotRegistrationError(
                    f"❌ سقف تعداد ربات ({self.config.max_bots_per_user}) پر شده."
                )

            await self.db.save_bot(bot.id, bot.username, self.vault.encrypt(token), owner_id)
        except BaseException:
            await bot.shutdown()
            raise

        await self.pool.detach(bot.id)  # replace a stale instance (token rotation)
        self.pool.attach(bot, owner_id)
        return bot

    async def remove_bot(self, bot_id: int) -> bool:
        await self.pool.detach(bot_id)
        return await self.db.delete_bot(bot_id)

    async def handle_dead_bot(self, bot: Bot) -> None:
        """Token was revoked: stop using it and tell whoever owns it."""
        if not await self.pool.detach(bot.id):
            return  # already handled by a concurrent failure
        rec = await self.db.get_bot(bot.id)
        await self.db.set_bot_status(bot.id, "invalid")
        text = f"⚠️ توکن ربات @{bot.username} دیگه معتبر نیست و غیرفعال شد."
        for uid in (self.config.owners if rec is None or rec.owner_id is None else {rec.owner_id}):
            await self.notify(uid, text)
