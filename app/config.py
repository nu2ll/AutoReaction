from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    owners: frozenset[int]
    data_dir: Path
    secret_key: str | None
    max_concurrency: int
    max_targets_per_user: int
    max_bots_per_user: int
    service_public: bool
    support: str
    log_level: str
    helper_api_id: int = 0
    helper_api_hash: str = ""
    helper_session: str = ""
    helper_leave: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoreaction.db"

    @property
    def helper_enabled(self) -> bool:
        return bool(self.helper_api_id and self.helper_api_hash and self.helper_session)

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        token = os.getenv("MAIN_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("MAIN_BOT_TOKEN is not set")

        owners = frozenset(
            int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x
        )
        if not owners:
            raise RuntimeError("OWNER_IDS is not set (comma separated numeric Telegram IDs)")

        return cls(
            token=token,
            owners=owners,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            secret_key=os.getenv("SECRET_KEY", "").strip() or None,
            max_concurrency=_int("MAX_CONCURRENCY", 20),
            max_targets_per_user=_int("MAX_TARGETS_PER_USER", 5),
            max_bots_per_user=_int("MAX_BOTS_PER_USER", 10),
            service_public=_flag("SERVICE_PUBLIC", False),
            support=os.getenv("SUPPORT_CONTACT", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            helper_api_id=_int("HELPER_API_ID", 0),
            helper_api_hash=os.getenv("HELPER_API_HASH", "").strip(),
            helper_session=os.getenv("HELPER_SESSION", "").strip(),
            helper_leave=_flag("HELPER_LEAVE", True),
        )
