from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import InvalidToken, TelegramError

from app.access import MAX_SERVICE_PER_CHAT, Access
from app.config import Config
from app.crypto import Vault
from app.db import Database
from app.models import Source
from app.pool import BotPool
from app.reactor import ReactionCache, Reactor

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
    cache: ReactionCache
    reactor: Reactor
    main: Bot | None = None
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @classmethod
    def build(cls, config: Config) -> Core:
        db = Database(config.db_path)
        vault = Vault.load(config.secret_key, config.data_dir / "secret.key")
        pool = BotPool()
        access = Access(config, db)
        cache = ReactionCache(config.reactions_ttl)
        reactor = Reactor(db, pool, access, cache, config.max_concurrency)
        core = cls(config, db, vault, pool, access, cache, reactor)
        reactor.on_dead = core.handle_dead_bot
        return core

    # ---------------------------------------------------------------- lifecycle
    async def start(self, main: Bot) -> None:
        self.main = main
        await self.access.load()
        await self._load_bots()
        await self._import_legacy()

    async def close(self) -> None:
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

    # ------------------------------------------------------------- legacy (v1)
    async def _import_legacy(self) -> None:
        path = self.config.legacy_state_file
        if not path.exists() or await self.db.kv_get("legacy_imported"):
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("cannot read legacy state file %s", path)
            return

        owner = min(self.config.owners)
        await self.db.ensure_user(owner)

        added: list[int] = []
        for token in data.get("workers", []):
            try:
                bot = await self.register_bot(token, None)
            except BotRegistrationError as exc:
                log.warning("legacy worker skipped: %s", exc)
                continue
            added.append(bot.id)

        for chat_id, title in data.get("chats", {}).items():
            if await self.db.get_target_by_chat(int(chat_id)):
                continue
            target = await self.db.create_target(
                int(chat_id),
                title,
                "",
                owner,
                source=Source.SERVICE,
                service_count=min(len(added), MAX_SERVICE_PER_CHAT),
                chance=int(data.get("chance", 100)),
                enabled=bool(data.get("enabled", True)),
            )
            for bot_id in added[:MAX_SERVICE_PER_CHAT]:  # v1 workers were already admins there
                await self.db.assign(target.id, bot_id)

        await self.db.kv_set("legacy_imported", "1")
        log.info(
            "imported legacy state: %d bots, %d chats", len(added), len(data.get("chats", {}))
        )
