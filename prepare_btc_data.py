"""Fetch public Binance Spot BTCUSDT 5m monthly data, verify, convert. No inference."""
from pathlib import Path
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import calendar, csv, hashlib, io, json, zipfile

ROOT = Path(__file__).resolve().parent
MONTHS = ('2026-07', '2026-08', '2026-09')
BASE = 'https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/5m/'
DATA = ROOT / 'data' / 'BTCUSDT-spot-5m-2026Q3'
RAW = DATA / 'raw'
RAW.mkdir(parents=True, exist_ok=True)
OUTPUT = DATA / 'BTCUSDT-5m-kronos.csv'
REPORT = DATA / 'validation.json'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
STEP_US = 300_000_000

def download(url, path):
    if path.exists():
        return path.read_bytes(), 'existing_local_file'
    req = Request(url, headers={'User-Agent': 'Kronos-local-data-preparation/1.0'})
    with urlopen(req, timeout=60) as response:
        payload = response.read()
    path.write_bytes(payload)
    return payload, 'downloaded'

def prepare_month(month):
    filename = f'BTCUSDT-5m-{month}.zip'
    archive, origin = download(BASE + filename, RAW / filename)
    checksum_bytes, _ = download(BASE + filename + '.CHECKSUM', RAW / (filename + '.CHECKSUM'))
    checksum_parts = checksum_bytes.decode('utf-8-sig').strip().split()
    expected = checksum_parts[0].lower()
    assert len(expected) == 64 and all(c in '0123456789abcdef' for c in expected), 'Invalid checksum'
    assert checksum_parts[1].lstrip('*') == filename, 'Checksum filename mismatch'
    actual_hash = hashlib.sha256(archive).hexdigest()
    assert actual_hash == expected, 'Archive checksum mismatch: ' + month
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        assert package.namelist() == [filename[:-4] + '.csv'], 'Unexpected ZIP members'
        assert package.testzip() is None, 'ZIP CRC mismatch'
        source_csv = package.read(package.namelist()[0])
    # Preserve uncompressed source next to its verified archive; never extract arbitrary paths.
    (RAW / (filename[:-4] + '.csv')).write_bytes(source_csv)
    year, number = map(int, month.split('-'))
    month_start = datetime(year, number, 1, tzinfo=timezone.utc)
    next_month = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=timezone.utc)
    rows = []
    for line, raw in enumerate(csv.reader(io.StringIO(source_csv.decode('utf-8-sig'))), 1):
        assert len(raw) == 12, f'Invalid field count {month}:{line}'
        opened, closed = int(raw[0]), int(raw[6])
        # Official Spot archives since 2025 use microseconds; verify, do not guess units.
        assert 10**15 <= opened < 10**16, 'Expected microsecond timestamp'
        timestamp = EPOCH + timedelta(microseconds=opened)
        assert month_start <= timestamp < next_month, 'Timestamp outside archive month'
        assert opened % STEP_US == 0 and closed == opened + STEP_US - 1, 'Invalid 5m boundaries'
        assert EPOCH + timedelta(microseconds=closed) < datetime.now(timezone.utc), 'Unclosed candle'
        values = [Decimal(raw[i]) for i in (1, 2, 3, 4, 5, 7)]
        assert all(v.is_finite() for v in values), 'Non-finite value'
        op, hi, lo, cl, volume, amount = values
        assert min(op, hi, lo, cl) > 0 and lo <= min(op, cl) <= max(op, cl) <= hi, 'Invalid OHLC'
        assert volume >= 0 and amount >= 0, 'Negative volume'
        assert int(raw[8]) >= 0, 'Negative trade count'
        # Keep original numeric strings, preserving decimal precision.
        rows.append((opened, [timestamp.strftime('%Y-%m-%d %H:%M:%S')] + [raw[i] for i in (1, 2, 3, 4, 5, 7)]))
    assert rows and all(a[0] < b[0] for a, b in zip(rows, rows[1:])), 'Non-increasing archive'
    return rows, dict(month=month, url=BASE + filename, checksum_url=BASE + filename + '.CHECKSUM',
                      archive_sha256=actual_hash, source_csv_sha256=hashlib.sha256(source_csv).hexdigest(),
                      retrieved_at=datetime.now(timezone.utc).isoformat(), origin=origin,
                      rows=len(rows), expected_rows=calendar.monthrange(year, number)[1] * 288)

with ThreadPoolExecutor(max_workers=3) as pool:
    downloaded = list(pool.map(prepare_month, MONTHS))
rows = [row for month_rows, _ in downloaded for row in month_rows]
assert all(a[0] < b[0] for a, b in zip(rows, rows[1:])), 'Duplicate or out-of-order merged timestamps'
start = datetime(2026, 7, 1, tzinfo=timezone.utc)
end = datetime(2026, 10, 1, tzinfo=timezone.utc)
expected_times = set(range(int((start-EPOCH).total_seconds())*1_000_000,
                           int((end-EPOCH).total_seconds())*1_000_000, STEP_US))
actual_times = {t for t, _ in rows}
missing = sorted(expected_times - actual_times)
assert not actual_times - expected_times, 'Unexpected candle outside requested period'
with OUTPUT.open('w', encoding='utf-8', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['timestamps', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    writer.writerows(row for _, row in rows)
report = dict(status='ready' if not missing else 'gaps_require_review',
              symbol='BTCUSDT', venue='Binance', market='spot', interval='5m', timezone='UTC',
              timestamp_semantics='candle open; naive CSV timestamps represent UTC', source_timestamp_unit='microseconds',
              volume_unit='BTC', amount_unit='USDT', amount_mapping='quote asset volume (source column 8)',
              months=MONTHS, rows=len(rows), expected_rows=len(expected_times),
              first_timestamp=rows[0][1][0], last_timestamp=rows[-1][1][0], duplicate_timestamps=0,
              missing_candles=len(missing), missing_timestamps=[(EPOCH+timedelta(microseconds=t)).isoformat() for t in missing],
              checks=dict(archive_sha256=True, zip_crc=True, finite_numeric_values=True, ohlc_constraints=True,
                          nonnegative_volumes=True, strict_time_order=True, closed_candles=True, five_minute_boundaries=True),
              source_files=[metadata for _, metadata in downloaded],
              output=str(OUTPUT.relative_to(ROOT)), output_sha256=hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
              created_at=datetime.now(timezone.utc).isoformat(),
              transformations=['Converted microsecond candle-open timestamps to UTC strings',
                               'Selected OHLC, base volume and quote asset volume; preserved original decimal strings',
                               'Concatenated monthly files in chronological order; no filling, interpolation or resampling'],
              inference_run=False, training_overlap='unknown')
REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({k: report[k] for k in ('status','rows','expected_rows','first_timestamp','last_timestamp','missing_candles','output','output_sha256')}, indent=2), flush=True)
if missing:
    raise SystemExit('Data has gaps; inspect validation.json before evaluation')
