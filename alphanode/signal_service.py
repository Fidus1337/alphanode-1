"""Local signal API — serve the CURRENT target positions of a formula (or a combined portfolio),
computed on LIVE Binance data, over a tiny stdlib HTTP server (JSON, localhost only).

    python alphanode/signal_service.py          # config from ALPHANODE_SIGNAL_* env + config.ini
    <exe> --role signal                         # frozen build

It is `paper_export`'s compute_targets turned into a live service: fetch fresh CLOSED daily
candles -> run each alpha through the REAL quantpylib engine -> combine via Portfolio -> the last
row's target weights + leverage. A background thread recomputes every REFRESH seconds and keeps the
last result in memory; requests are served instantly from that cache.

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
  GET /health   -> {ok, name, updated_at, age_secs, computing, error}

NOTE: this is an advisory SIGNAL feed, not execution. No orders, keys, limits or kill-switch —
the consumer decides how (and whether) to trade it. Same disclaimer as the paper bundle.
"""
import os
import sys
import json
import time
import threading
import http.server
import urllib.request
import urllib.error
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

KLINES = 'https://fapi.binance.com/fapi/v1/klines'
DUST_W = 0.0005                                          # ignore weights below this in the output

# ---- shared state (the HTTP handler reads this; the refresh thread writes it) ----
_STATE = {'lock': threading.Lock(), 'signal': None, 'updated': None, 'ts': 0.0,
          'error': None, 'computing': False, 'name': 'signal', 'formulas': []}


# ---------------- live Binance klines (stdlib, no keys) ----------------
def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _fetch_json(url, retries=4):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_klines(symbol, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        url = f'{KLINES}?symbol={symbol}&interval=1d&startTime={cur}&endTime={end_ms}&limit=1500'
        data = _fetch_json(url)
        if not data:
            break
        out.extend(data)
        if len(data) < 1500:
            break
        cur = data[-1][0] + 1
        time.sleep(0.1)
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


def fetch_live_dfs(tickers, start):
    """{ticker: OHLCV df} of fresh closed daily candles from `start` to now (skips failures)."""
    start_ms = int(pd.Timestamp(start, tz='UTC').timestamp() * 1000)
    dfs = {}
    for t in tickers:
        try:
            df = fetch_klines(t, start_ms, _now_ms())
            if len(df) > 60:
                dfs[t] = df
        except Exception:                                # noqa: BLE001
            pass
    return dfs


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


def compute_signal(formulas, tickers, start, vol, exec_rate):
    dfs = fetch_live_dfs(tickers, start)
    if not dfs:
        raise RuntimeError('could not fetch live data for any ticker')
    return compute_from_dfs(formulas, dfs, start, vol, exec_rate)


# ---------------- background refresh loop ----------------
def _utcnow_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def refresh_loop(formulas, tickers, start, vol, exec_rate, refresh, stop):
    while not stop.is_set():
        with _STATE['lock']:
            _STATE['computing'] = True
        try:
            sig = compute_signal(formulas, tickers, start, vol, exec_rate)
            with _STATE['lock']:
                _STATE.update(signal=sig, updated=_utcnow_iso(), ts=time.time(), error=None)
            print(f'[signal] updated {sig["as_of"]} · {len(sig["positions"])} positions · '
                  f'lev {sig["leverage"]:.2f}', flush=True)
        except Exception as e:                            # noqa: BLE001
            with _STATE['lock']:
                _STATE['error'] = f'{type(e).__name__}: {e}'
            print(f'[signal] compute failed: {e}', flush=True)
        finally:
            with _STATE['lock']:
                _STATE['computing'] = False
        stop.wait(refresh)                                # interruptible sleep


# ---------------- HTTP server (localhost) ----------------
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
        age = round(time.time() - ts, 1) if ts else None
        if path == '/health':
            self._send(200, {'ok': sig is not None, 'name': name, 'updated_at': upd,
                             'age_secs': age, 'computing': computing, 'error': err})
        elif path in ('/', '/signal'):
            if sig is None:
                self._send(503, {'ok': False, 'name': name, 'formulas': forms,
                                 'error': err or 'computing the first signal…'})
            else:
                self._send(200, {'ok': True, 'name': name, 'formulas': forms,
                                 'updated_at': upd, 'age_secs': age, 'error': err, **sig})
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
    refresh = max(30, int(os.environ.get('ALPHANODE_SIGNAL_REFRESH') or 900))
    start_env = os.environ.get('ALPHANODE_SIGNAL_START')
    start = datetime.fromisoformat(start_env) if start_env else cfg['start']

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
    stop = threading.Event()
    threading.Thread(target=refresh_loop,
                     args=(formulas, tickers, start, cfg['vol'], cfg['exec'], refresh, stop),
                     daemon=True).start()

    host = os.environ.get('ALPHANODE_SIGNAL_HOST') or '127.0.0.1'   # Docker: set 0.0.0.0 to expose it
    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f'[signal] "{name}" · {len(formulas)} formula(s) · {len(tickers)} pairs · '
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
