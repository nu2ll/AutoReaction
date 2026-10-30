from __future__ import annotations

from datetime import timedelta


def seconds(value: float | timedelta) -> float:
    """PTB exposes RetryAfter.retry_after as int or timedelta depending on the version."""
    return value.total_seconds() if isinstance(value, timedelta) else float(value)
