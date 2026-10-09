"""Five-field cron expressions: parse, validate, match and find the next fire.

One implementation shared by the scheduler loop (``engine/scheduler.py``) and
the jobs engine (``engine/orchestration.py``), so validation, matching and the
"next run" shown in the UI cannot disagree.

Semantics follow Vixie cron (the cron most people know from Linux):

- fields are ``minute hour day-of-month month day-of-week``
- items: ``*``, ``N``, ``A-B``, ``*/S``, ``A-B/S`` and ``A/S`` (= ``A-max/S``),
  comma-separated
- steps are anchored at the start of the field's range, so ``*/2`` in
  day-of-month is 1,3,5,... and ``*/3`` in month is 1,4,7,10
- month and weekday accept three-letter names (``JAN``, ``MON``); weekday 7
  is Sunday, like 0
- when day-of-month and day-of-week are both restricted (neither starts with
  ``*``), a day matches if EITHER matches; otherwise both must
- ``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly`` and
  ``@yearly``/``@annually`` are accepted as shorthands

Times are naive local datetimes, as everywhere else in the scheduler.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from functools import lru_cache


class CronError(ValueError):
    """Raised for an expression that is not valid 5-field cron."""


_MONTH_NAMES = {
    name: i + 1
    for i, name in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    )
}
_DOW_NAMES = {name: i for i, name in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}

# (low, high, names) per field. Weekday allows 7 (Sunday) on input; it is
# folded onto 0 after parsing.
_FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _MONTH_NAMES),
    ("day-of-week", 0, 7, _DOW_NAMES),
)

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# How far ahead next_fire looks. Eight years covers every schedule that can
# fire at all, including Feb 29 ("0 0 29 2 *") across a skipped century leap
# year (2096 -> 2104); day-level iteration keeps this to ~3000 cheap checks.
_SEARCH_DAYS = 366 * 8


def _parse_value(text: str, field: str, low: int, high: int, names: dict | None) -> int:
    if names is not None and text.upper() in names:
        return names[text.upper()]
    # isascii: str.isdigit() accepts "²", which int() then rejects.
    if not (text.isascii() and text.isdigit()):
        raise CronError(f"{field}: {text!r} is not a number")
    value = int(text)
    if not low <= value <= high:
        raise CronError(f"{field}: {value} is outside {low}-{high}")
    return value


def parse_field(pattern: str, field: str, low: int, high: int, names: dict | None = None) -> frozenset[int]:
    """Parse one cron field into the set of values it allows.

    Raises ``CronError`` for anything malformed: empty items, a zero or
    negative step, values out of range, or a reversed range.
    """
    if not pattern:
        raise CronError(f"{field}: empty field")
    values: set[int] = set()
    for item in pattern.split(","):
        if not item:
            raise CronError(f"{field}: empty item in {pattern!r}")
        head, sep, step_text = item.partition("/")
        if sep:
            if not (step_text.isascii() and step_text.isdigit()) or int(step_text) <= 0:
                raise CronError(f"{field}: invalid step {step_text!r}")
            step = int(step_text)
        else:
            step = 1
        if head == "*":
            lo, hi = low, high
        elif "-" in head:
            lo_text, _, hi_text = head.partition("-")
            lo = _parse_value(lo_text, field, low, high, names)
            hi = _parse_value(hi_text, field, low, high, names)
            if lo > hi:
                raise CronError(f"{field}: reversed range {head!r}")
        else:
            lo = _parse_value(head, field, low, high, names)
            # "5/15" means "from 5 to the end, every 15" (Vixie extension).
            hi = high if sep else lo
        values.update(range(lo, hi + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]          # POSIX: 0=Sunday .. 6=Saturday
    day_restricted: bool              # day-of-month does not start with '*'
    weekday_restricted: bool          # day-of-week does not start with '*'

    def day_matches(self, date: datetime.date) -> bool:
        if date.month not in self.months:
            return False
        dom_ok = date.day in self.days
        # Python weekday(): Monday=0 .. Sunday=6 -> POSIX Sunday=0.
        dow_ok = (date.weekday() + 1) % 7 in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, dt: datetime.datetime) -> bool:
        return dt.minute in self.minutes and dt.hour in self.hours and self.day_matches(dt.date())

    def next_fire(self, after: datetime.datetime) -> datetime.datetime | None:
        """First matching minute strictly after ``after``, or ``None``.

        Walks days, then hours and minutes within a matching day, so a
        monthly or yearly schedule costs a few hundred day checks rather
        than half a million minute checks.
        """
        start = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        day = start.date()
        for _ in range(_SEARCH_DAYS):
            if self.day_matches(day):
                first_day = day == start.date()
                for h in hours:
                    if first_day and h < start.hour:
                        continue
                    for m in minutes:
                        if first_day and h == start.hour and m < start.minute:
                            continue
                        return datetime.datetime.combine(day, datetime.time(h, m))
            day += datetime.timedelta(days=1)
        return None


@lru_cache(maxsize=256)
def parse_cron(expr: str) -> CronSchedule:
    """Parse a 5-field cron expression (or an ``@daily``-style alias)."""
    if not expr or not expr.strip():
        raise CronError("empty cron expression")
    text = _ALIASES.get(expr.strip().lower(), expr.strip())
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields, got {len(parts)}: {expr!r}")
    parsed = [
        parse_field(part, field, low, high, names)
        for part, (field, low, high, names) in zip(parts, _FIELDS)
    ]
    weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
    return CronSchedule(
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=weekdays,
        day_restricted=not parts[2].startswith("*"),
        weekday_restricted=not parts[4].startswith("*"),
    )


def is_valid_cron(expr: str) -> bool:
    try:
        parse_cron(expr)
    except CronError:
        return False
    return True


def cron_matches(expr: str, dt: datetime.datetime) -> bool:
    """True if ``dt`` (to the minute) matches ``expr``; False when invalid."""
    try:
        return parse_cron(expr).matches(dt)
    except CronError:
        return False


def next_cron_fire(expr: str, after: datetime.datetime | None = None) -> datetime.datetime | None:
    """Next fire time strictly after ``after`` (default now); None if invalid or never."""
    try:
        schedule = parse_cron(expr)
    except CronError:
        return None
    return schedule.next_fire(after or datetime.datetime.now())
