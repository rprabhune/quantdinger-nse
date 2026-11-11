"""Pure offline tests for Phase 3G multi-bhavcopy adjusted-price merge."""
from __future__ import annotations
import csv
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd
from app.data_sources.nse_official_overlay import (
    NSEOfficialOverlayError, read_official_bhavcopy,
    merge_official_special_candle,
)

_UTC = timezone.utc
_HEAD = ["TckrSymb", "SctySrs", "TradDt", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"]


def make_zip(path, day, *, bad_filename=False, duplicate=False, volume=15745877):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEAD)
    row = ['RELIANCE','EQ', day, '1244.70','1248.35','1235.00','1238.80',volume] if day == '2025-03-18' else [
        'RELIANCE','EQ',day,'1322.10','1324.90','1314.70','1324.00','4174389']
    w.writerow(row)
    if duplicate:
        w.writerow(row)
    csvday = '20250319' if bad_filename else day.replace('-','')
    with ZipFile(path,'w',ZIP_DEFLATED) as f:
        f.writestr(f'BhavCopy_NSE_CM_0_0_0_{csvday}_F_0000.csv',buf.getvalue())


def epoch(d):
    return int(datetime.fromisoformat(d).replace(tzinfo=_UTC).timestamp())


class FakeProvider:
    def __init__(self, march_factor=0.99, november_factor=1.0, march_factor_second=None):
        self.march_factor = march_factor
        self.november_factor = november_factor
        self.march_factor_second = march_factor if march_factor_second is None else march_factor_second
        self.history_calls = []

    def history(self, **kw):
        self.history_calls.append(kw)
        if kw.get('auto_adjust') is not False:
            raise ValueError('Only raw parity probes should happen here')
        if kw['start'].startswith('2025'):
            rows = [
                {'Close':1240., 'Adj Close':1240*self.march_factor, 'Volume':100000},
                {'Close':1248., 'Adj Close':1248*self.march_factor_second, 'Volume':100000},
            ]
            days = ['2025-03-17','2025-03-19']
        else:
            rows = [
                {'Close':1320., 'Adj Close':1320*self.november_factor, 'Volume':100000},
                {'Close':1324.4, 'Adj Close':1324.4*self.november_factor, 'Volume':100000},
            ]
            days = ['2026-11-06','2026-11-09']
        return pd.DataFrame(rows,index=pd.DatetimeIndex(days,tz='Asia/Kolkata'))


class MultiArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.march = Path(self.tmp.name)/'nse_20250318.zip'
        self.nov = Path(self.tmp.name)/'nse_20261108.zip'
        make_zip(self.march,'2025-03-18')
        make_zip(self.nov,'2026-11-08')
        self.provider = FakeProvider()
        self.results = {
            epoch('2025-03-17'):{'time':epoch('2025-03-17'),'close':1240*.99,'volume':100000},
            epoch('2025-03-19'):{'time':epoch('2025-03-19'),'close':1248*.99,'volume':100000},
            epoch('2026-11-06'):{'time':epoch('2026-11-06'),'close':1320.,'volume':100000},
            epoch('2026-11-09'):{'time':epoch('2026-11-09'),'close':1324.4,'volume':100000},
        }
        self.env = {'NSE_OFFICIAL_BHAVCOPY_ZIPS':f'{self.march};{self.nov}',
                    'NSE_OFFICIAL_BHAVCOPY_ZIP':''}

    def merge(self, env=None, **kw):
        with patch.dict(os.environ, self.env if env is None else env):
            merge_official_special_candle(
                provider=self.provider,symbol=kw.get('symbol','RELIANCE'),results=self.results,
                start=datetime(2025,1,1,tzinfo=_UTC),end=datetime(2026,11,11,tzinfo=_UTC),
                before_time=kw.get('before_time'),after_time=kw.get('after_time'),
                local_now=datetime(2026,11,11,tzinfo=_UTC),
            )

    def test_multi_date_import_with_adjustment(self):
        self.merge()
        m = self.results[epoch('2025-03-18')]
        n = self.results[epoch('2026-11-08')]
        self.assertAlmostEqual(m['close'],1238.80*0.99)
        self.assertAlmostEqual(m['open'],1244.70*0.99)
        self.assertEqual(m['volume'],15745877)
        self.assertAlmostEqual(n['close'],1324.0)
        self.assertEqual(n['volume'],4174389)
        self.assertEqual(len(self.provider.history_calls),2)

    def test_legacy_single_archive_still_imports_nov(self):
        self.merge(env={'NSE_OFFICIAL_BHAVCOPY_ZIPS':'','NSE_OFFICIAL_BHAVCOPY_ZIP':str(self.nov)})
        self.assertNotIn(epoch('2025-03-18'),self.results)
        self.assertIn(epoch('2026-11-08'),self.results)

    def test_unset_env_no_overlay(self):
        self.merge(env={'NSE_OFFICIAL_BHAVCOPY_ZIPS':'','NSE_OFFICIAL_BHAVCOPY_ZIP':''})
        self.assertEqual(len(self.results),4)
        self.assertEqual(len(self.provider.history_calls),0)

    def test_factor_discontinuity_refused_atomic(self):
        self.provider.march_factor_second=0.975
        self.results[epoch('2025-03-19')]['close']=1248*.975
        with self.assertRaisesRegex(NSEOfficialOverlayError,'factor changed'):
            self.merge()
        self.assertEqual(len(self.results),4)

    def test_november_requires_raw_adjusted_parity(self):
        self.provider.november_factor=.97
        self.results[epoch('2026-11-06')]['close']=1320*.97
        self.results[epoch('2026-11-09')]['close']=1324.4*.97
        with self.assertRaisesRegex(NSEOfficialOverlayError,'disagreement'):
            self.merge()
        self.assertEqual(len(self.results),4)

    def test_march_adjacent_candle_mismatch_refused(self):
        self.results[epoch('2025-03-17')]['close']+=3
        with self.assertRaisesRegex(NSEOfficialOverlayError,'parity failed'):
            self.merge()
        self.assertEqual(len(self.results),4)

    def test_duplicate_date_archive_rejected(self):
        with self.assertRaisesRegex(NSEOfficialOverlayError,'Duplicate official'):
            self.merge(env={'NSE_OFFICIAL_BHAVCOPY_ZIPS':f'{self.march};{self.march}',
                            'NSE_OFFICIAL_BHAVCOPY_ZIP':''})

    def test_bad_internal_date_refused(self):
        make_zip(self.march,'2025-03-18',bad_filename=True)
        with self.assertRaises(NSEOfficialOverlayError):
            self.merge()

    def test_duplicate_symbol_refused(self):
        make_zip(self.march,'2025-03-18',duplicate=True)
        with self.assertRaisesRegex(NSEOfficialOverlayError,'Duplicate'):
            self.merge()

    def test_zero_volume_refused(self):
        make_zip(self.march,'2025-03-18',volume=0)
        with self.assertRaisesRegex(NSEOfficialOverlayError,'Invalid OHLCV'):
            self.merge()

    def test_before_time_excludes_later_sessions(self):
        self.merge(before_time=epoch('2025-03-18'))
        self.assertNotIn(epoch('2025-03-18'),self.results)
        self.assertNotIn(epoch('2026-11-08'),self.results)

    def test_wrong_symbol_fails_atomically(self):
        with self.assertRaisesRegex(NSEOfficialOverlayError,'record absent'):
            self.merge(symbol='SBIN')
        self.assertEqual(len(self.results),4)

    def test_approved_report_raw_read(self):
        self.assertEqual(read_official_bhavcopy(self.march,expected_date=date(2025,3,18))['RELIANCE']['close'],1238.8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
