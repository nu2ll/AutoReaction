from __future__ import annotations

from dataclasses import dataclass

from telegram import Bot

from app.config import Config
from app.reactions import ReactionCache, Reactor
from app.storage import Store
from app.workers import WorkerPool


@dataclass(slots=True)
class Core:
    config: Config
    store: Store
    pool: WorkerPool
    cache: ReactionCache
    reactor: Reactor

    @classmethod
    def build(cls, config: Config) -> Core:
        cache = ReactionCache(config.reactions_ttl)
        return cls(
            config=config,
            store=Store(config.state_file),
            pool=WorkerPool(),
            cache=cache,
            reactor=Reactor(cache, config.max_concurrency),
        )

    def bots(self, main: Bot) -> tuple[Bot, ...]:
        return (main, *self.pool)

    async def close(self) -> None:
        await self.reactor.close()
        await self.pool.close()
