"""Settings for live models, read from the ``live:`` section of project.yml.

Kept free of engine imports: discovery parses ``live_interval`` with
:func:`parse_duration`, and discovery is imported by nearly everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off", ""}


def parse_duration(value: Any) -> float:
    """Seconds from ``10``, ``2.5``, ``"500ms"``, ``"10s"``, ``"5m"`` or ``"1h"``.

    A bare number is seconds. Raises ValueError for anything else, including
    a negative number, so a typo is reported rather than read as zero.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"duration cannot be negative: {value!r}")
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(
            f"not a duration: {value!r} (use e.g. 500ms, 10s, 5m or a number of seconds)"
        )
    number, unit = match.groups()
    return float(number) * _UNIT_SECONDS[unit.lower() if unit else None]


def parse_live_flag(value: Any) -> bool:
    """Whether an ``@config live=`` value turns the model live.

    Anything that is not a recognised true word is False here; the validator
    reports unrecognised values so ``live=ture`` does not pass silently.
    """
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE


def is_valid_live_flag(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE | _FALSE


@dataclass
class LiveSettings:
    """How the live runner schedules refreshes.

    Durations are seconds. Coalescing works like this: the first source
    advance after a quiet period opens a batch; the batch closes once no new
    advance has arrived for ``debounce`` seconds, or ``max_latency`` seconds
    after it opened, whichever comes first. Cycles never start closer than
    ``min_interval`` apart, so a burst of a thousand small commits becomes a
    handful of refreshes rather than a thousand.
    """

    enabled: bool = True             # start the runner inside `havn serve`
    min_interval: float = 1.0        # least time between two refresh cycles
    debounce: float = 0.2            # quiet time that closes a batch early
    max_latency: float = 5.0         # longest a batch is held open
    poll_interval: float = 5.0       # re-read source watermarks this often
    log_interval: float = 60.0       # one aggregated run_log row per model per interval
    backoff_base: float = 2.0        # first retry delay after a failure
    backoff_max: float = 300.0       # longest retry delay
    assertion_interval: float = 0.0  # 0 = check @assert on every refresh
    profile_interval: float = 300.0  # profile a live model at most this often
    max_lag: float = 300.0           # freshness: a live model behind by more is stale

    @classmethod
    def from_raw(cls, raw: dict | None) -> "LiveSettings":
        """Build from the raw ``live:`` mapping; unknown keys are ignored."""
        settings = cls()
        if not isinstance(raw, dict):
            return settings
        for f in fields(cls):
            if f.name not in raw or raw[f.name] is None:
                continue
            value = raw[f.name]
            if f.name == "enabled":
                settings.enabled = bool(value) if isinstance(value, bool) else parse_live_flag(value)
            else:
                setattr(settings, f.name, parse_duration(value))
        return settings

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}
