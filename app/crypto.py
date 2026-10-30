"""Encryption of bot tokens at rest."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)


class Vault:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def load(cls, configured: str | None, key_file: Path) -> Vault:
        if configured:
            return cls(configured.encode())

        if key_file.exists():
            return cls(key_file.read_bytes().strip())

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        log.warning(
            "SECRET_KEY is not set: generated a new key at %s. "
            "Back it up (or set SECRET_KEY) - without it stored tokens cannot be decrypted.",
            key_file,
        )
        return cls(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()
