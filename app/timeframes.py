"""Date-range resolution for the daily/weekly/monthly/yearly modes.

Ranges are inclusive of both endpoints and expressed in dates that follow
Product Hunt's own leaderboard day, which turns over at 00:01 **Pacific**
time (not UTC midnight) -- see the ``PACIFIC`` zone below. The Product Hunt
API wants ISO-8601 datetimes for postedAfter/postedBefore, so
:func:`to_api_bounds` widens an inclusive Pacific date range into a
half-open UTC datetime range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Product Hunt's leaderboard day starts at 00:01 Pacific, not UTC midnight.
# Using the IANA zone (rather than a fixed UTC-7/-8 offset) means DST
# transitions are handled automatically.
PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive

    def to_api_bounds(self) -> tuple[str, str]:
        """Return (postedAfter, postedBefore) as ISO-8601 UTC timestamps.

        Both edges are anchored to Pacific midnight (matching Product
        Hunt's own day boundary) and then converted to UTC.
        """
        after = datetime.combine(self.start, time.min, tzinfo=PACIFIC).astimezone(timezone.utc)
        before = datetime.combine(
            self.end + timedelta(days=1), time.min, tzinfo=PACIFIC
        ).astimezone(timezone.utc)
        return (
            after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def pacific_today() -> date:
    """Today's date in Product Hunt's own Pacific-time day boundary."""
    return datetime.now(PACIFIC).date()


def resolve_range(
    timeframe: str,
    today: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> DateRange:
    """Resolve a timeframe keyword into a concrete inclusive date range.

    All periods track the *current* window, matching what producthunt.com's
    leaderboard shows for "this day / week / month / year":

    daily   -> today
    weekly  -> this week so far (Monday through today)
    monthly -> this month so far
    yearly  -> this year so far
    custom  -> the caller-supplied bounds

    Ranks inside an unfinished window are provisional and will move until it
    closes; that's the same behaviour as the site itself.
    """
    today = today or pacific_today()
    tf = (timeframe or "").lower()

    if tf == "custom":
        if not date_from or not date_to:
            raise ValueError("custom timeframe requires date_from and date_to")
        if date_from > date_to:
            raise ValueError("date_from must be on or before date_to")
        return DateRange(date_from, date_to)

    if tf == "daily":
        return DateRange(today, today)

    if tf == "weekly":
        # Monday of the current week through today.
        return DateRange(today - timedelta(days=today.weekday()), today)

    if tf == "monthly":
        return DateRange(today.replace(day=1), today)

    if tf == "yearly":
        return DateRange(date(today.year, 1, 1), today)

    raise ValueError(f"unknown timeframe: {timeframe!r}")


def label_for(timeframe: str, rng: DateRange) -> str:
    """Human-readable label for the resolved range, shown in the UI."""
    if rng.start == rng.end:
        return rng.start.strftime("%b %-d, %Y") if _supports_dash() else rng.start.strftime("%b %d, %Y")
    if rng.start.year == rng.end.year:
        return f"{rng.start.strftime('%b %d')} – {rng.end.strftime('%b %d, %Y')}"
    return f"{rng.start.strftime('%b %d, %Y')} – {rng.end.strftime('%b %d, %Y')}"


def _supports_dash() -> bool:
    try:
        date(2020, 1, 5).strftime("%-d")
        return True
    except ValueError:
        return False
