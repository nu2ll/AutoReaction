from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from enum import StrEnum

UNLIMITED = -1  # users.service_until sentinel: access without expiry


class Source(StrEnum):
    OWN = "own"
    SERVICE = "service"
    BOTH = "both"


class Mode(StrEnum):
    RANDOM = "random"
    CUSTOM = "custom"


@dataclass(slots=True)
class User:
    user_id: int
    username: str | None
    full_name: str
    banned: bool
    service_until: int
    service_limit: int
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            user_id=row["user_id"],
            username=row["username"],
            full_name=row["full_name"] or "",
            banned=bool(row["banned"]),
            service_until=row["service_until"],
            service_limit=row["service_limit"],
            created_at=row["created_at"],
        )

    def has_service(self, now: float | None = None) -> bool:
        if self.service_until == UNLIMITED:
            return True
        return self.service_until > (now if now is not None else time.time())

    def days_left(self, now: float | None = None) -> int:
        remaining = self.service_until - (now if now is not None else time.time())
        return max(0, math.ceil(remaining / 86400))


@dataclass(slots=True)
class BotRecord:
    bot_id: int
    username: str
    token: str  # encrypted
    owner_id: int | None  # None = service pool
    status: str  # active | invalid
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> BotRecord:
        return cls(
            bot_id=row["bot_id"],
            username=row["username"],
            token=row["token"],
            owner_id=row["owner_id"],
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class Target:
    id: int
    chat_id: int
    title: str
    chat_type: str
    owner_id: int
    enabled: bool
    source: Source
    mode: Mode
    emojis: list[str] = field(default_factory=list)
    chance: int = 100
    service_count: int = 5
    reactions_sent: int = 0
    created_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Target:
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            title=row["title"],
            chat_type=row["chat_type"],
            owner_id=row["owner_id"],
            enabled=bool(row["enabled"]),
            source=Source(row["source"]),
            mode=Mode(row["mode"]),
            emojis=json.loads(row["emojis"] or "[]"),
            chance=row["chance"],
            service_count=row["service_count"],
            reactions_sent=row["reactions_sent"],
            created_at=row["created_at"],
        )

    @property
    def uses_own(self) -> bool:
        return self.source in (Source.OWN, Source.BOTH)

    @property
    def uses_service(self) -> bool:
        return self.source in (Source.SERVICE, Source.BOTH)
