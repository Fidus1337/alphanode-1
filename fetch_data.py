"""Download daily OHLCV from Binance USD-M (perpetual) and write data.pickle.

Takes the top-N USDT perps BY 24H VOLUME among those with history >= MIN_YEARS years (by the
onboardDate listing date), downloads daily bars, and atomically overwrites data.pickle in the
(tickers, ohlcvs) format — exactly what evolution/ and AlphaNode read. Public endpoints, no keys needed.

  python fetch_data.py --top 150
  python fetch_data.py --top 100 --min-years 3 --start 2019-09-05
  python fetch_data.py --top 200 --min-years 2 --out data.pickle

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
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from quantpylib.wrappers.binance import Binance   # noqa: E402

MIN_BARS = 30                                      # final backstop: completely empty ones — skip
YEAR_SECS = 365.25 * 24 * 3600


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


async def fetch(top_n, start, end, out_path, quote, min_years, concurrency, timeout):
    bn = Binance()

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

    print(f'· downloading daily bars up to {end.date()} '
          f'(each pair starts from its listing; {concurrency} in parallel)…', flush=True)
    sem = asyncio.Semaphore(concurrency)

    async def one(sym):
        ob = onboard.get(sym)
        s_start = start
        if ob is not None:
            ob_dt = datetime.fromtimestamp(ob, tz=timezone.utc)
            if ob_dt > start:
                s_start = ob_dt                                # without a day-by-day walk of the pre-listing period
        async with sem:
            try:
                df = await asyncio.wait_for(bn.get_trade_bars(sym, s_start, end, 'd', 1), timeout=timeout)
                return sym, df
            except asyncio.TimeoutError:
                return sym, TimeoutError(f'timeout {timeout}s')
            except Exception as e:                             # noqa: BLE001
                return sym, e

    got = {}
    done = 0
    for coro in asyncio.as_completed([one(s) for s in chosen]):
        sym, res = await coro
        done += 1
        if isinstance(res, Exception):
            print(f'  [{done}/{len(chosen)}] {sym}: ! {type(res).__name__} {res}', flush=True)
            continue
        n = 0 if res is None else len(res)
        ok = res is not None and not res.empty and n >= MIN_BARS
        print(f'  [{done}/{len(chosen)}] {sym}: {n} bars{"" if ok else "  (skip)"}', flush=True)
        if ok:
            got[sym] = res

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
    ap.add_argument('--start', default='2019-09-05', help='history start YYYY-MM-DD')
    ap.add_argument('--end', default=None, help='history end YYYY-MM-DD (default today)')
    ap.add_argument('--out', default=os.path.join(HERE, 'data.pickle'), help='where to write')
    ap.add_argument('--quote', default='USDT', help='quote currency (USDT)')
    ap.add_argument('--concurrency', type=int, default=6, help='how many pairs to download in parallel')
    ap.add_argument('--timeout', type=float, default=120, help='timeout per pair, sec')
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(lambda _l, _c: None)      # silence the cosmetic aiosonic teardown
    try:
        rc = loop.run_until_complete(fetch(args.top, start, end, args.out, args.quote,
                                           args.min_years, args.concurrency, args.timeout))
    except KeyboardInterrupt:
        rc = 130
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)                                         # instant: without GC junk from SSL transports


if __name__ == '__main__':
    main()
