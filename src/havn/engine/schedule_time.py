"""Cron fire times in a time zone, with defined behaviour across DST changes.

The scheduler keeps its clocks as aware UTC instants and evaluates each cron
expression in the schedule's zone: an IANA name (``Europe/Oslo``) loaded with
``zoneinfo``, or ``None`` for the server's local time (the default, so
existing projects keep their behaviour).

Two wall-clock cases need a rule:

- **A fire time that does not exist** (it falls in the hour skipped when
  clocks go forward, e.g. 02:30 on the last Sunday of March in Europe/Oslo)
  runs at the first minute that does exist after the gap (03:00). Several
  fire times inside one gap ("*/15") collapse into that one run.
- **A fire time that occurs twice** (the hour repeated when clocks go back)
  fires once, at its first occurrence. A schedule that fires every hour or
  more often therefore does not fire during the repeated hour.

``engine.cron`` stays naive: it answers "the next matching wall-clock minute
after this one", and this module turns wall-clock minutes into instants.
"""

from __future__ import annotations

import datetime

UTC = datetime.timezone.utc

# Longest DST gap in the tz database is two hours (and a few historical
# oddities); a day of minutes is a safe bound for the walk out of a gap.
_MAX_GAP_MINUTES = 24 * 60
# Bound on the walk past fire times already behind us (the repeated hour has
# at most 60 minutes of them).
_MAX_SKIPS = 1500


class ScheduleTimezoneError(ValueError):
    """A time zone name that zoneinfo cannot load."""


def load_zone(name: str | None):
    """The ``ZoneInfo`` for ``name``; ``None`` for server local time.

    ``None``, an empty string and ``"local"`` all mean server local time.
    Anything else must be an IANA name; an unknown one raises
    :class:`ScheduleTimezoneError` rather than falling back to local time.
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text or text.lower() == "local":
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        hint = "use an IANA name such as Europe/Oslo, America/New_York or UTC"
        try:
            ZoneInfo("UTC")
        except Exception:
            hint = "no time zone database is installed; run `pip install tzdata`"
        raise ScheduleTimezoneError(f"unknown time zone {text!r} ({hint})") from None


def zone_label(tz) -> str:
    """How a zone is shown to users: its IANA key, or ``local``."""
    if tz is None:
        return "local"
    return getattr(tz, "key", None) or str(tz)


def to_local(instant: datetime.datetime, tz) -> datetime.datetime:
    """Naive wall-clock time of ``instant`` in ``tz`` (``None`` = server local)."""
    return instant.astimezone(tz).replace(tzinfo=None)


def to_instant(wall: datetime.datetime, tz) -> datetime.datetime:
    """The aware UTC instant of naive wall-clock time ``wall`` in ``tz``.

    An ambiguous time (it occurs twice) resolves to its first occurrence. A
    time that does not exist resolves to the first existing minute after
    the gap. ``tz=None`` uses the server's local time zone.
    """
    wall = wall.replace(tzinfo=None)

    def convert(w: datetime.datetime) -> datetime.datetime:
        w = w.replace(fold=0)
        return (w.replace(tzinfo=tz) if tz is not None else w).astimezone(UTC)

    aware = convert(wall)
    # A round trip that lands elsewhere means the wall time does not exist.
    # (Naive datetimes differing only in fold compare equal.)
    if to_local(aware, tz) == wall:
        return aware
    probe = wall.replace(second=0, microsecond=0)
    for _ in range(_MAX_GAP_MINUTES):
        probe += datetime.timedelta(minutes=1)
        candidate = convert(probe)
        if to_local(candidate, tz) == probe:
            return candidate
    return aware


def as_instant(value: datetime.datetime) -> datetime.datetime:
    """An aware UTC instant; a naive value is read as server local time."""
    if value.tzinfo is None:
        return value.astimezone(UTC)
    return value.astimezone(UTC)


def next_fire(expr: str, after: datetime.datetime, tz) -> datetime.datetime | None:
    """The first fire of cron ``expr`` in ``tz`` strictly after instant ``after``.

    Returns an aware UTC instant, or ``None`` for an invalid expression or
    one that never fires.
    """
    from havn.engine.cron import next_cron_fire

    after = as_instant(after)
    wall = to_local(after, tz)
    for _ in range(_MAX_SKIPS):
        candidate = next_cron_fire(expr, wall)
        if candidate is None:
            return None
        instant = to_instant(candidate, tz)
        if instant > after:
            return instant
        # Behind us: the second pass through a repeated hour, or a gap time
        # that resolved to a minute we have already passed.
        wall = candidate
    return None


# With a ``limit``, fires older than the newest ``limit`` are only counted,
# and only up to this many: the count is for a log line, and walking every
# minute of a year-long outage (``* * * * *``) took seconds.
FIRES_COUNT_CAP = 10_000


def _walk(expr: str, start: datetime.datetime, end: datetime.datetime, tz,
          cap: int | None = None) -> list[datetime.datetime]:
    """Fires in ``(start, end]`` oldest first; at most ``cap`` of them."""
    out: list[datetime.datetime] = []
    cursor = start
    while cap is None or len(out) < cap:
        nxt = next_fire(expr, cursor, tz)
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        cursor = nxt
    return out


def fires_between(
    expr: str, start: datetime.datetime, end: datetime.datetime, tz, *, limit: int | None = None,
    count_cap: int = FIRES_COUNT_CAP,
) -> tuple[list[datetime.datetime], int]:
    """Fire instants in ``(start, end]``, oldest first, and how many there were.

    With ``limit``, only the newest ``limit`` fires are returned, and the
    work is bounded: they are found by walking a window that grows back
    from ``end`` (doubling) until it holds ``limit`` fires, and the older
    fires are counted up to ``count_cap`` only. The count is then exact
    when it is at most ``limit + count_cap``, and a lower bound beyond.
    """
    start = as_instant(start)
    end = as_instant(end)
    if end <= start:
        return [], 0
    if limit is None:
        out = _walk(expr, start, end, tz)
        return out, len(out)
    limit = max(0, int(limit))
    span = datetime.timedelta(hours=1)
    while True:
        lo = max(start, end - span)
        recent = _walk(expr, lo, end, tz)
        if len(recent) >= limit or lo == start:
            break
        span *= 2
    newest = recent[-limit:] if limit else []
    older = len(recent) - len(newest)
    if lo > start:
        older += len(_walk(expr, start, lo, tz, cap=count_cap))
    return newest, len(newest) + older
