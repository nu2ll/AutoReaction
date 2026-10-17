from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Iterable
from datetime import timedelta

from telegram import Bot, ReactionType, ReactionTypeCustomEmoji, ReactionTypeEmoji
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.storage import Settings

log = logging.getLogger(__name__)

DEFAULT_REACTIONS: tuple[ReactionType, ...] = tuple(ReactionTypeEmoji(e.value) for e in ReactionEmoji)
SUPPORTED_TYPES = (ReactionTypeEmoji, ReactionTypeCustomEmoji)
MAX_ATTEMPTS = 3


def _seconds(value: float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class ReactionCache:
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

    def clear(self) -> None:
        self._data.clear()


class AlbumGuard:
    def __init__(self, size: int = 500) -> None:
        self._size = size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def is_new(self, group_id: str) -> bool:
        if group_id in self._seen:
            return False
        self._seen[group_id] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


class Reactor:
    def __init__(self, cache: ReactionCache, concurrency: int) -> None:
        self._cache = cache
        self._albums = AlbumGuard()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(
        self,
        main: Bot,
        bots: Iterable[Bot],
        settings: Settings,
        chat_id: int,
        message_id: int,
        media_group_id: str | None,
    ) -> None:
        if media_group_id and not self._albums.is_new(media_group_id):
            return

        try:
            reactions = await self._cache.get(main, chat_id)
        except TelegramError as exc:
            log.error("cannot load reactions for %s: %s", chat_id, exc)
            return

        if not reactions:
            log.info("reactions disabled in %s", chat_id)
            return

        for bot in bots:
            if random.randint(1, 100) > settings.chance:
                continue
            reaction = random.choice(reactions)
            task = asyncio.create_task(self._react(bot, chat_id, message_id, reaction))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _react(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        reaction: ReactionType,
    ) -> None:
        async with self._semaphore:
            for _ in range(MAX_ATTEMPTS):
                try:
                    await bot.set_message_reaction(chat_id, message_id, reaction=reaction)
                except RetryAfter as exc:
                    await asyncio.sleep(_seconds(exc.retry_after) + 1)
                except (Forbidden, BadRequest) as exc:
                    log.warning("@%s rejected in %s: %s", bot.username, chat_id, exc.message)
                    return
                except TelegramError as exc:
                    log.error("@%s failed in %s: %s", bot.username, chat_id, exc)
                    return
                else:
                    label = getattr(reaction, "emoji", None) or getattr(reaction, "custom_emoji_id", "?")
                    log.info("@%s -> %s/%s %s", bot.username, chat_id, message_id, label)
                    return

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
