from __future__ import annotations

from collections.abc import Iterator

from telegram import Bot


class WorkerPool:
    def __init__(self) -> None:
        self._bots: dict[str, Bot] = {}

    def __iter__(self) -> Iterator[Bot]:
        return iter(tuple(self._bots.values()))

    def __len__(self) -> int:
        return len(self._bots)

    async def add(self, token: str) -> Bot:
        if bot := self._bots.get(token):
            return bot
        bot = Bot(token)
        await bot.initialize()
        self._bots[token] = bot
        return bot

    async def remove(self, username: str) -> str | None:
        target = username.lstrip("@").lower()
        for token, bot in tuple(self._bots.items()):
            if bot.username.lower() == target:
                await bot.shutdown()
                del self._bots[token]
                return token
        return None

    async def close(self) -> None:
        for bot in self._bots.values():
            await bot.shutdown()
        self._bots.clear()
