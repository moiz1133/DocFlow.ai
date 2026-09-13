"""Tiny "<int><unit>" duration parser for Settings fields like
PURGE_UNRETAINED_AFTER — simple string config (".env"-friendly) rather
than requiring deployers to compute raw seconds by hand.
"""

import re
from datetime import timedelta

_PATTERN = re.compile(r"^(\d+)\s*([smhd])$")

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


class InvalidDurationError(ValueError):
    """Raised when a duration string doesn't match "<int><s|m|h|d>"."""


def parse_duration(value: str) -> timedelta:
    match = _PATTERN.match(value.strip())
    if match is None:
        raise InvalidDurationError(
            f"invalid duration {value!r} — expected a number followed by s/m/h/d, e.g. '24h'"
        )
    amount, unit = match.groups()
    return timedelta(seconds=int(amount) * _UNIT_SECONDS[unit])
