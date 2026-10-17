from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    owners: frozenset[int]
    state_file: Path
    reactions_ttl: int
    max_concurrency: int

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        return cls(
            token=os.environ["MAIN_BOT_TOKEN"],
            owners=frozenset(int(x) for x in os.environ["OWNER_IDS"].split(",") if x.strip()),
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            reactions_ttl=int(os.getenv("REACTIONS_TTL", "300")),
            max_concurrency=int(os.getenv("MAX_CONCURRENCY", "20")),
        )
