"""Local signal API — serve the CURRENT target positions of a formula (or a combined portfolio),
computed on LIVE Binance data, over a tiny stdlib HTTP server (JSON, localhost only).

    python alphanode/signal_service.py          # config from ALPHANODE_SIGNAL_* env + config.ini
    <exe> --role signal                         # frozen build

It is the engine's target-weight computation turned into a live service: fetch fresh CLOSED candles
at the active timeframe -> compute target weights -> serve the last bar's book. Daily bars run
through the REAL quantpylib engine (combine via Portfolio); intraday timeframes (15m/1h/4h) use
the forward track's math — fastsim parameterized by the bar size — because the quantpylib
engine is daily-tuned. A background thread recomputes every REFRESH seconds and keeps the last
result in memory; requests are served instantly from that cache.

Env:
  ALPHANODE_SIGNAL_FORMULAS  JSON list of formula strings (or a single string). Required.
  ALPHANODE_SIGNAL_NAME      label for the served strategy (default 'signal')
  ALPHANODE_SIGNAL_PORT      default 8799
  ALPHANODE_SIGNAL_REFRESH   seconds between recomputes (default 900)
  ALPHANODE_SIGNAL_START     ISO date to warm the engine from (default = config train_start)
  (vol / fee / universe / data come from config.ini via load_config)

Endpoints (bound to 127.0.0.1 — a signal is private):
  GET /signal   -> {ok, name, formulas, as_of, leverage, positions:[{ticker,side,weight,weight_pct}],
                    updated_at, error}
  GET /health   -> {ok, name, pid, n_formulas, n_tickers, updated_at, age_secs, computing, error}
                   pid/n_* let a restarted GUI re-adopt a service it no longer has a handle on.

NOTE: this is an advisory SIGNAL feed, not execution. No orders, keys, limits or kill-switch —
the consumer decides how (and whether) to trade it. Nothing here places an order.
"""
import os
import sys
import json
import time
import threading
import http.server
import socketserver
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings                                          # noqa: E402
import numpy as np                                       # noqa: E402
import pandas as pd                                      # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')

import vision_klines as vk                               # noqa: E402  fapi→archive fallback

DUST_W = 0.0005                                          # ignore weights below this in the output

# ---- shared state (the HTTP handler reads this; the refresh thread writes it) ----
_STATE = {'lock': threading.Lock(), 'signal': None, 'updated': None, 'ts': 0.0,
          'error': None, 'computing': False, 'name': 'signal', 'formulas': [], 'n_tickers': 0,
          'tf': '1d', 'progress': ''}


def _progress(txt):
    """One line of live progress served on /health — the GUI shows it while `computing` is up.
    Without it a cold intraday start looks hung for minutes and users kill a working service."""
    with _STATE['lock']:
        _STATE['progress'] = txt


# Cold-start fetch concurrency, per bar size — the same counts fetch_data.py settled on: MORE
# workers trip Binance's request-weight cooldown and finish LATER in wall-clock, not sooner.
_WORKERS = {'1d': 6, '4h': 4, '1h': 3, '15m': 2}


# ---------------- live Binance klines (stdlib, no keys) ----------------
def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fetch_klines(symbol, start_ms, end_ms, interval='1d'):
    # fapi when reachable, the data.binance.vision archive when geo-blocked: same Binance
    # bars either way — a blocked region serves slightly older closed bars, never other data
    out = vk.fetch_rows(symbol, start_ms, end_ms, interval)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=['openTime', 'open', 'high', 'low', 'close', 'volume',
                                    'closeTime', 'qav', 'trades', 'tbb', 'tbq', 'ig'])
    df = df[df['closeTime'] <= _now_ms()]                # CLOSED candles only
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = df[c].astype(float)
    df['datetime'] = pd.to_datetime(df['openTime'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
    return df[~df.index.duplicated()]


def _snapshot_dfs(data_path, tickers, start_ts):
    """{ticker: OHLCV} seeded from the per-tf snapshot the GUI's data fetcher already wrote
    (cfg['data'] — the ALPHANODE_DATA the GUI passes). Re-downloading years of intraday bars on
    every cold start took ~17 min for 50 pairs of 1h — with the seed only the tail is paged.
    The 'funding' column is dropped: live fetches never had it, so the served math stays
    IDENTICAL to before, just fed faster. The (possibly still-open) last snapshot bar is
    replaced by the tail fetch, which serves closed candles only."""
    try:
        import pickle
        with open(data_path, 'rb') as f:
            tk, ohlcvs = pickle.load(f)
    except Exception:                                    # noqa: BLE001  no/bad snapshot -> full fetch
        return {}
    cols = ['open', 'high', 'low', 'close', 'volume']
    out = {}
    for t, df in zip(tk, ohlcvs):
        try:
            if t in tickers and len(df):
                d = df[cols].astype(float)
                d = d[d.index >= start_ts]
                if len(d):
                    out[t] = d
        except Exception:                                # noqa: BLE001  odd shape -> fetch this one
            pass
    return out


def fetch_live_dfs(tickers, start, interval='1d', cache=None, data_path=None):
    """{ticker: OHLCV df} of fresh CLOSED candles from `start` to now (skips failures).
    `cache` (the previous call's result) turns a refresh into one tail request per pair; on a
    cold start `data_path` seeds history from the local snapshot. Both empty -> the old
    behavior: page everything, but in parallel (_WORKERS) with live progress on /health."""
    start_ts = pd.Timestamp(start, tz='UTC')
    start_ms = int(start_ts.timestamp() * 1000)
    dfs = dict(cache) if cache else {}
    if not dfs and data_path:
        dfs = _snapshot_dfs(data_path, set(tickers), start_ts)
        if dfs:
            _progress(f'seeded {len(dfs)}/{len(tickers)} pairs from the local snapshot')
    todo = [t for t in tickers if t not in dfs] + [t for t in tickers if t in dfs]
    end_ms, total, done, out = _now_ms(), len(todo), 0, {}
    lock = threading.Lock()

    def _one(t):
        nonlocal done
        try:
            base = dfs.get(t)
            if base is not None:
                # refetch from the second-to-last kept bar: the merge below then REPLACES the
                # last cached row, so a partial bar from the snapshot/previous cycle never
                # survives into the served signal (fetch_klines keeps closed candles only).
                tail_from = base.index[-2] if len(base) > 1 else base.index[0]
                fresh = fetch_klines(t, int(tail_from.timestamp() * 1000), end_ms,
                                     interval=interval)
                df = (pd.concat([base[base.index < fresh.index[0]], fresh])
                      if len(fresh) else base.iloc[:-1])
            else:
                df = fetch_klines(t, start_ms, end_ms, interval=interval)
            df = df[df.index >= start_ts]
            if len(df) > 60:
                with lock:
                    out[t] = df
        except Exception:                                # noqa: BLE001
            pass
        finally:
            with lock:
                done += 1
            _progress(f'fetching live {interval} bars · {done}/{total} pairs')

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=_WORKERS.get(interval, 3)) as ex:
        list(ex.map(_one, todo))
    _progress('')
    return out


# ---------------- compute the current signal (pure: no network) ----------------
def compute_from_dfs(formulas, dfs, start, vol, exec_rate):
    """Target weights + leverage on the LAST closed bar, for a formula or a combined portfolio.
    `dfs`: {ticker: OHLCV df}. Returns a signal dict (as served on /signal)."""
    from evolved_strategy import make_evolved
    from quantpylib.simulator.alpha import Portfolio

    tk = [t for t in dfs if len(dfs[t]) > 60]
    if not tk:
        raise RuntimeError('no usable data for any ticker')
    last_date = max(dfs[t].index[-1] for t in tk)
    end = datetime(last_date.year, last_date.month, last_date.day)

    stratdfs = []
    for i, formula in enumerate(formulas):
        Alpha = make_evolved(formula, f'Sig{i}')
        a = Alpha(insts=tk, dfs={t: dfs[t].copy() for t in tk}, start=start, end=end,
                  portfolio_vol=vol, execrates=exec_rate)
        stratdfs.append(a.run_simulation())
    pf = Portfolio(insts=tk, dfs={t: dfs[t].copy() for t in tk}, start=start, end=end,
                   stratdfs=stratdfs, portfolio_vol=vol, execrates=exec_rate)
    last = pf.run_simulation().iloc[-1]

    lev = float(last.get('leverage', 0.0))
    positions = []
    for t in tk:
        w = float(last.get(f'{t} w', 0.0))
        if abs(w) > DUST_W:
            positions.append({'ticker': t, 'side': 'LONG' if w > 0 else 'SHORT',
                              'weight': round(w, 6), 'weight_pct': f'{w * 100:+.1f}%'})
    positions.sort(key=lambda p: -abs(p['weight']))
    return {'as_of': f'{end:%Y-%m-%d}', 'leverage': round(lev, 4),
            'n_assets': len(tk), 'positions': positions}


def compute_from_dfs_fast(formulas, dfs, start, vol, exec_rate, tf):
    """Intraday target weights — the forward track's math, not the daily engine: fastsim
    parameterized by the bar size (ann, vol window, EWMA λ from timeframe.resolve). The
    combined path's last row is already weight×leverage, so `leverage` is served as 1.0 and
    `weight` carries the whole signed position size — consumer math (weight × leverage ×
    equity) stays identical to the daily payload. Bit-for-bit the same numbers the forward
    track would trade on this bar."""
    from genome import parse
    from evaluator import panel_from_raw, make_market, eval_alpha_panel
    from fastsim import fast_sim_paths
    from timeframe import resolve
    t = resolve(tf)
    tk = [x for x in dfs if len(dfs[x]) > 60]
    if not tk:
        raise RuntimeError('no usable data for any ticker')
    start_ts = pd.Timestamp(start, tz='UTC')
    end = max(dfs[x].index[-1] for x in tk)
    panel = panel_from_raw(tk, dfs, start_ts, end, t.pandas_freq)
    market = make_market(panel, tk, dfs, vol_window=t.vol_window)
    paths = []
    for f in formulas:
        ap = eval_alpha_panel(parse(f), panel)
        _r, wl = fast_sim_paths(ap[tk].to_numpy(dtype=np.float64), market, vol, exec_rate,
                                ann=t.periods_per_year, ewma_lambda=t.ewma_lambda)
        paths.append(wl)
    comb = np.sum(paths, axis=0)
    _r, comb_wl = fast_sim_paths(comb, market, vol, exec_rate,
                                 ann=t.periods_per_year, ewma_lambda=t.ewma_lambda)
    last = np.nan_to_num(comb_wl[-1])
    positions = []
    for i, x in enumerate(tk):
        w = float(last[i])
        if abs(w) > DUST_W:
            positions.append({'ticker': x, 'side': 'LONG' if w > 0 else 'SHORT',
                              'weight': round(w, 6), 'weight_pct': f'{w * 100:+.1f}%'})
    positions.sort(key=lambda p: -abs(p['weight']))
    return {'as_of': f'{end:%Y-%m-%d %H:%M}', 'leverage': 1.0,
            'n_assets': len(tk), 'positions': positions}


def compute_signal(formulas, tickers, start, vol, exec_rate, tf='1d', cache=None, data_path=None):
    """One full cycle: (signal, dfs). `dfs` goes back in as `cache` next cycle — a refresh then
    costs one tail request per pair instead of re-paging the whole history."""
    dfs = fetch_live_dfs(tickers, start, interval=tf, cache=cache, data_path=data_path)
    if not dfs:
        raise RuntimeError('could not fetch live data for any ticker')
    _progress('computing target weights…')
    try:
        if tf == '1d':
            return compute_from_dfs(formulas, dfs, start, vol, exec_rate), dfs
        return compute_from_dfs_fast(formulas, dfs, start, vol, exec_rate, tf), dfs
    finally:
        _progress('')


# ---------------- background refresh loop ----------------
def _utcnow_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def refresh_loop(formulas, tickers, start, vol, exec_rate, refresh, stop, tf='1d', data_path=None):
    cache = {}                                            # {ticker: OHLCV} carried across cycles
    while not stop.is_set():
        with _STATE['lock']:
            _STATE['computing'] = True
        try:
            t0 = time.time()
            sig, dfs = compute_signal(formulas, tickers, start, vol, exec_rate, tf=tf,
                                      cache=cache, data_path=(None if cache else data_path))
            # merge, don't replace: a pair whose fetch failed this cycle keeps its history in
            # the cache (next cycle is a cheap tail request), it just sits out this signal
            cache.update(dfs)
            with _STATE['lock']:
                _STATE.update(signal=sig, updated=_utcnow_iso(), ts=time.time(), error=None)
            print(f'[signal] updated {sig["as_of"]} · {len(sig["positions"])} positions · '
                  f'lev {sig["leverage"]:.2f} · {time.time() - t0:.1f}s', flush=True)
        except Exception as e:                            # noqa: BLE001
            with _STATE['lock']:
                _STATE['error'] = f'{type(e).__name__}: {e}'
            print(f'[signal] compute failed: {e}', flush=True)
        finally:
            with _STATE['lock']:
                _STATE['computing'] = False
        stop.wait(refresh)                                # interruptible sleep


# ---------------- HTTP server (localhost) ----------------

class _NoDNSHTTPServer(http.server.ThreadingHTTPServer):
    """server_bind without socket.getfqdn() — the reverse-DNS lookup the stock HTTPServer
    does at bind time hangs for minutes on Windows boxes with broken PTR resolution, and
    the /health endpoint must come up instantly (same fix as the node status server)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/') or '/'
        with _STATE['lock']:
            sig, upd, ts = _STATE['signal'], _STATE['updated'], _STATE['ts']
            err, computing, name, forms = (_STATE['error'], _STATE['computing'],
                                           _STATE['name'], _STATE['formulas'])
            n_tick = _STATE['n_tickers']
        age = round(time.time() - ts, 1) if ts else None
        if path == '/health':
            # pid + counts: everything the GUI needs to re-adopt a service it lost the handle to
            self._send(200, {'ok': sig is not None, 'name': name, 'pid': os.getpid(),
                             'n_formulas': len(forms), 'n_tickers': n_tick, 'tf': _STATE['tf'],
                             'updated_at': upd, 'age_secs': age, 'computing': computing,
                             'progress': _STATE['progress'], 'error': err})
        elif path in ('/', '/signal'):
            if sig is None:
                self._send(503, {'ok': False, 'name': name, 'formulas': forms,
                                 'error': err or 'computing the first signal…'})
            else:
                self._send(200, {'ok': True, 'name': name, 'formulas': forms,
                                 'tf': _STATE['tf'], 'updated_at': upd, 'age_secs': age,
                                 'error': err, **sig})
        else:
            self._send(404, {'ok': False, 'error': 'not found', 'endpoints': ['/signal', '/health']})

    def log_message(self, *a):                            # keep the console quiet
        pass


def main():
    from config import load_config
    cfg = load_config()

    raw = os.environ.get('ALPHANODE_SIGNAL_FORMULAS') or ''
    try:
        formulas = json.loads(raw) if raw.strip().startswith('[') else ([raw] if raw.strip() else [])
    except json.JSONDecodeError:
        formulas = [raw] if raw.strip() else []
    formulas = [f for f in formulas if f and f.strip()]
    if not formulas:
        print('signal: no formula(s) — set ALPHANODE_SIGNAL_FORMULAS', file=sys.stderr)
        sys.exit(2)

    name = os.environ.get('ALPHANODE_SIGNAL_NAME') or 'signal'
    port = int(os.environ.get('ALPHANODE_SIGNAL_PORT') or 8799)
    tf = (os.environ.get('ALPHANODE_TF') or '1d').strip().lower()
    # intraday bars close often — refresh accordingly unless the caller pinned a cadence
    refresh_dflt = {'15m': 300, '1h': 300, '4h': 900}.get(tf, 900)
    refresh = max(30, int(os.environ.get('ALPHANODE_SIGNAL_REFRESH') or refresh_dflt))
    start_env = os.environ.get('ALPHANODE_SIGNAL_START')
    start = datetime.fromisoformat(start_env) if start_env else cfg['start']
    if tf != '1d':
        try:                                              # same clamp as forward-track enrollment:
            from timeframe import resolve                 # paging YEARS of intraday klines is warm-up
            start = max(start, datetime.fromisoformat(resolve(tf).history))   # nobody needs — the
        except Exception:                                 # noqa: BLE001        tf's recommended
            pass                                          # history is plenty

    def ev(k, fallback):
        raw_v = os.environ.get('ALPHANODE_' + k)
        return float(raw_v) if raw_v not in (None, '') else fallback
    vol = ev('TARGET_VOL', cfg['vol'])                    # load_config does not apply these env
    exec_rate = ev('EXEC_COST', cfg['exec'])              # overrides itself (node does) — mirror it

    tk_env = os.environ.get('ALPHANODE_SIGNAL_TICKERS')   # explicit universe (from the GUI)
    if tk_env:
        tickers = [x.strip().upper() for x in tk_env.split(',') if x.strip()]
    elif cfg.get('instruments'):
        tickers = list(cfg['instruments'])
    else:                                                 # universe = all -> tickers of the dataset
        import pickle
        with open(cfg['data'], 'rb') as f:
            tickers = list(pickle.load(f)[0])

    _STATE['name'] = name
    _STATE['formulas'] = formulas
    _STATE['n_tickers'] = len(tickers)
    _STATE['tf'] = tf
    stop = threading.Event()
    threading.Thread(target=refresh_loop,
                     args=(formulas, tickers, start, vol, exec_rate, refresh, stop, tf,
                           cfg['data']),                  # per-tf snapshot seeds the cold start
                     daemon=True).start()

    host = os.environ.get('ALPHANODE_SIGNAL_HOST') or '127.0.0.1'   # Docker: set 0.0.0.0 to expose it
    srv = _NoDNSHTTPServer((host, port), Handler)
    print(f'[signal] "{name}" · {len(formulas)} formula(s) · {len(tickers)} pairs · tf {tf} · '
          f'refresh {refresh}s · serving http://{host}:{port}/signal', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == '__main__':
    main()
