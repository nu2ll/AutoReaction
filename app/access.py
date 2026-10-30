"""Who may use what."""
from __future__ import annotations

from app.config import Config
from app.db import Database
from app.models import Target, User

# Telegram allows at most 50 admins per chat; keep room for the main bot, humans and own bots.
MAX_SERVICE_PER_CHAT = 40


class Access:
    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._public = config.service_public

    async def load(self) -> None:
        stored = await self._db.kv_get("service_public")
        if stored is not None:
            self._public = stored == "1"

    def is_owner(self, user_id: int) -> bool:
        return user_id in self._config.owners

    @property
    def public(self) -> bool:
        return self._public

    async def set_public(self, flag: bool) -> None:
        self._public = flag
        await self._db.kv_set("service_public", "1" if flag else "0")

    def service_allowed(self, user_id: int, user: User | None) -> bool:
        if self.is_owner(user_id) or self._public:
            return True
        return bool(user and user.has_service())

    def service_cap(self, user_id: int, user: User | None) -> int:
        """Per-user ceiling of service bots per chat (0 = no ceiling beyond the global one)."""
        if self.is_owner(user_id) or user is None:
            return 0
        return max(0, user.service_limit)

    def effective_service_count(self, target: Target, user: User | None) -> int:
        want = min(max(0, target.service_count), MAX_SERVICE_PER_CHAT)
        cap = self.service_cap(target.owner_id, user)
        return min(want, cap) if cap else want

    def can_manage(self, user_id: int, target: Target) -> bool:
        return target.owner_id == user_id or self.is_owner(user_id)
