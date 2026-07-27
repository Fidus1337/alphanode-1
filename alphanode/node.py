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
BASE_SEED = int(env('SEED', '1'))
PAUSE = float(env('PAUSE', '5'))
MAX_ROUNDS = int(env('MAX_ROUNDS', '0'))               # 0 = infinite
SEED_FROM_LIB = env('SEED_FROM_LIBRARY', '1') not in ('0', 'false', 'no', 'off')
EXPLORE_EVERY = max(1, int(env('EXPLORE_EVERY', '4')))  # every Nth round — pure exploration
STATE_DIR = env('STATE_DIR', os.path.join(HERE, 'state'))
STATUS_PORT = int(env('STATUS_PORT', '8787'))
KEEP = int(env('LEADERBOARD', '20'))
TF = (env('TF', '') or '1d').strip().lower()           # bar size; also read by load_config (ALPHANODE_TF)

os.makedirs(STATE_DIR, exist_ok=True)
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
          'current': '', 'gen': '', 'best': []}


def save_status():
    status['updated'] = iso()
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


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
    llm_lib = 0
    if os.path.exists(LIB):
        for line in open(LIB, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                seen.add(c['formula'])
                llm_lib += (c.get('origin') == 'llm')
                leaderboard.append(c)
            except json.JSONDecodeError:
                pass
    # cumulative advisor footprint: consults/injected count this session, lib_llm the whole library
    status['advisor'] = {'consults': 0, 'injected': 0, 'lib_llm': llm_lib}
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
             'origin': h.get('origin', 'ga')}             # 'llm' = proposed by the advisor
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
        f"<td class=f>{'🧠 ' if c.get('origin') == 'llm' else ''}{c['formula']}</td></tr>"
        for i, c in enumerate(leaderboard[:KEEP]))
    adv = status.get('advisor') or {}
    adv_card = ''
    if adv.get('consults') or adv.get('lib_llm'):
        adv_card = (f"<div class=card><div class=k>🧠 LLM advisor</div>"
                    f"<b>{adv.get('consults', 0)}</b> consults · {adv.get('injected', 0)} injected "
                    f"· {adv.get('lib_llm', 0)} champions</div>")
    adv_lines = ''.join(f'<div>{ln}</div>' for ln in status.get('advisor_log', [])[-4:])
    adv_log = f'<div class=gen>{adv_lines}</div>' if adv_lines else ''
    return f"""<!doctype html><meta charset=utf-8><title>AlphaNode</title>
<style>body{{font:14px system-ui;background:#0f1115;color:#d7dce3;margin:0;padding:26px;max-width:1100px}}
h1{{margin:0 0 2px;font-size:20px}} .sub{{color:#8a93a2;margin:0 0 18px}} .k{{color:#8a93a2;font-size:12px}}
b{{color:#fff}} .grid{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}}
.card{{background:#171a21;border:1px solid #232833;border-radius:12px;padding:12px 16px}}
table{{width:100%;border-collapse:collapse}} td,th{{padding:6px 8px;border-bottom:1px solid #232833;text-align:right;font-size:12px}}
th{{color:#8a93a2}} td.f,th.f{{text-align:left;font-family:ui-monospace,monospace;color:#cbd5e1}} td.t{{color:#4ade80}}
.dot{{width:9px;height:9px;border-radius:50%;background:#4ade80;display:inline-block;margin-right:7px;animation:p 1.1s infinite}}
@keyframes p{{50%{{opacity:.35}}}} .gen{{font-family:ui-monospace,monospace;font-size:11px;color:#8a93a2;margin:0 0 14px}}</style>
<h1><span class=dot></span>AlphaNode <span style="color:#8a93a2;font-weight:400;font-size:13px">— {status['state']}</span></h1>
<p class=sub>background alpha-search node · page refreshes itself</p>
<div class=grid>
  <div class=card><div class=k>rounds</div><b style="font-size:18px">{status['rounds']}</b></div>
  <div class=card><div class=k>formulas tried</div><b style="font-size:18px">{status['trials_total']:,}</b></div>
  <div class=card><div class=k>alphas found</div><b style="font-size:18px">{len(seen)}</b></div>
  <div class=card><div class=k>resources</div><b>{status['cpu_percent']}%</b> · {status['n_jobs']}/{status['cores']} cores</div>
  <div class=card><div class=k>universe</div><b>{status['universe']}</b> · pop {status['pop']} gens {status['gens']}</div>
  {adv_card}
</div>
<div class=gen>{status.get('current','')} &nbsp; {status.get('gen','')}</div>
{adv_log}
<div class=card><div class=k style="margin-bottom:8px">best by fitness min(train,val) · TEST — honest held-out (read-only, does NOT enter selection)</div>
<table><thead><tr><th>#</th><th>fitness</th><th>TEST (OOS)</th><th class=f>formula</th></tr></thead><tbody>{rows}</tbody></table></div>
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


# ---- main loop ----
_last_save = [0.0]


def _cb(msg):
    m = str(msg)
    status['gen'] = m
    if 'advisor' in m:                                 # consults + proposals -> a rolling trace
        alog = status.setdefault('advisor_log', [])    # the GUI/status page shows what the LLM did
        alog.append(m.strip())
        del alog[:-8]
    now = time.time()
    if now - _last_save[0] > 1.0:                      # live progress for GUI/page (throttle 1s)
        _last_save[0] = now
        save_status()
    if STOP:
        raise KeyboardInterrupt('stop requested')


def main():
    load_existing()
    status['target_vol'] = build_cfg(BASE_SEED).get('vol')   # effective target vol (env or config.ini)
    threading.Thread(target=serve, daemon=True).start()
    print(f'AlphaNode: {CPU_PERCENT}% -> {N_JOBS}/{CORES} cores | universe={UNIVERSE} tf={TF} '
          f'pop={POP} gens={GENS} | status: http://localhost:{STATUS_PORT}')
    if SEED_FROM_LIB and EXPLORE_EVERY == 1:
        # rnd % 1 != 0 is never true -> the refine branch below is unreachable
        print('WARNING: explore_every=1 makes EVERY round a from-scratch exploration — '
              'warm-start refinement of library champions never runs. Set explore_every to 3-4.')
    status['state'] = 'running'
    save_status()

    rnd = status['rounds']
    while not STOP and (MAX_ROUNDS == 0 or rnd < MAX_ROUNDS):
        rnd += 1
        seed = BASE_SEED + rnd
        # refine on the best from the library (warm-start); periodically — pure exploration
        refine = SEED_FROM_LIB and bool(leaderboard) and (rnd % EXPLORE_EVERY != 0)
        seeds = [c['formula'] for c in leaderboard] if refine else None
        mode = 'refining best' if refine else 'exploring new'
        status['mode'] = mode
        status['current'] = f'round {rnd}: {mode} (seed {seed})…'
        save_status()
        t0 = time.time()
        cfg = build_cfg(seed, seeds)
        try:
            hof, _hist, cache = evolve(cfg, log=_cb)
        except KeyboardInterrupt:
            break
        except Exception as e:                         # noqa: BLE001
            status['current'] = f'round {rnd}: error {type(e).__name__}: {e}'
            save_status()
            time.sleep(PAUSE)
            continue

        new, new_llm = 0, 0
        with open(LIB, 'a', encoding='utf-8') as f:
            for c in champions_from_hof(hof):
                if c['formula'] in seen:
                    continue
                seen.add(c['formula'])
                c['round'], c['ts'] = rnd, iso()
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
                leaderboard.append(c)
                new += 1
                new_llm += (c.get('origin') == 'llm')
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
        # advisor footprint of the round -> status line, cumulative counters, round history
        astats = cfg.pop('advisor_stats', None)
        llm_s = ''
        if astats and astats.get('calls'):
            adv = status.get('advisor') or {'consults': 0, 'injected': 0, 'lib_llm': 0}
            adv['consults'] += astats['calls']
            adv['injected'] += astats['injected']
            adv['lib_llm'] += new_llm
            status['advisor'] = adv
            entry['llm'] = {'calls': astats['calls'], 'injected': astats['injected'],
                            'hof': astats['hof_llm'], 'new_champs': new_llm}
            llm_s = (f' · LLM: {astats["calls"]} consult{"s" if astats["calls"] > 1 else ""}, '
                     f'{astats["injected"]} injected, {astats["hof_llm"]}/{astats["hof_total"]} HoF')
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
                              f'TEST(OOS) {bt_s}{llm_s} · {time.time()-t0:.0f}s')
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
