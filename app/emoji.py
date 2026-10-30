"""Parsing of the reaction emoji list a user sends us."""
from __future__ import annotations

from telegram.constants import ReactionEmoji

MAX_CUSTOM_EMOJIS = 20


def norm(value: str) -> str:
    """Drop variation selectors so '❤' and '❤️' compare equal."""
    return value.replace("\ufe0f", "")


# normalised -> exact string Telegram accepts
ALLOWED: dict[str, str] = {norm(e.value): e.value for e in ReactionEmoji}
_KEYS = sorted(ALLOWED, key=len, reverse=True)


def parse_emojis(text: str) -> list[str]:
    """Extract the emoji Telegram allows as reactions, in order, without duplicates."""
    text = norm(text)
    found: list[str] = []
    i = 0
    while i < len(text):
        for key in _KEYS:
            if text.startswith(key, i):
                value = ALLOWED[key]
                if value not in found:
                    found.append(value)
                i += len(key)
                break
        else:
            i += 1
    return found[:MAX_CUSTOM_EMOJIS]
