"""Phase 3K.1: offline unit tests for pinned future share actions and fail-closed overlay."""
from __future__ import annotations
import csv
import io
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
import pandas as pd

from app.data_sources.nse_official_overlay import (
    NSEOfficialOverlayError, _epoch, _post_action_share_multiplier,
    _validate_close_continuity, merge_official_special_candle,
)

UTC = timezone.utc
HEADER = ['TckrSymb','SctySrs','TradDt','OpnPric','HghPric','LwPric','ClsPric','TtlTradgVol']


def make_archive(path, day, ticker, raw_close, raw_volume):
    csv_text = io.StringIO()
    writer = csv.writer(csv_text)
    writer.writerow(HEADER)
    writer.writerow([ticker, 'EQ', day, raw_close * .995, raw_close * 1.01,
                     raw_close * .985, raw_close, raw_volume])
    with ZipFile(path, 'w', ZIP_DEFLATED) as z:
        z.writestr(f'BhavCopy_NSE_CM_0_0_0_{day.replace("-", "")}_F_0000.csv',csv_text.getvalue())


class FakeYahoo:
    def __init__(self, first_adj, last_adj, div_factor, first_volume=18_000_000, last_volume=16_000_000):
        self.first_adj, self.last_adj = first_adj, last_adj
        self.div_factor = div_factor
        self.first_volume, self.last_volume = first_volume, last_volume

    def history(self, **kwargs):
        assert kwargs['auto_adjust'] is False
        assert kwargs['actions'] is False
        assert kwargs['interval'] == '1d'
        if kwargs['start'].startswith('2025'):
            days=['2025-03-17', '2025-03-19']
        else:
            days=['2026-11-06', '2026-11-09']
        return pd.DataFrame([
            {'Close': self.first_adj/self.div_factor, 'Adj Close': self.first_adj,
             'Volume': self.first_volume},
            {'Close': self.last_adj/self.div_factor, 'Adj Close': self.last_adj,
             'Volume': self.last_volume},
        ],index=pd.DatetimeIndex(days,tz='Asia/Kolkata'))


class NormalizationTests(unittest.TestCase):
    def test_exact_pinned_actions_only(self):
        self.assertEqual(_post_action_share_multiplier('HDFCBANK',date(2025,3,18)),2.0)
        self.assertEqual(_post_action_share_multiplier('KOTAKBANK',date(2025,3,18)),5.0)
        self.assertEqual(_post_action_share_multiplier('RELIANCE',date(2025,3,18)),1.0)
        self.assertEqual(_post_action_share_multiplier('KOTAKBANK',date(2026,11,8)),1.0)
        self.assertEqual(_post_action_share_multiplier('HDFCBANK',date(2026,11,8)),1.0)
        self.assertEqual(_post_action_share_multiplier('KOTAKBANK',date(2026,1,14)),1.0)

    def merged(self, ticker, raw_close, raw_volume, prev_close, next_close, dividend_factor=.99, day='2025-03-18'):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'archive.zip'
            make_archive(archive,day,ticker,raw_close,raw_volume)
            left,right = (('2025-03-17','2025-03-19') if day.startswith('2025')
                          else ('2026-11-06','2026-11-09'))
            results={_epoch(date.fromisoformat(left)): {'close':prev_close,'volume':18_000_000},
                     _epoch(date.fromisoformat(right)):{'close':next_close,'volume':16_000_000}}
            provider=FakeYahoo(prev_close,next_close,dividend_factor)
            with patch.dict('os.environ',{'NSE_OFFICIAL_BHAVCOPY_ZIPS':str(archive),
                                          'NSE_OFFICIAL_BHAVCOPY_ZIP':''}):
                merge_official_special_candle(
                    provider=provider, symbol=ticker, results=results,
                    start=datetime(2025,1,1,tzinfo=UTC),
                    end=datetime(2026,11,11,tzinfo=UTC),
                    before_time=None,after_time=None,
                    local_now=datetime(2026,11,11,tzinfo=UTC)
                )
            return results[_epoch(date.fromisoformat(day))]

    def test_hdfc_bonus_price_and_volume(self):
        row=self.merged('HDFCBANK',1698.0,10_953_589,830.0656,846.4204,
                        dividend_factor=.99015)
        self.assertAlmostEqual(row['close'],1698.0*.99015/2)
        self.assertEqual(row['volume'],21_907_178)

    def test_kotak_split_price_and_volume(self):
        row=self.merged('KOTAKBANK',2050.0,5_384_288,397.4747,403.1484,
                        dividend_factor=.985)
        self.assertAlmostEqual(row['close'],2050.0*.985/5)
        self.assertEqual(row['volume'],26_921_440)

    def test_no_share_action_keeps_original_volume(self):
        row=self.merged('RELIANCE',1238.80,15_745_877,1228.2428,1236.4719,
                        dividend_factor=.99143794)
        self.assertAlmostEqual(row['close'],1238.80*.99143794)
        self.assertEqual(row['volume'],15_745_877)

    def test_november_bhavcopy_not_double_adjusted(self):
        row=self.merged('HDFCBANK',990.,3000000,989.,991.,
                        dividend_factor=1.,day='2026-11-08')
        self.assertEqual(row['close'],990.)
        self.assertEqual(row['volume'],3000000)

    def test_unknown_split_like_spike_fails_closed(self):
        with self.assertRaisesRegex(NSEOfficialOverlayError,'discontinuity'):
            self.merged('UNKNOWN',2028.,5_384_288,397.47,403.15,dividend_factor=.985)

    def test_bad_dividend_parity_fails_closed(self):
        with self.assertRaisesRegex(NSEOfficialOverlayError,'disagreement'):
            self.merged('RELIANCE',1238.80,15745877,1228.,1236.,dividend_factor=.96,day='2026-11-08')

    def test_continuity_tolerance_at_25_percent(self):
        ref={_epoch(date(2025,3,17)):{'close':100.},
             _epoch(date(2025,3,19)):{'close':100.}}
        _validate_close_continuity(ref,date(2025,3,18),'XYZ',{'close':125.})
        with self.assertRaisesRegex(NSEOfficialOverlayError,'discontinuity'):
            _validate_close_continuity(ref,date(2025,3,18),'XYZ',{'close':126.})


if __name__=='__main__':
    unittest.main(verbosity=2)
