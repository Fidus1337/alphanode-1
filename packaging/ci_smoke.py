"""Frozen-bundle smoke for CI (runs on BOTH the Windows and Linux jobs).

Two checks --role selfcheck can't give:
  1. one REAL search round — exercises the multiprocessing pool + numba + the library writer
     inside the frozen exe (mp in a windowed PyInstaller build on Windows is the classic
     breakage: freeze_support / spawn re-imports);
  2. the PDF analytics worker end-to-end — JSON payload on stdin -> {"ok": true} + a 4-page
     PDF on disk, exactly the path the GUI's "PDF report" buttons take via a child process
     (windowed exe + piped stdio is the other Windows-specific risk).

data.pickle is NOT in the repo (a 4.6MB binary the fetcher refreshes), so the smoke generates
a SYNTHETIC snapshot in load_raw's exact format and points the children at it via
ALPHANODE_DATA — CI needs no market data and no network.

    python packaging/ci_smoke.py packaging/dist/AlphaNode/AlphaNode[.exe]
    python packaging/ci_smoke.py alphanode/app_entry.py     # dev mode (script, not frozen)
"""
import json
import os
import pickle
import re
import random
import subprocess
import sys
import tempfile
from datetime import date, timedelta

target = os.path.abspath(sys.argv[1])
cmd = [sys.executable, target] if target.endswith('.py') else [target]
cwd = os.path.dirname(target)
tmp = tempfile.mkdtemp(prefix='an_ci_')


def make_data(path, n_tk=25):
    """Synthetic (tickers, [OHLCV dfs]) pickle covering the config.ini TRAIN..TEST span."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(11)
    idx = pd.date_range('2019-09-01', '2026-07-05', freq='D', tz='UTC')
    n = len(idx)
    tk, dfs = [], []
    for i in range(n_tk):
        ret = rng.normal(0.0005, 0.03 + 0.01 * (i % 5), n)
        close = 100.0 * np.exp(np.cumsum(ret))
        op = close * (1 + rng.normal(0, 0.005, n))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
        vol = np.exp(rng.normal(10, 1, n)) * (1 + 0.5 * np.sin(np.arange(n) / 37 + i))
        dfs.append(pd.DataFrame({'open': op, 'high': hi, 'low': lo, 'close': close,
                                 'volume': vol}, index=idx))
        tk.append(f'T{i:02d}USDT')
    with open(path, 'wb') as f:
        pickle.dump((tk, dfs), f)


data = os.path.join(tmp, 'data.pickle')
make_data(data)
print(f'synthetic  : {os.path.getsize(data) // 1024} KB market snapshot -> {data}')

# ---- 1. one search round in a scratch state dir ----
hang_file = os.path.join(tmp, 'hang_dump.txt')
node_log = os.path.join(tmp, 'node_log.txt')
env = dict(os.environ, ALPHANODE_DATA=data,
           ALPHANODE_MAX_ROUNDS='1', ALPHANODE_POP='20', ALPHANODE_GENS='2',
           ALPHANODE_PAUSE='0', ALPHANODE_STATE_DIR=tmp, ALPHANODE_STATUS_PORT='8799',
           # 100% CPU -> n_jobs=2 on the 2-core runners: the round goes through the REAL
           # multiprocessing spawn pool — the frozen-exe breakage this smoke exists to catch.
           # The default 50% gave n_jobs=1, whose in-process path keeps the GIL inside the
           # cythonized engine for a whole generation: the status thread never got scheduled,
           # so the HTTP watchdog was blind on EVERY platform (green runs just outran it).
           ALPHANODE_CPU_PERCENT='100',
           # dump-and-die at 1380s: just UNDER the 1500s watchdog kill, so a wedged node
           # leaves stacks behind instead of a silent TimeoutExpired — but far ABOVE any
           # honest slow round (240s here once shot a healthy node right after the DNS fix)
           ALPHANODE_HANG_DUMP='1380', ALPHANODE_HANG_DUMP_FILE=hang_file)
import time
import urllib.request
NODE_TIMEOUT = 1500


def _peek_status():
    # HTTP first (also proves the server answers); the on-disk heartbeat as fallback — while
    # the engine crunches inside native code the server thread can starve for GIL time, but
    # save_status() still lands status.json about once a second from the engine callback.
    try:
        with urllib.request.urlopen('http://127.0.0.1:8799/status.json', timeout=3) as r:
            return 'http', json.load(r)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        with open(os.path.join(tmp, 'status.json'), encoding='utf-8') as f:
            return 'disk', json.load(f)
    except Exception:                                     # noqa: BLE001
        return None, None


def _dump_file(path, title, tail=0):
    if os.path.exists(path) and os.path.getsize(path):
        text = open(path, encoding='utf-8', errors='replace').read()
        if tail:
            text = '\n'.join(text.splitlines()[-tail:])
        print(f'--- {title} ---', flush=True)
        print(text, flush=True)


# stdout/stderr -> file: a windowed exe prints into the void unless the parent hands it real
# handles; with the redirect the node's own narrative (banner, rounds, tracebacks) survives.
with open(node_log, 'w', encoding='utf-8', errors='replace') as _nl:
    proc = subprocess.Popen(cmd + ['--role', 'node'], env=env, cwd=cwd,
                            stdout=_nl, stderr=subprocess.STDOUT)
    _t0, _last = time.time(), 'no status yet'
    while proc.poll() is None:
        if time.time() - _t0 > NODE_TIMEOUT:
            proc.kill()
            proc.wait()
            _dump_file(node_log, 'node stdout/stderr')
            _dump_file(os.path.join(tmp, 'status.json'), 'status.json at kill time')
            raise SystemExit(f'node round did not finish in {NODE_TIMEOUT}s; last: {_last}')
        time.sleep(20)
        src, st = _peek_status()
        if st is None:
            _last = 'no status yet'
            print(f'[watch {int(time.time() - _t0):4d}s] status not up yet', flush=True)
        else:
            _last = (f"{src}: state={st.get('state')} round={st.get('rounds')} "
                     f"gen={st.get('gen')!r} found={st.get('found')} "
                     f"current={st.get('current')!r}")
            print(f'[watch {int(time.time() - _t0):4d}s] {_last}', flush=True)
if proc.returncode != 0:
    _dump_file(hang_file, 'hang dump (all thread stacks at the moment the watchdog fired)')
    _dump_file(node_log, 'node stdout/stderr')
    _dump_file(os.path.join(tmp, 'status.json'), 'status.json post-mortem')
    raise SystemExit(f'node exited with {proc.returncode}')
_dump_file(node_log, 'node stdout/stderr (last 15 lines)', tail=15)
lib = os.path.join(tmp, 'library.jsonl')
rows = [json.loads(l) for l in open(lib, encoding='utf-8') if l.strip()]
assert rows, 'node round produced an empty library'
assert all('base' in r and r.get('train') for r in rows), 'library rows missing metrics'
print(f'node round : ok, {len(rows)} champions in library')

# ---- 2. PDF worker: portfolio payload on stdin -> 4-page PDF (no market data needed) ----
random.seed(7)
days = [(date(2023, 1, 1) + timedelta(d)).isoformat() for d in range(200)]
tickers = ['AUSDT', 'BUSDT', 'CUSDT']
W, eq, x = [], [], 1.0
for _ in range(200):
    w = [random.gauss(0, 1) for _ in tickers]
    s = sum(abs(v) for v in w) or 1.0
    W.append([round(v / s, 5) for v in w])
    x *= 1.0 + random.gauss(0.001, 0.01)
    eq.append(round(x, 5))
out_pdf = os.path.join(tmp, 'report.pdf')
payload = {'kind': 'portfolio', 'out': out_pdf, 'title': 'AlphaNode — CI', 'subtitle': 'smoke',
           'stamp': 'ci', 'doc': {'n': 3, 'test': '2023-01-01..2023-07-19',
                                  'metrics': {'sharpe': 1.0, 'cagr': 0.5, 'dd': -0.2},
                                  'weights': {'dates': days, 'tickers': tickers, 'W': W},
                                  'equity': {'dates': days, 'combined': eq}}}
r = subprocess.run(cmd + ['--role', 'pdfreport'], input=json.dumps(payload), env=env, cwd=cwd,
                   capture_output=True, text=True, timeout=300)
last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else '{}'
doc = json.loads(last)
assert doc.get('ok'), f'pdfreport failed: stdout={last!r} stderr={r.stderr[-500:]!r}'
pages = len(re.findall(rb'/Type\s*/Page[^s]', open(out_pdf, 'rb').read()))
assert pages == 4, f'expected a 4-page PDF, got {pages}'
print(f'pdf worker : ok, 4 pages, {os.path.getsize(out_pdf) // 1024} KB')
print('CI SMOKE OK')
