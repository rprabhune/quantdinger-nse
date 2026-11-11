"""Read-only NSE cash-equity research calendar for verified years 2025–2026.

This is deliberately NOT a broker-execution calendar, not an exchange-calendars
XNSE implementation, and not an open-ended substitute for official circulars.
Only daily OHLCV historical research is in scope. Future years fail closed.
Sources:
 NSE 2025 holiday circular: https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf
 NSE 2025 Muhurat: https://nsearchives.nseindia.com/content/circulars/CMTR70319.pdf
 NSE 2026 holiday list: https://www.nseindia.com/resources/exchange-communication-holidays
 NSE 2026 Muhurat: https://upstox.com/announcements/updates/muhurat-trading-2026-sunday-8-november-2-20-pm-3-30-pm/
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

# Calendar = published NSE cash market holidays, not bank/settlement holidays.
# Muhurat sessions are encoded separately, including the 2025 date that is
# shown as a holiday in the standard trading-holiday table.
_HOLIDAYS = frozenset({
    date.fromisoformat(s) for s in (
        "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
        "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15",
        "2025-08-27", "2025-10-02", "2025-10-22", "2025-11-05",
        "2025-12-25",
        "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
        "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
        "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
        "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
    )
})

# Note: These are non-CAS continuous-equity times. CAS eligibility and
# security-specific closing auctions require a separate instrument feed.
_SPECIAL = {
    date(2025, 10, 21): (time(13, 45), time(14, 45)),
    date(2026, 11, 8): (time(14, 20), time(15, 30)),
}


class NSEResearchCalendar:
    """Minimal exchange-calendar-compatible adapter for daily NSE research."""

    tz = IST
    supported_years = (2025, 2026)

    @staticmethod
    def _day(value) -> date:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            raise ValueError("NSE calendar date cannot be NaT")
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(IST)
        return stamp.date()

    @classmethod
    def _require_year(cls, day: date) -> None:
        if day.year not in cls.supported_years:
            raise ValueError(
                f"NSE research calendar not validated for {day.year}; "
                "update official exchange holiday/special-session data first"
            )

    def is_session(self, session) -> bool:
        day = self._day(session)
        self._require_year(day)
        if day in _SPECIAL:
            return True
        return day.weekday() < 5 and day not in _HOLIDAYS

    def sessions_in_range(self, start, end) -> pd.DatetimeIndex:
        first, last = self._day(start), self._day(end)
        self._require_year(first)
        self._require_year(last)
        if first > last:
            return pd.DatetimeIndex([], tz="UTC")
        # Session labels at UTC midnight, consistent with normalized daily OHLCV.
        return pd.DatetimeIndex(
            [pd.Timestamp(d, tz="UTC") for d in pd.date_range(first, last, freq="D").date
             if self.is_session(d)]
        )

    def session_open(self, session) -> pd.Timestamp:
        return self._session_boundary(session, which=0)

    def session_close(self, session) -> pd.Timestamp:
        return self._session_boundary(session, which=1)

    def _session_boundary(self, session, which: int) -> pd.Timestamp:
        day = self._day(session)
        if not self.is_session(day):
            raise ValueError(f"Not an NSE equity session: {day}")
        start, end = _SPECIAL.get(day, (time(9, 15), time(15, 30)))
        local_time = (start, end)[which]
        return pd.Timestamp(datetime.combine(day, local_time, tzinfo=IST)).tz_convert("UTC")

    def session_break_start(self, session):
        return pd.NaT

    def session_break_end(self, session):
        return pd.NaT

    def previous_session(self, session) -> pd.Timestamp:
        candidate = self._day(session) - timedelta(days=1)
        while True:
            self._require_year(candidate)
            if self.is_session(candidate):
                return pd.Timestamp(candidate, tz="UTC")
            candidate -= timedelta(days=1)
