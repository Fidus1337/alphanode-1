"""Binance klines/funding with a geo-block fallback: fapi first, data.binance.vision second.

fapi.binance.com returns HTTP 451 to US IPs, but data.binance.vision — Binance's own public
market-data archive (plain S3/CloudFront, no keys) — serves the SAME bars worldwide as zipped
CSVs: monthly files for finished months, daily files for the current one. Same symbols, same
candles, same funding events, one data lineage — the archive just lags live by ~10-30 hours
(daily files land ~10h after the UTC day closes; fundingRate exists as monthly files only).

`fetch_rows()` is a drop-in for the /fapi/v1/klines paging loop: it returns rows in the exact
fapi shape (12 fields, ms timestamps, prices as strings), probing fapi once per process and
switching to the archive when fapi is unreachable or geo-blocked. Stdlib only — this module is
also inlined into generated paper-trade bundles.
"""
import io
import csv
import json
import time
import zipfile
import calendar
import urllib.error
import urllib.request
from datetime import datetime, timezone

FAPI = 'https://fapi.binance.com'
VISION = 'https://data.binance.vision/data/futures/um'
GEO_CODES = (451, 403)                                 # how the fapi edge says "not your region"

_MODE: dict = {'fapi': None}                           # None = not probed | True | False (this process)


def _now_utc():
    return datetime.now(timezone.utc)


def _http_get(url, timeout=30, retries=3):
    """GET with small retries. Returns bytes; None on HTTP 404; raises on anything persistent."""
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == retries - 1:
                raise
        except (TimeoutError, OSError):                # URLError ⊂ OSError; also covers a
            if i == retries - 1:                       # connection reset mid-body-read
                raise
        time.sleep(1.2 * (i + 1))


def fapi_reachable(timeout=6):
    """One probe per process: can we talk to fapi from this network at all?"""
    if _MODE['fapi'] is None:
        try:
            raw = _http_get(f'{FAPI}/fapi/v1/time', timeout=timeout, retries=1)
            _MODE['fapi'] = raw is not None and b'serverTime' in raw
        except Exception:                              # noqa: BLE001 — 451, DNS, timeout: all mean "no"
            _MODE['fapi'] = False
    return _MODE['fapi']


def active_source():
    return 'binance-fapi' if _MODE['fapi'] else 'binance-vision'


def _zip_csv_rows(url):
    """Rows of the single CSV inside a Vision zip; None if the file does not exist (404).
    Futures CSVs may or may not carry a header line — sniff the first field."""
    raw = _http_get(url)
    if raw is None:
        return None
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            rows = list(csv.reader(io.TextIOWrapper(f, encoding='utf-8')))
    if rows and rows[0] and not rows[0][0].strip().isdigit():
        rows = rows[1:]                                # header row
    return rows


def _months(start_dt, end_dt):
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _kline_row(c):
    """CSV kline -> fapi-shaped row: ints for the two times, strings elsewhere (fapi sends
    prices as strings too; every consumer casts to float itself)."""
    return [int(c[0]), c[1], c[2], c[3], c[4], c[5], int(c[6]),
            c[7], int(c[8] or 0), c[9], c[10], c[11] if len(c) > 11 else '0']


def vision_rows(symbol, start_ms, end_ms, interval='1d'):
    """Klines from the Vision archive, fapi-shaped. Finished months come as monthly zips
    (downloaded in parallel — a 1h warm-up spans dozens of months); the current month (and the
    previous one while its monthly file is still being published, 1-2 days after month end) is
    assembled from daily zips."""
    from concurrent.futures import ThreadPoolExecutor
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(max(start_ms, end_ms) / 1000, tz=timezone.utc)
    now = _now_utc()
    months = list(_months(start_dt, end_dt))

    def month_rows(ym):
        y, m = ym
        cur_month = (y, m) == (now.year, now.month)
        rows = None
        if not cur_month:
            rows = _zip_csv_rows(f'{VISION}/monthly/klines/{symbol}/{interval}/'
                                 f'{symbol}-{interval}-{y:04d}-{m:02d}.zip')
        if rows is None:
            # current month, or last month's monthly file not published yet -> daily files
            if not cur_month and (now.year, now.month) != (y + (m == 12), m % 12 + 1):
                return []                              # a genuinely old gap: pre-listing month
            # clamp to today ONLY for the current month — a finished month whose monthly zip
            # is still being published (1-2 days after month end) needs ALL of its days, or
            # early-of-month runs would leave a month-sized hole mid-series
            eom = calendar.monthrange(y, m)[1]
            last_day = min(now.day, eom) if cur_month else eom
            days = list(range(1, last_day + 1))
            with ThreadPoolExecutor(max_workers=8) as dpool:
                daily = list(dpool.map(
                    lambda d: _zip_csv_rows(f'{VISION}/daily/klines/{symbol}/{interval}/'
                                            f'{symbol}-{interval}-{y:04d}-{m:02d}-{d:02d}.zip'),
                    days))
            rows = []
            seen = False
            for day in daily:
                if day is None:
                    if seen:
                        break                          # first hole AFTER data: the archive tail
                    continue                           # leading hole: listed mid-month
                seen = True
                rows.extend(day)
        return rows

    with ThreadPoolExecutor(max_workers=8) as pool:
        per_month = list(pool.map(month_rows, months))
    out = [_kline_row(c) for rows in per_month for c in rows]
    # inclusive end bound: /fapi/v1/klines treats endTime as inclusive on open time
    return [r for r in out if start_ms <= r[0] <= end_ms]


def vision_funding(symbol, start_ms, end_ms):
    """Funding events [(calc_time_ms, rate), ...] from monthly Vision files. The archive has no
    daily fundingRate files, so the current month is missing until ~1-2 days after month end —
    callers should treat the tail as "not yet known", not as zero-funding truth."""
    out = []
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(max(start_ms, end_ms) / 1000, tz=timezone.utc)
    for y, m in _months(start_dt, end_dt):
        rows = _zip_csv_rows(f'{VISION}/monthly/fundingRate/{symbol}/'
                             f'{symbol}-fundingRate-{y:04d}-{m:02d}.zip')
        if rows is None:
            continue                                   # pre-listing or not published yet
        for c in rows:
            try:
                ts, rate = int(c[0]), float(c[-1])     # calc_time, last_funding_rate
            except (ValueError, IndexError):
                continue
            if start_ms <= ts <= end_ms:               # inclusive, like fapi's endTime
                out.append((ts, rate))
    return out


def vision_has_month(symbol, interval, year, month, timeout=15):
    """Does the archive have this symbol's klines for that month? (cheap HEAD probe — used as a
    combined existence + listing-age check when exchangeInfo is unreachable)."""
    url = (f'{VISION}/monthly/klines/{symbol}/{interval}/'
           f'{symbol}-{interval}-{year:04d}-{month:02d}.zip')
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, TimeoutError):
        return False


def _fapi_rows(symbol, start_ms, end_ms, interval, timeout=20):
    out, cur = [], start_ms
    while cur < end_ms:
        url = (f'{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}'
               f'&startTime={cur}&endTime={end_ms}&limit=1500')
        raw = _http_get(url, timeout=timeout, retries=4)
        data = json.loads(raw) if raw else []
        if not data:
            break
        out.extend(data)
        if len(data) < 1500:
            break
        cur = data[-1][0] + 1
        time.sleep(0.1)
    return out


def _fapi_funding(symbol, start_ms, end_ms, timeout=20):
    """Funding events [(calc_time_ms, rate), ...] from the live API, paginated."""
    out, cur = [], start_ms
    while cur < end_ms:
        url = (f'{FAPI}/fapi/v1/fundingRate?symbol={symbol}'
               f'&startTime={cur}&endTime={end_ms}&limit=1000')
        raw = _http_get(url, timeout=timeout, retries=4)
        data = json.loads(raw) if raw else []
        if not data:
            break
        out.extend((int(r['fundingTime']), float(r['fundingRate'])) for r in data)
        nxt = int(data[-1]['fundingTime']) + 1
        if nxt <= cur or len(data) < 1000:
            break
        cur = nxt
    return out


_FUND_COV = {}                                         # (symbol, y, m) -> prev monthly file exists?


def _vision_funding_coverage_ms(symbol):
    """Through when the archive's funding is COMPLETE for this symbol: the start of the current
    month, or of the previous one while its monthly file is still unpublished (1-2 day lag,
    probed once per process). Beyond this point absence of events means "not published yet",
    never "no funding happened"."""
    now = _now_utc()
    py, pm = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    key = (symbol, py, pm)
    if key not in _FUND_COV:
        url = f'{VISION}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{py:04d}-{pm:02d}.zip'
        try:
            req = urllib.request.Request(url, method='HEAD')
            with urllib.request.urlopen(req, timeout=15) as r:
                _FUND_COV[key] = (r.status == 200)
        except Exception:                              # noqa: BLE001 — unknown -> not covered
            _FUND_COV[key] = False
    y, m = (now.year, now.month) if _FUND_COV[key] else (py, pm)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_funding(symbol, start_ms, end_ms):
    """Funding events with the same fapi→Vision fallback as fetch_rows — or **None** when the
    source cannot cover the WHOLE window. The archive has monthly files only, so under a
    geo-block the current month is uncovered: unknown is not zero, and a caller must never
    book an empty-because-unpublished window as zero-funding truth."""
    if fapi_reachable():
        try:
            return _fapi_funding(symbol, start_ms, end_ms)
        except urllib.error.HTTPError as e:
            if e.code not in GEO_CODES:
                raise
            _MODE['fapi'] = False
    if end_ms > _vision_funding_coverage_ms(symbol):
        return None                                    # window reaches beyond the archive
    return vision_funding(symbol, start_ms, end_ms)


def fetch_rows(symbol, start_ms, end_ms, interval='1d'):
    """Klines rows in /fapi/v1/klines shape: fapi when reachable, the Vision archive otherwise.
    Same Binance bars either way — the fallback only trades freshness (archive lag), never the
    data lineage. A mid-flight geo-block (451/403) flips the process to Vision permanently."""
    if fapi_reachable():
        try:
            return _fapi_rows(symbol, start_ms, end_ms, interval)
        except urllib.error.HTTPError as e:
            if e.code not in GEO_CODES:
                raise
            _MODE['fapi'] = False                      # edge said "wrong region" — stop asking
    if not _MODE.get('warned'):
        _MODE['warned'] = True
        print('[data] fapi.binance.com unreachable (geo-block?) → data.binance.vision archive: '
              'same Binance bars, ~10-30h behind live', flush=True)
    return vision_rows(symbol, start_ms, end_ms, interval)


def freshness_note(rows):
    """Human line about how far behind live the tail of `rows` is (for logs/health payloads)."""
    if not rows:
        return 'no bars'
    last_close = datetime.fromtimestamp(rows[-1][6] / 1000, tz=timezone.utc)
    lag = _now_utc() - last_close
    hours = lag.total_seconds() / 3600
    return (f'data through {last_close:%Y-%m-%d %H:%M} UTC '
            f'({hours:.1f}h behind live, source {active_source()})')
