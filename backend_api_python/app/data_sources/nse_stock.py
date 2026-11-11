"""Read-only, daily NSE equity candle adapter for QuantDinger.

Research/prototype only. Uses Yahoo Finance's unofficial endpoint via yfinance.
It does not provide broker quotes or order execution. For historic backtests,
validate adjustments, holidays, symbol changes, and data provenance separately.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.data_sources.base import BaseDataSource
from app.services.nse_research_calendar import NSEResearchCalendar
from app.data_sources.nse_official_overlay import merge_official_special_candle

_IST = ZoneInfo("Asia/Kolkata")
_NSE_SYMBOL = re.compile(r"[A-Z0-9&_-]{1,30}")


class NSEStockDataSource(BaseDataSource):
    name = "NSEStock/yfinance-daily-research"

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if raw.startswith("NSESTOCK:"):
            raw = raw.split(":", 1)[1]
        if raw.endswith(".NS"):
            raw = raw[:-3]
        if not _NSE_SYMBOL.fullmatch(raw):
            raise ValueError(f"Invalid NSE symbol: {symbol!r}")
        return f"{raw}.NS"

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if str(timeframe).lower() != "1d":
            raise ValueError("NSE POC only supports daily ('1D') candles")
        if not 1 <= int(limit) <= 3000:
            raise ValueError("limit must be between 1 and 3000")
        if before_time is not None and after_time is not None and int(after_time) >= int(before_time):
            return []

        import yfinance as yf  # already a backend requirement in upstream QuantDinger

        now = datetime.now(timezone.utc)
        end = (datetime.fromtimestamp(int(before_time), timezone.utc) if before_time is not None else now) + timedelta(days=2)
        # Equity candles are not present every calendar day. Overfetch for weekends/holidays.
        if after_time is not None:
            start = datetime.fromtimestamp(int(after_time), timezone.utc) - timedelta(days=10)
        else:
            start = end - timedelta(days=max(30, int(limit * 2.2) + 15))

        provider = yf.Ticker(self._provider_symbol(symbol))
        history = provider.history(
            start=start.date().isoformat(),
            end=end.date().isoformat(),
            interval="1d",
            auto_adjust=True,
            actions=False,
        )
        if history is None or history.empty:
            return []

        local_now = datetime.now(_IST)
        nse_calendar = NSEResearchCalendar()  # NSE_PHASE2C_QUALITY_FILTER
        results: dict[int, Dict[str, Any]] = {}
        for index, record in history.iterrows():
            # Yahoo daily candle index is an NSE session-date label. Treat as a
            # date (not an exchange opening timestamp), stored at UTC midnight.
            session_date = index.date()
            # Research is validated for 2025–2026 only; unsupported years raise.
            # A Yahoo holiday placeholder is not a real NSE trading bar.
            if not nse_calendar.is_session(session_date):
                continue
            # No still-forming today's daily candle; allow a publication buffer.
            if session_date > local_now.date() or (
                session_date == local_now.date() and local_now.time() < time(16, 0)
            ):
                continue
            epoch = int(datetime.combine(session_date, time.min, tzinfo=timezone.utc).timestamp())
            if before_time is not None and epoch >= int(before_time):
                continue
            if after_time is not None and epoch < int(after_time):
                continue
            try:
                o, h, l, c, v = (float(record[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(x) for x in (o, h, l, c, v)):
                continue
            # A zero-volume row is an unusable daily stock trading candle,
            # often a forward-filled Yahoo holiday placeholder.
            if v <= 0:
                continue
            if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
                continue
            results[epoch] = self.format_kline(epoch, o, h, l, c, v)

        # NSE_PHASE2D_OFFICIAL_OVERLAY: opt-in official special session,
        # guarded by raw/adjusted-price parity checks. No orders.
        merge_official_special_candle(
            provider=provider, symbol=symbol, results=results,
            start=start, end=end, before_time=before_time,
            after_time=after_time, local_now=local_now,
        )
        ordered = [results[t] for t in sorted(results)]
        # For a fixed historical window, preserve all rows (up to the requested
        # safety bound) instead of truncating left-edge warmup bars silently.
        return ordered if after_time is not None else ordered[-int(limit):]

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError("NSE adapter is historical-data-only; no live quotes or orders")
