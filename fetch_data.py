"""Download OHLCV from Binance USD-M (perpetual) and write a data snapshot pickle.

Takes the top-N USDT perps BY 24H VOLUME among those with history >= MIN_YEARS years (by the
onboardDate listing date), downloads bars at --interval (1d default; 15m|1h|4h intraday),
and atomically overwrites the snapshot in the (tickers, ohlcvs) format — exactly what
evolution/ and AlphaNode read. 1d writes data.pickle; an intraday interval writes its own
data_<tf>.pickle, so timeframes never clobber each other. Public endpoints, no keys needed.

  python fetch_data.py --top 150
  python fetch_data.py --top 100 --min-years 3 --start 2019-09-05
  python fetch_data.py --top 60 --interval 1h --start 2023-01-01

Why the years filter: young coins (listed recently) would give almost solid NaN in the search
window and would break the data fetcher (the wrapper walks the pre-listing period one day at a time
-> thousands of requests). We cut them off by onboardDate and start each pair's download from its listing date.

Caution: overwrites data.pickle (the old snapshot is replaced). Run it when the node is stopped.
Note: these are SURVIVING coins (Binance doesn't serve delisted ones) — survivorship remains.
"""
import os
import sys
import pickle
import asyncio
import argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, 'evolution')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantpylib.wrappers.binance import Binance                 # noqa: E402
from quantpylib.throttler.rate_semaphore import AsyncRateSemaphore  # noqa: E402
from quantpylib.throttler.exceptions import HTTPException           # noqa: E402
try:
    from timeframe import resolve as _resolve_tf   # per-timeframe recommended history start
except Exception:                                  # pragma: no cover
    _resolve_tf = None

MIN_BARS = 30                                      # final backstop: completely empty ones — skip
YEAR_SECS = 365.25 * 24 * 3600
# bar size -> the qt wrapper's (granularity, multiplier); keep in sync with evolution/timeframe.py
INTERVALS = {'15m': ('m', 15), '1h': ('h', 1), '4h': ('h', 4), '1d': ('d', 1)}
# default per-PAIR timeout by interval: intraday majors need MANY paginated requests (BTC 1h from
# 2019 ≈ 40+ x 1490-bar pages through a shared rate limiter). A flat 120s silently DROPS them.
TIMEOUTS = {'1d': 120, '4h': 360, '1h': 900, '15m': 2400}
# default concurrency by interval: intraday floods Binance with paginated kline requests; at 6
# parallel the weight limit kicks in after ~15 min and ALL downloads stall in a rate-limit pause.
# Fewer workers finish sooner in wall-clock because they never trip the cooldown.
CONCURRENCY = {'1d': 6, '4h': 4, '1h': 3, '15m': 2}


def save_pickle(path, obj):
    with open(path, 'wb') as f:
        pickle.dump(obj, f)


async def _aclose(bn):
    """Gently close the aiosonic session before the loop closes — otherwise a cosmetic SSL-transport
    traceback spills out on exit (the data is already written by that point)."""
    try:
        sess = getattr(bn.http_client, 'session', None)
        if sess is not None:
            await sess.__aexit__(None, None, None)
    except Exception:
        pass
    await asyncio.sleep(0.2)


def _onboard_ts(sym_info):
    od = sym_info.get('onboardDate')
    try:
        return int(od) / 1000.0 if od else None    # listing unix-seconds
    except (TypeError, ValueError):
        return None


async def fetch(top_n, start, end, out_path, quote, min_years, concurrency, timeout, interval='1d'):
    gran, mult = INTERVALS[interval]
    bn = Binance()

    # Binance fapi allows 2400 weight/min and a 1500-bar kline page costs weight 10 → ~240 pages
    # a minute for the whole IP. quantpylib's stock throttle (6000 credits / 20 per page / 60s)
    # admits 300 pages/min — 25% OVER budget, so long intraday paginations trip 429 regardless of
    # how few pairs run in parallel. Re-budget to 180 pages/min and, when a 429/418 still slips
    # through, honor Retry-After and repeat the SAME page instead of dropping the whole pair.
    bn.rate_semaphore = AsyncRateSemaphore(3600)
    raw_request = bn.http_client.request

    async def paced_request(**kw):
        attempts = 8
        for i in range(attempts):
            try:
                return await raw_request(**kw)
            except HTTPException as e:
                if e.status_code not in (429, 418) or i == attempts - 1:
                    raise
                try:
                    ra = float(e.headers.get('retry-after'))
                except (TypeError, ValueError, AttributeError):
                    ra = 30.0
                wait = ra + 5 + 15 * i                     # the weight window is a rolling minute:
                print(f'  · rate-limit {e.status_code}: pausing {wait:.0f}s, '  # right after Retry-After
                      f'then retrying the same page (attempt {i + 1}/{attempts})…', flush=True)
                await asyncio.sleep(wait)

    bn.http_client.request = paced_request

    print('· pulling instrument list (exchangeInfo)…', flush=True)
    info = await bn.exchange_info()
    onboard = {}
    perps = []
    for s in info.get('symbols', []):
        if (s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL'
                and s.get('quoteAsset') == quote):
            perps.append(s['symbol'])
            onboard[s['symbol']] = _onboard_ts(s)

    cutoff = end.timestamp() - min_years * YEAR_SECS          # listed no later than min_years before the end
    aged = [sym for sym in perps if onboard.get(sym) is not None and onboard[sym] <= cutoff]
    print(f'  {quote} perps TRADING: {len(perps)}; with history >= {min_years:g} years: {len(aged)} '
          f'(young filtered out: {len(perps) - len(aged)})', flush=True)

    print('· ranking by 24h turnover (ticker/24hr)…', flush=True)
    tick = await bn.http_client.request(endpoint='/fapi/v1/ticker/24hr', method='GET')
    vol = {t['symbol']: float(t.get('quoteVolume', 0) or 0) for t in tick}
    aged.sort(key=lambda s: vol.get(s, 0.0), reverse=True)
    chosen = aged[:top_n]
    print(f'  taking top-{len(chosen)}: {", ".join(chosen[:12])}{" …" if len(chosen) > 12 else ""}', flush=True)

    print(f'· downloading {interval} bars up to {end.date()} '
          f'(each pair starts from its listing; {concurrency} in parallel)…', flush=True)
    sem = asyncio.Semaphore(concurrency)
    active = {}                                                # sym -> download start (monotonic)

    async def one(sym):
        ob = onboard.get(sym)
        s_start = start
        if ob is not None:
            ob_dt = datetime.fromtimestamp(ob, tz=timezone.utc)
            if ob_dt > start:
                s_start = ob_dt                                # without a day-by-day walk of the pre-listing period
        async with sem:
            active[sym] = asyncio.get_event_loop().time()
            try:
                df = await asyncio.wait_for(bn.get_trade_bars(sym, s_start, end, gran, mult),
                                            timeout=timeout)
                return sym, df
            except asyncio.TimeoutError:
                return sym, TimeoutError(f'timeout {timeout}s')
            except Exception as e:                             # noqa: BLE001
                return sym, e
            finally:
                active.pop(sym, None)

    got = {}
    done = 0
    pending = set(chosen)

    async def heartbeat():
        """Intraday pairs take minutes each with no per-request output — show what is ACTUALLY
        downloading right now (only `concurrency` pairs at a time; the rest wait in a queue) so
        the console doesn't look frozen while the majors paginate through years of bars."""
        loop = asyncio.get_event_loop()
        last_done = -1
        stall_since = loop.time()
        while pending:
            await asyncio.sleep(20)
            if not pending:
                break
            if done != last_done:                              # progress since the last beat
                last_done, stall_since = done, loop.time()
            now = loop.time()
            act = ', '.join(f'{s} {now - t:.0f}s' for s, t in sorted(active.items(), key=lambda kv: kv[1]))
            line = (f'  … downloading now: {act or "—"} · queued {max(0, len(pending) - len(active))} '
                    f'· done {done}/{len(chosen)}')
            if now - stall_since > 120:                        # nothing finished for 2+ min
                line += ('  [no completions for a while — likely a Binance rate-limit pause; '
                         f'the client retries, each pair caps at {timeout:.0f}s]')
            print(line, flush=True)

    hb = asyncio.ensure_future(heartbeat())
    for coro in asyncio.as_completed([one(s) for s in chosen]):
        sym, res = await coro
        done += 1
        pending.discard(sym)
        if isinstance(res, Exception):
            hint = (' — re-run, or raise --timeout / lower --top' if isinstance(res, TimeoutError)
                    else '')
            print(f'  [{done}/{len(chosen)}] {sym}: ! {type(res).__name__} {res}{hint}', flush=True)
            continue
        n = 0 if res is None else len(res)
        ok = res is not None and not res.empty and n >= MIN_BARS
        print(f'  [{done}/{len(chosen)}] {sym}: {n} bars{"" if ok else "  (skip)"}', flush=True)
        if ok:
            got[sym] = res
    hb.cancel()

    tickers = [s for s in chosen if s in got]                 # order by volume
    ohlcvs = [got[s] for s in tickers]
    if not tickers:
        print('✗ nothing downloaded — not overwriting data.pickle', flush=True)
        await _aclose(bn)
        return 1

    tmp = out_path + '.tmp'                                    # atomic: the node won't catch a half-written file
    save_pickle(tmp, (tickers, ohlcvs))
    os.replace(tmp, out_path)
    span = f'{min(df.index.min() for df in ohlcvs).date()}..{max(df.index.max() for df in ohlcvs).date()}'
    print(f'✓ wrote {out_path}: {len(tickers)} pairs, range {span}', flush=True)
    await _aclose(bn)
    return 0


def main():
    ap = argparse.ArgumentParser(description='Download top-N USDT perps (with history >= N years) from Binance')
    ap.add_argument('--top', type=int, default=150, help='how many pairs (top by 24h turnover)')
    ap.add_argument('--min-years', type=float, default=3.0, help='minimum years of history (by listing date)')
    ap.add_argument('--start', default=None,
                    help='history start YYYY-MM-DD (default: the timeframe\'s recommended start — '
                         '2019-09-05 for 1d, later for intraday since its history is denser)')
    ap.add_argument('--end', default=None, help='history end YYYY-MM-DD (default today)')
    ap.add_argument('--out', default=None,
                    help='where to write (default: data.pickle for 1d, data_<interval>.pickle otherwise)')
    ap.add_argument('--quote', default='USDT', help='quote currency (USDT)')
    ap.add_argument('--concurrency', type=int, default=None,
                    help='how many pairs to download in parallel (default scales with --interval: '
                         '6 for 1d, 3 for 1h, 2 for 15m — more trips the Binance rate limit)')
    ap.add_argument('--timeout', type=float, default=None,
                    help='timeout per pair, sec (default scales with --interval: 120 for 1d, '
                         '900 for 1h, … — intraday needs many paginated requests per pair)')
    ap.add_argument('--interval', default='1d', choices=sorted(INTERVALS),
                    help='bar size (15m|1h|4h|1d); intraday is written to its own data_<tf>.pickle')
    args = ap.parse_args()
    if args.timeout is None:
        args.timeout = TIMEOUTS[args.interval]
    if args.concurrency is None:
        args.concurrency = CONCURRENCY[args.interval]
    if args.out is None:
        suffix = '' if args.interval == '1d' else f'_{args.interval}'
        args.out = os.path.join(HERE, f'data{suffix}.pickle')
    if args.start is None:                                # per-timeframe recommended history start
        args.start = (_resolve_tf(args.interval).history if _resolve_tf is not None
                      else '2019-09-05')
        print(f'· --start not given → {args.start} (recommended for {args.interval}); '
              'override with --start', flush=True)
    if args.interval == '15m' and args.top > 60:
        print('note: 15m bars are heavy (~96/day/pair) — consider --top <= 60', flush=True)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(lambda _l, _c: None)      # silence the cosmetic aiosonic teardown
    try:
        rc = loop.run_until_complete(fetch(args.top, start, end, args.out, args.quote,
                                           args.min_years, args.concurrency, args.timeout,
                                           interval=args.interval))
    except KeyboardInterrupt:
        rc = 130
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)                                         # instant: without GC junk from SSL transports


if __name__ == '__main__':
    main()
