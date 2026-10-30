"""In-memory registry of every live Bot object (service bots and users' own bots)."""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)


class BotPool:
    def __init__(self, connections_per_bot: int = 4) -> None:
        self._connections = connections_per_bot
        self._bots: dict[int, Bot] = {}
        self._owner: dict[int, int | None] = {}  # bot_id -> user_id, None for service bots

    # ---------------------------------------------------------------- lifecycle
    async def connect(self, token: str) -> Bot:
        """Create and initialise a Bot (validates the token). Does not register it."""
        request = HTTPXRequest(connection_pool_size=self._connections)
        updates_request = HTTPXRequest(connection_pool_size=1)
        bot = Bot(token, request=request, get_updates_request=updates_request)
        try:
            await bot.initialize()
        except Exception:
            # Bot.shutdown() is a no-op when initialisation failed, so close the clients ourselves.
            await request.shutdown()
            await updates_request.shutdown()
            raise
        return bot

    def attach(self, bot: Bot, owner_id: int | None) -> None:
        self._bots[bot.id] = bot
        self._owner[bot.id] = owner_id

    async def detach(self, bot_id: int) -> bool:
        bot = self._bots.pop(bot_id, None)
        self._owner.pop(bot_id, None)
        if bot is None:
            return False
        try:
            await bot.shutdown()
        except Exception as exc:  # noqa: BLE001 - shutting down must never raise
            log.debug("shutdown of %s failed: %s", bot_id, exc)
        return True

    async def close(self) -> None:
        for bot_id in tuple(self._bots):
            await self.detach(bot_id)

    # ------------------------------------------------------------------ queries
    def get(self, bot_id: int) -> Bot | None:
        return self._bots.get(bot_id)

    def has_owner(self, bot_id: int) -> bool:
        return bot_id in self._owner

    def owner_of(self, bot_id: int) -> int | None:
        return self._owner.get(bot_id)

    def service(self) -> list[Bot]:
        return [bot for bid, bot in self._bots.items() if self._owner[bid] is None]

    def of_user(self, user_id: int) -> list[Bot]:
        return [bot for bid, bot in self._bots.items() if self._owner[bid] == user_id]

    def __len__(self) -> int:
        return len(self._bots)
