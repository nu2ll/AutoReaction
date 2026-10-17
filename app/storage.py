from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass(slots=True)
class Settings:
    enabled: bool = True
    chance: int = 100
    chats: dict[str, str] = field(default_factory=dict)
    workers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        if not self._path.exists():
            return Settings()
        return Settings.from_dict(json.loads(self._path.read_text(encoding="utf-8")))

    async def save(self) -> None:
        payload = json.dumps(asdict(self.settings), ensure_ascii=False, indent=2)
        async with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
