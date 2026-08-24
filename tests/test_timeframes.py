"""Date-range logic -- the part that's easiest to get subtly wrong."""

from datetime import date

import pytest

from app.timeframes import DateRange, resolve_range


# A Sunday, mid-month, mid-year: exercises weekday and boundary maths.
SUNDAY = date(2026, 8, 23)


def test_daily_is_today():
    rng = resolve_range("daily", today=SUNDAY)
    assert rng == DateRange(SUNDAY, SUNDAY)


@pytest.mark.parametrize(
    "today,expected_start",
    [
        (date(2026, 8, 23), date(2026, 8, 17)),  # Sunday -> that Monday
        (date(2026, 8, 24), date(2026, 8, 24)),  # Monday -> itself
        (date(2026, 8, 22), date(2026, 8, 17)),  # Saturday
    ],
)
def test_weekly_runs_from_this_monday_to_today(today, expected_start):
    rng = resolve_range("weekly", today=today)
    assert rng == DateRange(expected_start, today)
    assert rng.start.weekday() == 0  # always a Monday
    assert rng.end == today


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 8, 23), DateRange(date(2026, 8, 1), date(2026, 8, 23))),
        (date(2026, 1, 9), DateRange(date(2026, 1, 1), date(2026, 1, 9))),
        # First of the month is a single-day range, not a whole month
        (date(2026, 5, 1), DateRange(date(2026, 5, 1), date(2026, 5, 1))),
        # Leap day is a valid endpoint
        (date(2024, 2, 29), DateRange(date(2024, 2, 1), date(2024, 2, 29))),
    ],
)
def test_monthly_runs_from_the_first_to_today(today, expected):
    assert resolve_range("monthly", today=today) == expected


@pytest.mark.parametrize(
    "today,expected",
    [
        (SUNDAY, DateRange(date(2026, 1, 1), SUNDAY)),
        # New Year's Day is a single-day range
        (date(2026, 1, 1), DateRange(date(2026, 1, 1), date(2026, 1, 1))),
        (date(2024, 12, 31), DateRange(date(2024, 1, 1), date(2024, 12, 31))),
    ],
)
def test_yearly_runs_from_january_first_to_today(today, expected):
    assert resolve_range("yearly", today=today) == expected


def test_all_periods_end_today_and_start_no_later():
    """Every non-custom window is current, never a past completed period."""
    for tf in ("daily", "weekly", "monthly", "yearly"):
        rng = resolve_range(tf, today=SUNDAY)
        assert rng.end == SUNDAY, tf
        assert rng.start <= SUNDAY, tf


def test_periods_nest_from_narrowest_to_widest():
    d = resolve_range("daily", today=SUNDAY)
    w = resolve_range("weekly", today=SUNDAY)
    m = resolve_range("monthly", today=SUNDAY)
    y = resolve_range("yearly", today=SUNDAY)
    assert y.start <= m.start <= w.start <= d.start


def test_custom_range_passes_through():
    rng = resolve_range(
        "custom", today=SUNDAY, date_from=date(2026, 3, 4), date_to=date(2026, 3, 9)
    )
    assert rng == DateRange(date(2026, 3, 4), date(2026, 3, 9))


def test_custom_single_day_is_allowed():
    day = date(2026, 3, 4)
    assert resolve_range("custom", date_from=day, date_to=day) == DateRange(day, day)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"date_from": date(2026, 3, 4)},  # missing date_to
        {"date_to": date(2026, 3, 4)},  # missing date_from
        {"date_from": date(2026, 3, 9), "date_to": date(2026, 3, 4)},  # reversed
    ],
)
def test_custom_rejects_bad_bounds(kwargs):
    with pytest.raises(ValueError):
        resolve_range("custom", today=SUNDAY, **kwargs)


def test_unknown_timeframe_raises():
    with pytest.raises(ValueError):
        resolve_range("fortnightly", today=SUNDAY)


class TestApiBounds:
    """postedBefore must be exclusive, i.e. the day *after* the inclusive end.

    Bounds are anchored to Pacific midnight (Product Hunt's own day
    boundary), then converted to UTC -- so August dates land on 07:00Z
    (PDT, UTC-7) and January dates land on 08:00Z (PST, UTC-8).
    """

    def test_single_day_spans_exactly_24h(self):
        after, before = DateRange(date(2026, 8, 22), date(2026, 8, 22)).to_api_bounds()
        assert after == "2026-08-22T07:00:00Z"
        assert before == "2026-08-23T07:00:00Z"

    def test_month_end_rolls_into_next_month(self):
        _, before = DateRange(date(2026, 7, 1), date(2026, 7, 31)).to_api_bounds()
        assert before == "2026-08-01T07:00:00Z"

    def test_partial_current_month_covers_through_today(self):
        after, before = DateRange(date(2026, 8, 1), date(2026, 8, 23)).to_api_bounds()
        assert after == "2026-08-01T07:00:00Z"
        assert before == "2026-08-24T07:00:00Z"

    def test_year_end_rolls_into_next_year(self):
        after, before = DateRange(date(2025, 1, 1), date(2025, 12, 31)).to_api_bounds()
        assert after == "2025-01-01T08:00:00Z"
        assert before == "2026-01-01T08:00:00Z"

    def test_dst_spring_forward_transition(self):
        # 2026-03-08 is the US DST start date; Pacific midnight before it is
        # still PST (-8), the day after is PDT (-7).
        after, before = DateRange(date(2026, 3, 8), date(2026, 3, 8)).to_api_bounds()
        assert after == "2026-03-08T08:00:00Z"
        assert before == "2026-03-09T07:00:00Z"
