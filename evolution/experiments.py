"""Experiment registry + strategy library (cumulative, on top of champions.json).

champions.json = the "current working set" (the last run/re-scoring).
experiments/registry.jsonl = a permanent log of ALL runs (never overwritten).
Strategy library = the union of all champion formulas from the log (+ the current champions.json).

Test hygiene: TEST plays no part in selection; each re-scoring/evaluation on the test increments
the test_peeks counter — so you can see the accumulated multiple-testing debt.
"""
import os
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EXP_DIR = os.path.join(HERE, 'experiments')
REG = os.path.join(EXP_DIR, 'registry.jsonl')
PEEKS = os.path.join(EXP_DIR, 'test_peeks.json')
CHAMPS = os.path.join(HERE, 'champions.json')

# which config fields we capture in the snapshot (scalars only, all serializable)
CFG_KEYS = ['vol', 'exec', 'pop', 'gens', 'seed', 'n_jobs', 'max_depth', 'max_size',
            'tourn', 'elitism', 'random_inject', 'cx_prob', 'parsimony',
            'corr_thresh', 'corr_penalty', 'hof_cap']


def universe_key(instruments):
    return 'ALL' if not instruments else '|'.join(sorted(instruments))


def _cfg_snapshot(cfg):
    return {k: cfg[k] for k in CFG_KEYS if k in cfg}


def append_run(doc):
    """Append one run to the permanent log. Returns the id."""
    os.makedirs(EXP_DIR, exist_ok=True)
    entry = {'id': datetime.now().strftime('%Y%m%d-%H%M%S'),
             'time': datetime.now().isoformat(timespec='seconds'), **doc}
    with open(REG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry['id']


def save_champions(cfg, champions, n_trials, kind='evolve', champions_path=None):
    """Write the current set to champions.json AND append to the registry. Returns (id, path)."""
    path = champions_path or CHAMPS
    uni = cfg.get('instruments')
    doc = {
        'kind': kind,
        'universe': uni or 'ALL',
        'universe_key': universe_key(uni),
        'config': _cfg_snapshot(cfg),
        'splits': {k: [str(a), str(b)] for k, (a, b) in cfg['splits'].items()},
        'n_trials': n_trials,
        'champions': champions,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    rid = append_run(doc)
    return rid, path


def bootstrap_from_champions():
    """One-off: if the registry is empty but champions.json exists — record it into the log,
    so previous champions aren't lost on the first overwrite of champions.json."""
    if load_registry() or not os.path.exists(CHAMPS):
        return None
    try:
        doc = json.load(open(CHAMPS, encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    doc.setdefault('kind', 'imported')
    doc.setdefault('universe', doc.get('universe', 'ALL'))
    doc.setdefault('universe_key', doc.get('universe_key', 'ALL'))
    return append_run(doc)


def load_registry():
    if not os.path.exists(REG):
        return []
    out = []
    for line in open(REG, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def strategy_pool(source=None, dedup=True):
    """List of champion formulas from the log. source=None -> all; otherwise filter by universe_key
    ('ALL' or 'BTCUSDT|ETHUSDT|...'). If the log is empty — fall back to the current champions.json."""
    runs = load_registry()
    if not runs and os.path.exists(CHAMPS):
        runs = [json.load(open(CHAMPS, encoding='utf-8'))]
    pool, seen = [], set()
    for r in runs:
        if source and r.get('universe_key') != source:
            continue
        for c in r.get('champions', []):
            f = c.get('formula')
            if not f or (dedup and f in seen):
                continue
            seen.add(f)
            pool.append(f)
    return pool


def get_test_peeks():
    if os.path.exists(PEEKS):
        try:
            return json.load(open(PEEKS)).get('peeks', 0)
        except json.JSONDecodeError:
            return 0
    return 0


def bump_test_peeks(n=1):
    os.makedirs(EXP_DIR, exist_ok=True)
    cur = get_test_peeks() + n
    json.dump({'peeks': cur, 'updated': datetime.now().isoformat(timespec='seconds')},
              open(PEEKS, 'w'))
    return cur
