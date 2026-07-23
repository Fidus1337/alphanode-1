"""Frozen-bundle smoke for CI (runs on BOTH the Windows and Linux jobs).

Two checks --role selfcheck can't give:
  1. one REAL search round — exercises the multiprocessing pool + numba + the library writer
     inside the frozen exe (mp in a windowed PyInstaller build on Windows is the classic
     breakage: freeze_support / spawn re-imports);
  2. the PDF analytics worker end-to-end — JSON payload on stdin -> {"ok": true} + a 4-page
     PDF on disk, exactly the path the GUI's "PDF report" buttons take via a child process
     (windowed exe + piped stdio is the other Windows-specific risk).

    python packaging/ci_smoke.py packaging/dist/AlphaNode/AlphaNode[.exe]
    python packaging/ci_smoke.py alphanode/app_entry.py     # dev mode (script, not frozen)
"""
import json
import os
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

# ---- 1. one search round in a scratch state dir ----
env = dict(os.environ, ALPHANODE_MAX_ROUNDS='1', ALPHANODE_POP='20', ALPHANODE_GENS='2',
           ALPHANODE_PAUSE='0', ALPHANODE_STATE_DIR=tmp, ALPHANODE_STATUS_PORT='8799')
subprocess.run(cmd + ['--role', 'node'], env=env, cwd=cwd, timeout=900, check=True)
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
