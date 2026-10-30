from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Hashable

from telegram import Bot, ReactionType, ReactionTypeCustomEmoji, ReactionTypeEmoji
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut

from app.access import Access
from app.db import Database
from app.emoji import norm
from app.models import Mode, Target, User
from app.pool import BotPool
from app.utils import seconds

log = logging.getLogger(__name__)

DEFAULT_REACTIONS: tuple[ReactionType, ...] = tuple(ReactionTypeEmoji(e.value) for e in ReactionEmoji)
SUPPORTED_TYPES = (ReactionTypeEmoji, ReactionTypeCustomEmoji)
MAX_ATTEMPTS = 3


class ReactionCache:
    """Which reactions a chat allows (cached; the main bot fetches it)."""

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._data: dict[int, tuple[float, tuple[ReactionType, ...]]] = {}

    async def get(self, bot: Bot, chat_id: int) -> tuple[ReactionType, ...]:
        cached = self._data.get(chat_id)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]

        chat = await bot.get_chat(chat_id)
        if chat.available_reactions is None:
            reactions = DEFAULT_REACTIONS
        else:
            reactions = tuple(r for r in chat.available_reactions if isinstance(r, SUPPORTED_TYPES))

        self._data[chat_id] = (time.monotonic(), reactions)
        return reactions

    def drop(self, chat_id: int) -> None:
        self._data.pop(chat_id, None)

    def clear(self) -> None:
        self._data.clear()


class AlbumGuard:
    """An album arrives as N messages; react once per album."""

    def __init__(self, size: int = 1000) -> None:
        self._size = size
        self._seen: OrderedDict[Hashable, None] = OrderedDict()

    def is_new(self, key: Hashable) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


class Reactor:
    def __init__(
        self,
        db: Database,
        pool: BotPool,
        access: Access,
        cache: ReactionCache,
        concurrency: int,
    ) -> None:
        self._db = db
        self._pool = pool
        self._access = access
        self._cache = cache
        self._albums = AlbumGuard()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        # Set by Core: called when Telegram rejects a bot's token (revoked).
        self.on_dead: Callable[[Bot], Awaitable[None]] | None = None

    # ------------------------------------------------------------------ entry
    async def on_message(
        self, main: Bot, chat_id: int, message_id: int, media_group_id: str | None
    ) -> None:
        ctx = await self._db.post_context(chat_id)
        if ctx is None:
            return
        target, user, assigned = ctx
        if not target.enabled or not assigned or (user and user.banned):
            return
        if media_group_id and not self._albums.is_new((chat_id, media_group_id)):
            return

        bots = self.select_bots(target, user, assigned)
        if not bots:
            return

        reactions = await self.reactions_for(main, target)
        if not reactions:
            return

        task = asyncio.create_task(self._run(target, message_id, bots, reactions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -------------------------------------------------------------- selection
    def select_bots(self, target: Target, user: User | None, assigned: set[int]) -> list[Bot]:
        own: list[Bot] = []
        service: list[Bot] = []
        for bot_id in assigned:
            bot = self._pool.get(bot_id)
            if bot is None:
                continue
            owner = self._pool.owner_of(bot_id)
            if owner is None:
                service.append(bot)
            elif owner == target.owner_id:
                own.append(bot)

        chosen: list[Bot] = own if target.uses_own else []
        if target.uses_service and self._access.service_allowed(target.owner_id, user):
            want = self._access.effective_service_count(target, user)
            if len(service) > want:
                service = random.sample(service, want)
            chosen = chosen + service

        return [bot for bot in chosen if random.randint(1, 100) <= target.chance]

    async def reactions_for(self, main: Bot, target: Target) -> tuple[ReactionType, ...]:
        try:
            reactions = await self._cache.get(main, target.chat_id)
        except TelegramError as exc:
            log.error("cannot load reactions for %s: %s", target.chat_id, exc)
            return ()

        if not reactions:
            log.info("reactions disabled in %s", target.chat_id)
            return ()

        if target.mode is Mode.CUSTOM:
            wanted = {norm(e) for e in target.emojis}
            reactions = tuple(
                r for r in reactions if isinstance(r, ReactionTypeEmoji) and norm(r.emoji) in wanted
            )
            if not reactions:
                log.info("none of the custom emoji is allowed in %s", target.chat_id)
        return reactions

    # -------------------------------------------------------------- execution
    async def _run(
        self,
        target: Target,
        message_id: int,
        bots: list[Bot],
        reactions: tuple[ReactionType, ...],
    ) -> None:
        results = await asyncio.gather(
            *(self._react(bot, target.chat_id, message_id, random.choice(reactions)) for bot in bots),
            return_exceptions=True,
        )
        ok = sum(1 for r in results if r is True)
        if ok:
            try:
                await self._db.bump_reactions(target.id, ok)
            except Exception:  # noqa: BLE001 - statistics must never break reacting
                log.exception("could not store reaction counter")

    async def _react(self, bot: Bot, chat_id: int, message_id: int, reaction: ReactionType) -> bool:
        for _ in range(MAX_ATTEMPTS):
            delay = 0.0
            async with self._semaphore:
                try:
                    await bot.set_message_reaction(chat_id, message_id, reaction=reaction)
                except RetryAfter as exc:
                    delay = seconds(exc.retry_after) + 1
                except InvalidToken:
                    log.warning("@%s: token rejected, disabling bot", bot.username)
                    if self.on_dead:
                        await self.on_dead(bot)
                    return False
                except (Forbidden, BadRequest) as exc:
                    log.warning("@%s rejected in %s: %s", bot.username, chat_id, exc.message)
                    return False
                except (TimedOut, NetworkError) as exc:
                    log.warning("@%s network error in %s: %s", bot.username, chat_id, exc)
                    delay = 1.0
                except TelegramError as exc:
                    log.error("@%s failed in %s: %s", bot.username, chat_id, exc)
                    return False
                else:
                    label = getattr(reaction, "emoji", None) or getattr(reaction, "custom_emoji_id", "?")
                    log.info("@%s -> %s/%s %s", bot.username, chat_id, message_id, label)
                    return True
            await asyncio.sleep(delay)  # sleep outside the semaphore so others keep going
        return False

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
