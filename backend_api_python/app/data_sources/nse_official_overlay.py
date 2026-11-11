"""NSE Phase 3G: opt-in multi-date official bhavcopy overlay (daily research only).

Supported official UDiFF EQ dates: 2025-03-18 and 2026-11-08.

Research only. Yahoo provides adjusted OHLC; NSE bhavcopy provides raw OHLC.
For March 2025, derive Yahoo's daily price-adjustment factor from valid
adjacent regular sessions and apply that SAME factor to all four official
prices. Reject if Yahoo adjustment factors change across the missing session
or if the source adapter's surrounding adjusted closes disagree with Yahoo.
For the November 2026 special session require factor 1, preserving the
previous, stricter Phase 2D safeguard.

Does not write data, place orders, or download official bhavcopies. This is
NOT comprehensive corporate-action, historical-data, or execution certification.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo
from zipfile import BadZipFile, ZipFile

_IST = ZoneInfo("Asia/Kolkata")
SPECIAL_SESSION = date(2026, 11, 8)
MISSING_REGULAR_SESSION = date(2025, 3, 18)
SESSION = SPECIAL_SESSION  # backwards-compatible Phase 2D default
# Only these two sessions have been explicitly verified by the user against NSE UDiFF.
_APPROVED = {
    MISSING_REGULAR_SESSION: (date(2025, 3, 17), date(2025, 3, 19)),
    SPECIAL_SESSION: (date(2026, 11, 6), date(2026, 11, 9)),
}
_MAX_CSV_BYTES = 16_000_000
_FIELDS = frozenset({"TckrSymb", "SctySrs", "TradDt", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"})
_NSE_FILENAME = re.compile(r"(?:^|/)BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv$", re.I)


class NSEOfficialOverlayError(ValueError):
    """Fail closed if official archive or Yahoo adjustment provenance is unsafe."""


def _epoch(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _parse_official_date(value: str) -> date:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise NSEOfficialOverlayError(f"Unsupported NSE TradDt: {raw!r}")


def read_official_bhavcopy(zip_path: str | Path, *, expected_date: date = SESSION) -> Dict[str, Dict[str, Any]]:
    """Read official NSE UDiFF EQ series; check report date, schema and OHLCV.

    ZIP entries are read, never extracted. The CSV header and filename date
    must match the supplied expected date. Exactly one CSV is allowed.
    """
    if expected_date not in _APPROVED:
        raise NSEOfficialOverlayError(f"Unapproved official session: {expected_date}")
    path = Path(zip_path)
    if not path.is_file():
        raise NSEOfficialOverlayError(f"Official bhavcopy ZIP not found: {path}")
    try:
        with ZipFile(path) as zf:
            files = [x for x in zf.infolist() if not x.is_dir() and x.filename.lower().endswith(".csv")]
            if len(files) != 1:
                raise NSEOfficialOverlayError("Bhavcopy ZIP must contain exactly one CSV")
            item = files[0]
            match = _NSE_FILENAME.search(item.filename)
            if not match or match.group(1) != expected_date.strftime("%Y%m%d"):
                raise NSEOfficialOverlayError("Bhavcopy internal filename does not match report date")
            if item.file_size > _MAX_CSV_BYTES:
                raise NSEOfficialOverlayError("Official bhavcopy CSV is unexpectedly large")
            with zf.open(item) as file:
                data = file.read(_MAX_CSV_BYTES + 1)
            if len(data) > _MAX_CSV_BYTES:
                raise NSEOfficialOverlayError("Official bhavcopy CSV exceeds read limit")
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise NSEOfficialOverlayError(f"Cannot open NSE bhavcopy ZIP: {exc}") from exc
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NSEOfficialOverlayError("Official NSE bhavcopy is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not _FIELDS.issubset(set(reader.fieldnames)):
        raise NSEOfficialOverlayError(f"Unexpected official UDiFF headers: {reader.fieldnames!r}")
    result: Dict[str, Dict[str, Any]] = {}
    for row in reader:
        symbol = str(row.get("TckrSymb") or "").strip().upper()
        series = str(row.get("SctySrs") or "").strip().upper()
        if not symbol or series != "EQ":
            continue
        day = _parse_official_date(row.get("TradDt", ""))
        if day != expected_date:
            raise NSEOfficialOverlayError(f"Bhavcopy EQ row date {day} != {expected_date}")
        if symbol in result:
            raise NSEOfficialOverlayError(f"Duplicate NSE EQ row for {symbol}")
        try:
            op, hi, lo, cl, vol = (float(row[key]) for key in
                                   ("OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"))
        except (ValueError, TypeError, KeyError) as exc:
            raise NSEOfficialOverlayError(f"Invalid NSE EQ numeric values for {symbol}") from exc
        if (not all(math.isfinite(v) for v in (op, hi, lo, cl, vol))
            or min(op, hi, lo, cl) <= 0 or vol <= 0
            or hi < max(op, lo, cl) or lo > min(op, hi, cl)):
            raise NSEOfficialOverlayError(f"Invalid OHLCV for {symbol}")
        result[symbol] = {
            "time": _epoch(day), "open": op, "high": hi,
            "low": lo, "close": cl, "volume": vol,
        }
    if not result:
        raise NSEOfficialOverlayError(f"No NSE EQ rows for {expected_date}")
    return result


def _archive_paths() -> Dict[date, Path]:
    """Explicit multi-ZIP env wins; legacy single-ZIP remains compatible."""
    raw_multi = os.environ.get("NSE_OFFICIAL_BHAVCOPY_ZIPS", "").strip()
    raw_single = os.environ.get("NSE_OFFICIAL_BHAVCOPY_ZIP", "").strip()
    if raw_multi:
        strings = [x.strip() for x in raw_multi.split(";") if x.strip()]
        if not strings:
            raise NSEOfficialOverlayError("No valid NSE archive paths")
    elif raw_single:
        strings = [raw_single]
    else:
        return {}
    if len(strings) > len(_APPROVED):
        raise NSEOfficialOverlayError("Too many NSE official ZIP files configured")
    archives = {}
    for raw in strings:
        path = Path(raw)
        # File date comes from the file's *internal* CSV filename, not from a user-controlled path.
        if not path.is_file():
            raise NSEOfficialOverlayError(f"Official bhavcopy ZIP not found: {path}")
        try:
            with ZipFile(path) as zf:
                files = [x for x in zf.namelist() if x.lower().endswith('.csv')]
                if len(files) != 1:
                    raise NSEOfficialOverlayError(f"Official ZIP must have exactly one CSV: {path}")
                match = _NSE_FILENAME.search(files[0])
        except (BadZipFile, OSError) as exc:
            raise NSEOfficialOverlayError(f"Invalid official ZIP {path}: {exc}") from exc
        if not match:
            raise NSEOfficialOverlayError(f"Unexpected official CSV filename in {path}")
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError as exc:
            raise NSEOfficialOverlayError(f"Invalid report date in {path}") from exc
        if day not in _APPROVED:
            raise NSEOfficialOverlayError(f"Official report date not approved for overlay: {day}")
        if day in archives:
            raise NSEOfficialOverlayError(f"Duplicate official ZIP for {day}")
        archives[day] = path
    return archives


def _find_row(frame, day: date):
    if frame is None or frame.empty:
        raise NSEOfficialOverlayError("No Yahoo raw history for adjustment parity check")
    matches = [idx for idx in frame.index if idx.date() == day]
    if len(matches) != 1:
        raise NSEOfficialOverlayError(f"Expected one Yahoo raw row on {day}; got {len(matches)}")
    return frame.loc[matches[0]]


def _close_equal(a: float, b: float) -> bool:
    return math.isfinite(a) and math.isfinite(b) and abs(a-b) <= max(0.03, abs(a)*0.00005)


def _adjacent_factor(provider, results: Dict[int, Dict[str, Any]], day: date) -> float:
    """Verify Yahoo raw/adjusted price ratio stable across the official gap."""
    previous, following = _APPROVED[day]
    raw_frame = provider.history(
        start=previous.isoformat(), end=(following + timedelta(days=1)).isoformat(),
        interval="1d", auto_adjust=False, actions=False,
    )
    factors = []
    for neighbor in (previous, following):
        yahoo = _find_row(raw_frame, neighbor)
        try:
            raw_close, adjusted_close, volume = (
                float(yahoo["Close"]), float(yahoo["Adj Close"]), float(yahoo["Volume"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NSEOfficialOverlayError(f"Missing Yahoo raw/adjusted close for {neighbor}") from exc
        adapter = results.get(_epoch(neighbor))
        if (raw_close <= 0 or adjusted_close <= 0 or volume <= 0
            or not all(math.isfinite(v) for v in (raw_close, adjusted_close, volume))
            or adapter is None or not _close_equal(adjusted_close, float(adapter['close']))):
            raise NSEOfficialOverlayError(f"Yahoo raw/adjusted or adapter parity failed for {neighbor}")
        factors.append(adjusted_close / raw_close)
    # Do not splice through a split/dividend event or diverging adjustment records.
    if abs(factors[0] - factors[1]) > max(0.00005, max(factors)*0.00005):
        raise NSEOfficialOverlayError(
            f"Yahoo adjustment factor changed across {day}: {factors}; refusing to merge"
        )
    factor = sum(factors) / 2
    if day == SPECIAL_SESSION and abs(factor - 1.0) > 0.00005:
        raise NSEOfficialOverlayError(
            f"Yahoo adjusted/raw close disagreement near {day}: factor={factor:.9f}; "
            "refusing to mix raw November bhavcopy"
        )
    if not 0.1 <= factor <= 2.0:
        raise NSEOfficialOverlayError(f"Suspicious Yahoo adjustment factor near {day}: {factor}")
    return factor


def merge_official_special_candle(
    *, provider, symbol: str, results: Dict[int, Dict[str, Any]],
    start: datetime, end: datetime, before_time, after_time, local_now: datetime,
) -> None:
    """Backward compatible Phase 2D entry point; now merges BOTH approved dates."""
    archives = _archive_paths()
    if not archives:
        return
    raw_symbol = str(symbol or "").strip().upper()
    if raw_symbol.startswith("NSESTOCK:"):
        raw_symbol = raw_symbol.split(":", 1)[1]
    if raw_symbol.endswith(".NS"):
        raw_symbol = raw_symbol[:-3]
    candidates = {}
    for day, path in sorted(archives.items()):
        stamp = _epoch(day)
        if not (start.date() <= day < end.date()):
            continue
        if before_time is not None and stamp >= int(before_time):
            continue
        if after_time is not None and stamp < int(after_time):
            continue
        # Both approved dates have verified 15:30 IST closes; daily EOD only.
        published_after = datetime.combine(day, time(15, 30), tzinfo=_IST) + timedelta(minutes=15)
        if local_now.astimezone(_IST) < published_after:
            continue
        official = read_official_bhavcopy(path, expected_date=day)
        if raw_symbol not in official:
            raise NSEOfficialOverlayError(f"Official NSE EQ record absent: {raw_symbol} on {day}")
        multiplier = _adjacent_factor(provider, results, day)
        raw = official[raw_symbol]
        candidate = {
            **raw,
            **{k: float(raw[k]) * multiplier for k in ("open", "high", "low", "close")},
        }
        if stamp in results:
            existing = results[stamp]
            if any(not _close_equal(float(existing[k]), float(candidate[k]))
                   for k in ("open", "high", "low", "close")) or abs(float(existing['volume'])-float(candidate['volume'])) > 0.01:
                raise NSEOfficialOverlayError(f"Existing Yahoo and NSE official candle conflict on {day}: {raw_symbol}")
        else:
            candidates[stamp] = candidate
    # Atomic per-symbol merge: never insert one official date if another fails.
    results.update(candidates)
