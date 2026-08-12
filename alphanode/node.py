"""AlphaNode — background trading-strategy search node.

Runs the evolutionary search (evolution/ engine) in ROUNDS non-stop, accumulating discovered
champions into a library (dedup), consuming a set percentage of the machine's resources. Minimal
interface — a live status page at http://localhost:PORT.

Config — via ALPHANODE_* environment variables (see alphanode.env), layered over
evolution/config.ini (which provides the TRAIN/VAL/TEST segments, vol, penalties, etc.).
"""
import os
import sys
import json
import math
import time
import signal
import threading
import http.server
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
EVO = os.path.join(PROJ, 'evolution')
if EVO not in sys.path:
    sys.path.insert(0, EVO)

import warnings                                        # noqa: E402
warnings.filterwarnings('ignore')
import numpy as np                                     # noqa: E402
np.seterr(divide='ignore', invalid='ignore')
import pandas as pd                                     # noqa: E402

from config import load_config                         # noqa: E402
from evolution import evolve                           # noqa: E402


def env(k, d):
    return os.environ.get('ALPHANODE_' + k, d)


def iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---- node config ----
CPU_PERCENT = max(5, min(95, int(env('CPU_PERCENT', '50'))))
UNIVERSE = env('UNIVERSE', 'all')
POP = int(env('POP', '200'))
GENS = int(env('GENS', '25'))
PAUSE = float(env('PAUSE', '5'))
MAX_ROUNDS = int(env('MAX_ROUNDS', '0'))               # 0 = infinite
SEED_FROM_LIB = env('SEED_FROM_LIBRARY', '1') not in ('0', 'false', 'no', 'off')
EXPLORE_EVERY = max(1, int(env('EXPLORE_EVERY', '4')))  # every Nth round — pure exploration
STATE_DIR = env('STATE_DIR', os.path.join(HERE, 'state'))
STATUS_PORT = int(env('STATUS_PORT', '8787'))
KEEP = int(env('LEADERBOARD', '20'))
TF = (env('TF', '') or '1d').strip().lower()           # bar size; also read by load_config (ALPHANODE_TF)
FORWARD = env('FORWARD', '1').strip().lower() not in ('0', 'false', 'no', 'off')

os.makedirs(STATE_DIR, exist_ok=True)


def _resolve_seed():
    """Base seed for the whole run. ALPHANODE_SEED unset / '' / 'auto' / '0' -> derived from a
    persistent random node ID (state/node_id, minted on first run): every install walks its own
    trajectory through formula space, so two nodes never mine identical libraries. An explicit
    integer keeps the old fully reproducible behavior. Returns (seed, node_id, is_auto)."""
    nid_path = os.path.join(STATE_DIR, 'node_id')
    try:
        nid = open(nid_path).read().strip().lower()
    except OSError:
        nid = ''
    if not (len(nid) == 8 and all(c in '0123456789abcdef' for c in nid)):
        import secrets
        nid = secrets.token_hex(4)
        try:
            with open(nid_path, 'w') as f:
                f.write(nid + '\n')
        except OSError:
            pass
    raw = str(env('SEED', 'auto')).strip().lower()
    if raw in ('', 'auto', '0'):
        return int(nid, 16) % 900_000 + 1, nid, True
    return int(raw), nid, False


BASE_SEED, NODE_ID, SEED_AUTO = _resolve_seed()

# per-timeframe library/history: alphas mined on different bar sizes are NOT comparable
# (different annualization, different dynamics) and must never mix in one leaderboard.
# 1d keeps the historical file names.
_SUF = '' if TF == '1d' else f'_{TF}'
LIB = os.path.join(STATE_DIR, f'library{_SUF}.jsonl')
HIST = os.path.join(STATE_DIR, f'history{_SUF}.jsonl')  # one line per round (for the progress chart)
STATUS_FILE = os.path.join(STATE_DIR, 'status.json')
CORES = os.cpu_count() or 4
N_JOBS = max(1, round(CPU_PERCENT / 100 * CORES))      # resources -> number of parallel workers

try:
    os.nice(10)                                        # background priority (don't disturb interactive use)
except (AttributeError, OSError):
    pass

STOP = False


def _sig(*_a):
    global STOP
    STOP = True


for _s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_s, _sig)

status = {'app': 'AlphaNode', 'state': 'starting', 'started': iso(), 'updated': iso(),
          'rounds': 0, 'trials_total': 0, 'found': 0, 'cpu_percent': CPU_PERCENT, 'n_jobs': N_JOBS,
          'cores': CORES, 'universe': UNIVERSE, 'tf': TF, 'pop': POP, 'gens': GENS,
          'explore_every': EXPLORE_EVERY, 'seed_from_lib': SEED_FROM_LIB,
          'node_id': NODE_ID, 'seed_base': BASE_SEED, 'seed_auto': SEED_AUTO,
          'current': '', 'gen': '', 'best': []}


def save_status():
    status['updated'] = iso()
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def log_event(kind, text):
    """Append to the human-readable activity feed (GUI 'LIVE LOG' + status page).
    kinds: round | best | polish | warn | err — the GUI colors by them."""
    ev = status.setdefault('events', [])
    ev.append({'ts': time.strftime('%H:%M:%S'), 'k': kind, 't': str(text)})
    del ev[:-80]


# ---- library (dedup by formula) + leaderboard by fitness base=min(train,val) + round history ----
seen = set()
leaderboard = []
history = []


def _testsh(c):
    t = c.get('test')
    return t['sharpe'] if (t and t.get('sharpe') is not None) else -1e9


def _basesh(c):
    """Selection fitness = base = min(train,val) Sharpe. TEST does NOT enter selection (kept closed)."""
    b = c.get('base')
    return b if b is not None else -1e9


def _rm(m):
    if not m:
        return None
    return {k: (round(float(v), 4) if math.isfinite(float(v)) else None) for k, v in m.items()}


def load_existing():
    if os.path.exists(LIB):
        for line in open(LIB, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                seen.add(c['formula'])
                leaderboard.append(c)
            except json.JSONDecodeError:
                pass
    leaderboard.sort(key=_basesh, reverse=True)        # selection by fitness min(train,val), NOT by TEST
    del leaderboard[KEEP:]
    if os.path.exists(HIST):
        for line in open(HIST, encoding='utf-8'):
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # resume the round counter from HISTORY (every round is logged there), not from the trimmed
    # top-KEEP leaderboard: evolve() is seed-deterministic, so rewinding the counter to a stale
    # leaderboard round would replay every round since with identical seeds for zero new alphas.
    status['rounds'] = max(max((e.get('round', 0) for e in history), default=0),
                           max((c.get('round', 0) for c in leaderboard), default=0))
    try:                                               # keep the lifetime trials counter across restarts
        with open(STATUS_FILE, encoding='utf-8') as f:
            status['trials_total'] = int(json.load(f).get('trials_total', 0) or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    status['found'] = len(seen)
    status['best'] = leaderboard[:KEEP]
    if leaderboard:                                    # honest champion metrics right at startup
        ch = leaderboard[0]
        bb, bt = _basesh(ch), _testsh(ch)
        status['best_base'] = round(bb, 3) if bb > -1e8 else None
        status['best_test'] = round(bt, 3) if bt > -1e8 else None
    status['history'] = history[-300:]


def champions_from_hof(hof):
    return [{'rank': i, 'formula': h['canon'], 'size': h['size'], 'base': round(h['base'], 3),
             'train': _rm(h.get('train')), 'val': _rm(h.get('val')), 'test': _rm(h.get('test')),
             'blocks': h.get('blocks'), 'eff_n': h.get('eff_n'),   # robust-fitness evidence
             'origin': h.get('origin', 'ga')}
            for i, h in enumerate(hof)]


# ---- override ANY search parameter via ALPHANODE_* (empty/unset -> taken from config.ini) ----
def _envset(name):
    v = os.environ.get('ALPHANODE_' + name)
    return v if v not in (None, '') else None


def _override(cfg, key, name, cast):
    v = _envset(name)
    if v is not None:
        try:
            cfg[key] = cast(v)
        except ValueError:
            pass


def _apply_segments(cfg):
    order = ('TRAIN_START', 'VAL_START', 'TEST_START', 'TEST_END')
    raw = {k: _envset(k) for k in order}
    if not any(raw.values()):
        return
    sp = cfg['splits']
    cur = [sp['train'][0], sp['val'][0], sp['test'][0], sp['test'][1]]
    tr, va, te, en = [pd.Timestamp(raw[k], tz='UTC') if raw[k] else cur[i]
                      for i, k in enumerate(order)]
    cfg['splits'] = {'train': (tr, va), 'val': (va, te), 'test': (te, en)}
    cfg['start'] = tr.tz_localize(None).to_pydatetime()
    cfg['end'] = en.tz_localize(None).to_pydatetime()


def _apply_overrides(cfg):
    _override(cfg, 'vol', 'TARGET_VOL', float)
    _override(cfg, 'exec', 'EXEC_COST', float)
    _override(cfg, 'max_depth', 'MAX_DEPTH', int)
    _override(cfg, 'max_size', 'MAX_SIZE', int)
    _override(cfg, 'tourn', 'TOURNAMENT', int)
    _override(cfg, 'elitism', 'ELITISM', int)
    _override(cfg, 'random_inject', 'RANDOM_INJECT', int)
    _override(cfg, 'cx_prob', 'CROSSOVER_PROB', float)
    _override(cfg, 'parsimony', 'PARSIMONY', float)
    _override(cfg, 'corr_thresh', 'CORR_THRESHOLD', float)
    _override(cfg, 'corr_penalty', 'CORR_PENALTY', float)
    _override(cfg, 'hof_cap', 'HOF_CAPACITY', int)
    _override(cfg, 'fit_blocks', 'FIT_BLOCKS', int)    # robust fitness; 0 = legacy min(train,val)
    _apply_segments(cfg)


def build_cfg(seed, seeds=None):
    cfg = load_config()                                # segments/vol/penalties from evolution/config.ini
    if UNIVERSE.lower() not in ('all', '*', ''):
        cfg['instruments'] = [x.strip().upper() for x in UNIVERSE.split(',') if x.strip()]
    cfg.update(pop=POP, gens=GENS, seed=seed, n_jobs=N_JOBS)
    _apply_overrides(cfg)                              # target_vol, genome, GA, fitness, date segments
    if seeds:                                          # warm-start: seed with the best from the library
        cfg['seed_formulas'] = list(seeds)
    return cfg




# ---- minimal status server (stdlib) ----
def render_html():
    rows = ''.join(
        f"<tr><td>{i + 1}</td>"
        f"<td class=t>{('%+.2f' % _basesh(c)) if _basesh(c) > -1e8 else '—'}</td>"
        f"<td>{('%+.2f' % _testsh(c)) if _testsh(c) > -1e8 else '—'}</td>"
        f"<td class=f>{c['formula']}</td></tr>"
        for i, c in enumerate(leaderboard[:KEEP]))
    evs = status.get('events') or []
    ev_lines = ''.join(
        f"<div class='e {e.get('k', '')}'><span>{e.get('ts', '')}</span>{e.get('t', '')}</div>"
        for e in reversed(evs[-16:]))
    adv_log = (f"<div class=card style='margin-bottom:16px'>"
               f"<div class=k style='margin-bottom:6px'>live log — what the node is doing</div>"
               f"{ev_lines}</div>") if ev_lines else ''
    fwd = status.get('forward') or []
    fwd_rows = ''.join(
        f"<tr><td class=f>{e['id']}</td><td>{e.get('tf', '1d')}</td><td>{e['steps']}</td>"
        f"<td>{e['ret'] * 100:+.1f}%</td>"
        f"<td>{('%+.2f' % e['sharpe']) if e['sharpe'] is not None else '—'}</td></tr>"
        for e in fwd)
    fwd_card = (f"<div class=card style='margin-bottom:16px'>"
                f"<div class=k style='margin-bottom:8px'>forward track — append-only paper steps "
                f"(stepped by this node; no GUI needed)</div>"
                f"<div class=tw><table><thead><tr><th class=f>id</th><th>tf</th><th>steps</th>"
                f"<th>return</th><th>sharpe</th></tr></thead><tbody>{fwd_rows}</tbody></table>"
                f"</div></div>" if fwd_rows else '')
    # Visual language modeled on nixtla.io: warm paper background, near-black ink, hairline
    # borders, flat white cards, uppercase mono micro-labels, periwinkle + orange accents.
    # Self-contained on purpose (system font stacks, no CDN): the node may run offline.
    return f"""<!doctype html><meta charset=utf-8><title>AlphaNode</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{{--paper:#f6f4f0;--card:#ffffff;--ink:#222121;--mut:#6f6b66;--line:#dedddd;--soft:#eae9e6;
--acc:#7d8cff;--accsoft:#bfd1ff;--orange:#f99c00;--green:#1e7f4e;--red:#c14b36}}
@media(prefers-color-scheme:dark){{:root{{--paper:#262421;--card:#2e2b27;--ink:#f6f4f0;--mut:#a5a09a;
--line:#3a3733;--soft:#35322e;--accsoft:#4a5285;--green:#5abd8c;--red:#e57a63}}}}
*{{box-sizing:border-box}}
body{{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--paper);
color:var(--ink);margin:0 auto;padding:44px 30px 60px;max-width:1180px}}
h1{{margin:0;font-size:27px;font-weight:600;letter-spacing:-.02em}}
.sub{{color:var(--mut);margin:4px 0 26px;font-size:14px}}
.k{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;letter-spacing:.09em;
text-transform:uppercase;color:var(--mut)}}
b{{color:var(--ink);font-weight:600}}
.grid{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}}
.grid .card{{min-width:150px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px}}
.num{{font-size:20px;font-weight:600;letter-spacing:-.01em}}
table{{width:100%;border-collapse:collapse}}
td,th{{padding:7px 10px;border-bottom:1px solid var(--soft);text-align:right;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
tr:last-child td{{border-bottom:none}}
th{{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);font-weight:500}}
td.f,th.f{{text-align:left;color:var(--ink)}} td.t{{color:var(--acc);font-weight:600}}
.dot{{width:9px;height:9px;border-radius:50%;background:var(--acc);display:inline-block;
margin-right:10px;animation:p 1.2s infinite}}
@keyframes p{{50%{{opacity:.3}}}}
.gen{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mut);
margin:0 0 16px;white-space:pre-wrap}}
.tw{{overflow-x:auto}} .tw td{{white-space:nowrap}}
.e{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;padding:2.5px 0;
white-space:pre-wrap;word-break:break-word;color:var(--mut)}}
.e span{{color:var(--accsoft);margin-right:10px}} .e.best{{color:var(--green)}}
.e.round{{color:var(--ink)}} .e.polish{{color:var(--acc)}} .e.err,.e.warn{{color:var(--orange)}}
</style>
<h1><span class=dot></span>AlphaNode <span style="color:var(--mut);font-weight:400;font-size:14px">— {status['state']} · node {status.get('node_id', '—')}</span></h1>
<p class=sub>background alpha-search node · page refreshes itself</p>
<div class=grid>
  <div class=card><div class=k>rounds</div><div class=num>{status['rounds']}</div></div>
  <div class=card><div class=k>formulas tried</div><div class=num>{status['trials_total']:,}</div></div>
  <div class=card><div class=k>alphas found</div><div class=num>{len(seen)}</div></div>
  <div class=card><div class=k>resources</div><div class=num>{status['cpu_percent']}%</div><span class=k>{status['n_jobs']}/{status['cores']} cores</span></div>
  <div class=card><div class=k>universe</div><div class=num>{status['universe']}</div><span class=k>pop {status['pop']} · gens {status['gens']}</span></div>
</div>
<div class=gen>{status.get('current','')} &nbsp; {status.get('gen','')}</div>
{adv_log}
{fwd_card}
<div class=card><div class=k style="margin-bottom:8px">best by fitness min(train,val) · TEST — honest held-out (read-only, does NOT enter selection)</div>
<div class=tw><table><thead><tr><th>#</th><th>fitness</th><th>TEST (OOS)</th><th class=f>formula</th></tr></thead><tbody>{rows}</tbody></table></div></div>
<script>setTimeout(()=>location.reload(),4000)</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip('/') == '/status.json':
            body = json.dumps(status, ensure_ascii=False, default=str).encode()
            ctype = 'application/json'
        else:
            body = render_html().encode()
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    try:
        http.server.ThreadingHTTPServer(('0.0.0.0', STATUS_PORT), Handler).serve_forever()
    except OSError as e:
        print('status server off:', e)


# ---- forward track, headless ----
def _fwd_summary(ft, track):
    out = []
    for e in track['entries']:
        if e.get('archived'):
            continue
        m = ft.metrics(e)
        out.append({'id': e['id'], 'tf': e.get('tf', '1d'), 'steps': m['days'],
                    'ret': m['ret'], 'sharpe': m['sharpe']})
    return out


def forward_loop():
    """Step the forward track WITHOUT the GUI. Historically only the desktop app advanced the
    enrolled strategies (its 5-minute tick), so a server/Docker node silently froze the honest
    forward test the moment the window closed. Same cadence and the same code path as the GUI
    (forward_track.is_due / step_all — append-only, closed bars only, 2-bar lag), so the two
    never double-step: whoever wakes up first appends the bar, the other sees it as done.
    Disable with ALPHANODE_FORWARD=0."""
    import forward_track as ft
    while not STOP:
        try:
            track = ft.load_track()
            active = [e for e in track['entries'] if not e.get('archived')]
            due = [e for e in active if ft.is_due(e)]
            if due:
                log_event('round', f'forward track: {len(due)}/{len(active)} entries have a '
                                   f'new closed bar — stepping…')
                ft.step_all(log=lambda m: log_event('polish', f'[forward] {m}'))
                track = ft.load_track()
            status['forward'] = _fwd_summary(ft, track)
            save_status()
        except Exception as ex:                        # noqa: BLE001 — never kill the loop
            log_event('warn', f'forward step failed: {type(ex).__name__}: {ex}')
        for _ in range(300):                           # 5 min, responsive to shutdown
            if STOP:
                return
            time.sleep(1)


# ---- main loop ----
_last_save = [0.0]


def _cb(msg):
    m = str(msg).rstrip()
    ms = m.strip()
    if ms.startswith('★'):
        log_event('best', ms)
    elif 'window polish' in ms:
        log_event('polish', ms)
    elif ms.startswith('WARNING'):
        log_event('warn', ms)
    else:                                              # per-generation progress -> the live ticker
        status['gen'] = m
    now = time.time()
    if now - _last_save[0] > 1.0:                      # live progress for GUI/page (throttle 1s)
        _last_save[0] = now
        save_status()
    if STOP:
        raise KeyboardInterrupt('stop requested')


def ensure_data():
    """First start with no market data next to the node: download a starter universe of
    10 liquid majors (BTC, ETH, SOL, XRP, …) at the active timeframe so the search can run."""
    path = load_config()['data']
    if os.path.exists(path):
        return
    if PROJ not in sys.path:                           # fetch_data.py lives at the repo root
        sys.path.insert(0, PROJ)
    import fetch_data
    names = ', '.join(s.replace('USDT', '') for s in fetch_data.DEFAULT_SYMBOLS)
    print(f'no market data at {path} — downloading a starter universe of 10 majors '
          f'({names}) as {TF} candles…', flush=True)
    rc = fetch_data.run(path, interval=TF, symbols=list(fetch_data.DEFAULT_SYMBOLS))
    if rc != 0 or not os.path.exists(path):
        raise SystemExit(f'✗ data bootstrap failed (code {rc}) — check the internet connection '
                         'or run fetch_data.py manually')
    print('✓ starter data ready', flush=True)


def main():
    ensure_data()
    load_existing()
    c0 = build_cfg(BASE_SEED)
    status['target_vol'] = c0.get('vol')                     # effective target vol (env or config.ini)
    threading.Thread(target=serve, daemon=True).start()
    if FORWARD:
        threading.Thread(target=forward_loop, daemon=True).start()
    print(f'AlphaNode [{NODE_ID}]: {CPU_PERCENT}% -> {N_JOBS}/{CORES} cores | universe={UNIVERSE} '
          f'tf={TF} pop={POP} gens={GENS} | status: http://localhost:{STATUS_PORT}')
    if SEED_AUTO:
        log_event('i', f'node {NODE_ID}: unique search — base seed {BASE_SEED} derived from this '
                       f'install\'s node ID; no two nodes mine the same library '
                       f'(set ALPHANODE_SEED=<int> for a reproducible run)')
    else:
        log_event('i', f'node {NODE_ID}: fixed seed {BASE_SEED} — reproducible run '
                       f'(ALPHANODE_SEED=auto for a per-install unique search)')
    if SEED_FROM_LIB and EXPLORE_EVERY == 1:
        # rnd % 1 != 0 is never true -> the refine branch below is unreachable
        print('WARNING: explore_every=1 makes EVERY round a from-scratch exploration — '
              'warm-start refinement of library champions never runs. Set explore_every to 3-4.')
    status['state'] = 'running'
    save_status()

    rnd = status['rounds']
    refine_explained = False                             # the warm-start lesson is logged once
    while not STOP and (MAX_ROUNDS == 0 or rnd < MAX_ROUNDS):
        rnd += 1
        seed = BASE_SEED + rnd
        # refine on the best from the library (warm-start); periodically — pure exploration
        refine = SEED_FROM_LIB and bool(leaderboard) and (rnd % EXPLORE_EVERY != 0)
        seeds = [c['formula'] for c in leaderboard] if refine else None
        mode = 'refining best' if refine else 'exploring new'
        status['mode'] = mode
        status['current'] = f'round {rnd}: {mode} (seed {seed})…'
        if refine:
            extra = ('; evolution mutates around what already works '
                     f'(1 round in {EXPLORE_EVERY} explores from scratch)'
                     if not refine_explained else '')
            refine_explained = True                     # the lesson once — not every 2nd round
            log_event('round', f'▶ round {rnd}: refine — improving {len(seeds)} champions '
                               f'from the library{extra}')
        else:
            log_event('round', f'▶ round {rnd}: explore — a fresh random population, '
                               f'hunting new formula families')
        save_status()
        t0 = time.time()
        cfg = build_cfg(seed, seeds)
        try:
            hof, _hist, cache = evolve(cfg, log=_cb)
        except KeyboardInterrupt:
            break
        except Exception as e:                         # noqa: BLE001
            status['current'] = f'round {rnd}: error {type(e).__name__}: {e}'
            log_event('err', f'✗ round {rnd} failed: {type(e).__name__}: {e}')
            save_status()
            time.sleep(PAUSE)
            continue

        new = 0
        with open(LIB, 'a', encoding='utf-8') as f:
            for c in champions_from_hof(hof):
                if c['formula'] in seen:
                    continue
                seen.add(c['formula'])
                c['round'], c['ts'] = rnd, iso()
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
                leaderboard.append(c)
                new += 1
        leaderboard.sort(key=_basesh, reverse=True)    # champion = best by min(train,val); TEST closed
        del leaderboard[KEEP:]
        champ = leaderboard[0] if leaderboard else None
        bb = _basesh(champ) if champ else None          # optimized fitness
        bt = _testsh(champ) if champ else None          # honest held-out OOS of the same champion (read-only)
        bb_val = round(bb, 3) if (bb is not None and bb > -1e8) else None
        bt_val = round(bt, 3) if (bt is not None and bt > -1e8) else None
        bb_s = f'{bb_val:+.2f}' if bb_val is not None else '—'
        bt_s = f'{bt_val:+.2f}' if bt_val is not None else '—'
        entry = {'round': rnd, 'best_base': bb_val, 'best_test': bt_val,
                 'found': len(seen), 'mode': mode, 'ts': iso()}
        history.append(entry)
        try:
            with open(HIST, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError:
            pass
        status.update(rounds=rnd, trials_total=status['trials_total'] + len(cache),
                      found=len(seen), best=leaderboard[:KEEP], best_base=bb_val, best_test=bt_val,
                      history=history[-300:],
                      current=f'round {rnd} done [{mode}]: +{new} new · fitness {bb_s} · '
                              f'TEST(OOS) {bt_s} · {time.time()-t0:.0f}s')
        champs_s = (f'+{new} champion{"s" if new != 1 else ""} kept'
                    if new else 'none kept — the library held its bar')
        log_event('round', f'✓ round {rnd} · {time.time() - t0:.0f}s · {len(cache):,} formulas '
                           f'tried · {champs_s} · best fitness {bb_s} · held-out TEST {bt_s}')
        save_status()
        print(status['current'])

        for _ in range(int(PAUSE * 2)):                # interruptible pause
            if STOP:
                break
            time.sleep(0.5)

    status['state'] = 'stopped'
    save_status()
    print('AlphaNode stopped.')


if __name__ == '__main__':
    main()
