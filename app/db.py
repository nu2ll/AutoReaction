"""SQLite storage. Plain sqlite3 behind asyncio.to_thread - no extra dependency."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.models import BotRecord, Mode, Source, Target, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT NOT NULL DEFAULT '',
    banned        INTEGER NOT NULL DEFAULT 0,
    service_until INTEGER NOT NULL DEFAULT 0,
    service_limit INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    bot_id     INTEGER PRIMARY KEY,
    username   TEXT NOT NULL,
    token      TEXT NOT NULL,
    owner_id   INTEGER,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id);

CREATE TABLE IF NOT EXISTS targets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    chat_type      TEXT NOT NULL DEFAULT '',
    owner_id       INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    source         TEXT NOT NULL DEFAULT 'both',
    mode           TEXT NOT NULL DEFAULT 'random',
    emojis         TEXT NOT NULL DEFAULT '[]',
    chance         INTEGER NOT NULL DEFAULT 100,
    service_count  INTEGER NOT NULL DEFAULT 5,
    reactions_sent INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_owner ON targets(owner_id);

CREATE TABLE IF NOT EXISTS target_bots (
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    bot_id    INTEGER NOT NULL REFERENCES bots(bot_id) ON DELETE CASCADE,
    PRIMARY KEY (target_id, bot_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TARGET_FIELDS = frozenset(
    {"title", "enabled", "source", "mode", "emojis", "chance", "service_count"}
)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ plumbing
    def _exec(self, sql: str, params: tuple = ()) -> tuple[list[sqlite3.Row], int]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall(), cur.rowcount

    async def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        rows, _ = await asyncio.to_thread(self._exec, sql, params)
        return rows

    async def _x(self, sql: str, params: tuple = ()) -> int:
        _, count = await asyncio.to_thread(self._exec, sql, params)
        return count

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_close)

    # --------------------------------------------------------------------- users
    async def touch_user(self, user_id: int, username: str | None, full_name: str) -> User:
        rows = await self._q(
            """INSERT INTO users(user_id, username, full_name, created_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                                                  full_name=excluded.full_name
               RETURNING *""",
            (user_id, username, full_name, int(time.time())),
        )
        return User.from_row(rows[0])

    async def ensure_user(self, user_id: int) -> None:
        await self._x(
            "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?,?)",
            (user_id, int(time.time())),
        )

    async def get_user(self, user_id: int) -> User | None:
        rows = await self._q("SELECT * FROM users WHERE user_id=?", (user_id,))
        return User.from_row(rows[0]) if rows else None

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.ensure_user(user_id)
        await self._x("UPDATE users SET banned=? WHERE user_id=?", (int(banned), user_id))

    async def set_service(self, user_id: int, until: int, limit: int) -> User:
        await self.ensure_user(user_id)
        rows = await self._q(
            "UPDATE users SET service_until=?, service_limit=? WHERE user_id=? RETURNING *",
            (until, limit, user_id),
        )
        return User.from_row(rows[0])

    # ---------------------------------------------------------------------- bots
    async def save_bot(self, bot_id: int, username: str, token: str, owner_id: int | None) -> None:
        await self._x(
            """INSERT INTO bots(bot_id, username, token, owner_id, status, created_at)
               VALUES (?,?,?,?, 'active', ?)
               ON CONFLICT(bot_id) DO UPDATE SET username=excluded.username,
                                                 token=excluded.token,
                                                 status='active'""",
            (bot_id, username, token, owner_id, int(time.time())),
        )

    async def get_bot(self, bot_id: int) -> BotRecord | None:
        rows = await self._q("SELECT * FROM bots WHERE bot_id=?", (bot_id,))
        return BotRecord.from_row(rows[0]) if rows else None

    async def find_service_bot(self, username: str) -> BotRecord | None:
        rows = await self._q(
            "SELECT * FROM bots WHERE owner_id IS NULL AND lower(username)=lower(?)",
            (username.lstrip("@"),),
        )
        return BotRecord.from_row(rows[0]) if rows else None

    async def list_bots(self, owner_id: int | None) -> list[BotRecord]:
        rows = await self._q(
            "SELECT * FROM bots WHERE owner_id IS ? ORDER BY username COLLATE NOCASE",
            (owner_id,),
        )
        return [BotRecord.from_row(r) for r in rows]

    async def list_active_bots(self) -> list[BotRecord]:
        rows = await self._q("SELECT * FROM bots WHERE status='active'")
        return [BotRecord.from_row(r) for r in rows]

    async def count_bots(self, owner_id: int | None) -> int:
        rows = await self._q("SELECT COUNT(*) AS n FROM bots WHERE owner_id IS ?", (owner_id,))
        return rows[0]["n"]

    async def set_bot_status(self, bot_id: int, status: str) -> None:
        await self._x("UPDATE bots SET status=? WHERE bot_id=?", (status, bot_id))

    async def delete_bot(self, bot_id: int) -> bool:
        return await self._x("DELETE FROM bots WHERE bot_id=?", (bot_id,)) > 0

    async def delete_invalid_service_bots(self) -> int:
        return await self._x("DELETE FROM bots WHERE owner_id IS NULL AND status='invalid'")

    # ------------------------------------------------------------------- targets
    async def create_target(
        self,
        chat_id: int,
        title: str,
        chat_type: str,
        owner_id: int,
        *,
        source: Source = Source.BOTH,
        service_count: int = 5,
        chance: int = 100,
        enabled: bool = True,
    ) -> Target:
        rows = await self._q(
            """INSERT INTO targets(chat_id, title, chat_type, owner_id, enabled, source,
                                   chance, service_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?) RETURNING *""",
            (chat_id, title, chat_type, owner_id, int(enabled), source.value, chance,
             service_count, int(time.time())),
        )
        return Target.from_row(rows[0])

    async def get_target(self, target_id: int) -> Target | None:
        rows = await self._q("SELECT * FROM targets WHERE id=?", (target_id,))
        return Target.from_row(rows[0]) if rows else None

    async def get_target_by_chat(self, chat_id: int) -> Target | None:
        rows = await self._q("SELECT * FROM targets WHERE chat_id=?", (chat_id,))
        return Target.from_row(rows[0]) if rows else None

    async def list_targets(self, owner_id: int) -> list[Target]:
        rows = await self._q("SELECT * FROM targets WHERE owner_id=? ORDER BY id", (owner_id,))
        return [Target.from_row(r) for r in rows]

    async def count_targets(self, owner_id: int) -> int:
        rows = await self._q("SELECT COUNT(*) AS n FROM targets WHERE owner_id=?", (owner_id,))
        return rows[0]["n"]

    async def update_target(self, target_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - TARGET_FIELDS
        if unknown:
            raise ValueError(f"unknown target fields: {sorted(unknown)}")

        values: list[Any] = []
        for key, value in fields.items():
            if key == "emojis":
                value = json.dumps(list(value), ensure_ascii=False)
            elif isinstance(value, bool):
                value = int(value)
            elif isinstance(value, (Source, Mode)):
                value = value.value
            values.append(value)

        assignments = ", ".join(f"{key}=?" for key in fields)
        await self._x(f"UPDATE targets SET {assignments} WHERE id=?", (*values, target_id))

    async def delete_target(self, target_id: int) -> bool:
        return await self._x("DELETE FROM targets WHERE id=?", (target_id,)) > 0

    async def delete_targets_by_chat(self, chat_id: int) -> list[Target]:
        rows = await self._q("DELETE FROM targets WHERE chat_id=? RETURNING *", (chat_id,))
        return [Target.from_row(r) for r in rows]

    async def bump_reactions(self, target_id: int, count: int) -> None:
        await self._x(
            "UPDATE targets SET reactions_sent = reactions_sent + ? WHERE id=?",
            (count, target_id),
        )

    # --------------------------------------------------------------- assignments
    async def assigned_bot_ids(self, target_id: int) -> set[int]:
        rows = await self._q("SELECT bot_id FROM target_bots WHERE target_id=?", (target_id,))
        return {r["bot_id"] for r in rows}

    async def assign(self, target_id: int, bot_id: int) -> None:
        await self._x(
            "INSERT OR IGNORE INTO target_bots(target_id, bot_id) VALUES (?,?)",
            (target_id, bot_id),
        )

    async def unassign(self, target_id: int, bot_id: int) -> None:
        await self._x(
            "DELETE FROM target_bots WHERE target_id=? AND bot_id=?", (target_id, bot_id)
        )

    # --------------------------------------------------------- hot path (1 hop)
    def _post_context(self, chat_id: int) -> tuple[Target, User | None, set[int]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM targets WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is None:
                return None
            target = Target.from_row(row)
            urow = self._conn.execute(
                "SELECT * FROM users WHERE user_id=?", (target.owner_id,)
            ).fetchone()
            ids = {
                r["bot_id"]
                for r in self._conn.execute(
                    "SELECT bot_id FROM target_bots WHERE target_id=?", (target.id,)
                )
            }
        return target, (User.from_row(urow) if urow else None), ids

    async def post_context(self, chat_id: int) -> tuple[Target, User | None, set[int]] | None:
        """Target + owner + assigned bot ids for an incoming post, in a single thread hop."""
        return await asyncio.to_thread(self._post_context, chat_id)

    # ------------------------------------------------------------------ kv/stats
    async def kv_get(self, key: str) -> str | None:
        rows = await self._q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._x(
            "INSERT INTO kv(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def kv_delete(self, key: str) -> None:
        await self._x("DELETE FROM kv WHERE key=?", (key,))

    async def stats(self) -> dict[str, int]:
        rows = await self._q(
            """SELECT
                 (SELECT COUNT(*) FROM users)                                         AS users,
                 (SELECT COUNT(*) FROM users WHERE banned=1)                          AS banned,
                 (SELECT COUNT(*) FROM targets)                                       AS targets,
                 (SELECT COUNT(*) FROM targets WHERE enabled=1)                       AS active_targets,
                 (SELECT COUNT(*) FROM bots WHERE owner_id IS NULL)                   AS service_bots,
                 (SELECT COUNT(*) FROM bots WHERE owner_id IS NULL AND status='invalid') AS invalid_bots,
                 (SELECT COUNT(*) FROM bots WHERE owner_id IS NOT NULL)               AS user_bots,
                 (SELECT COALESCE(SUM(reactions_sent), 0) FROM targets)               AS reactions"""
        )
        return dict(rows[0])
