from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeChat
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, Defaults

from app.config import Config
from app.core import Core
from app.handlers import ADMIN_COMMANDS, ALLOWED_UPDATES, USER_COMMANDS, register

log = logging.getLogger("autoreact")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def on_startup(app: Application) -> None:
    core: Core = app.bot_data["core"]
    await core.start(app.bot)

    await app.bot.set_my_commands([BotCommand(n, d) for n, d in USER_COMMANDS])
    for owner_id in core.config.owners:
        try:
            await app.bot.set_my_commands(
                [BotCommand(n, d) for n, d in (*USER_COMMANDS, *ADMIN_COMMANDS)],
                scope=BotCommandScopeChat(owner_id),
            )
        except TelegramError as exc:  # the owner has not started the bot yet
            log.debug("admin commands not set for %s: %s", owner_id, exc)

    log.info("ready: @%s with %d reaction bots", app.bot.username, len(core.pool))


async def on_shutdown(app: Application) -> None:
    await app.bot_data["core"].close()


def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)
    core = Core.build(config)

    app = (
        Application.builder()
        .token(config.token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.bot_data["core"] = core
    register(app, core)
    app.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
