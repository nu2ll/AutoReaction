from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application

from app.config import Config
from app.core import Core
from app.handlers import COMMANDS, register

log = logging.getLogger("autoreact")


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def on_startup(app: Application) -> None:
    core: Core = app.bot_data["core"]

    for token in core.store.settings.workers:
        try:
            await core.pool.add(token)
        except TelegramError as exc:
            log.warning("worker skipped: %s", exc)

    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc, _ in COMMANDS])
    log.info("ready: @%s with %d workers", app.bot.username, len(core.pool))


async def on_shutdown(app: Application) -> None:
    await app.bot_data["core"].close()


def main() -> None:
    setup_logging()
    config = Config.from_env()
    core = Core.build(config)

    app = (
        Application.builder()
        .token(config.token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.bot_data["core"] = core
    register(app, core)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
