"""AlphaNode CLI — control the strategy-search node without a GUI (for Docker/server/ssh).

    python alphanode/cli.py run [flags]      # start continuous search (foreground, log to stdout)
    python alphanode/cli.py fetch [flags]    # download fresh Binance data
    python alphanode/cli.py top [flags]      # top alphas found in the library (table in the terminal)
    python alphanode/cli.py status           # current node state (rounds, best)
    python alphanode/cli.py export [flags]    # build a paper-trading bundle from a formula/rank

Everything configurable in the GUI is here as flags too; an unset flag = taken from ALPHANODE_*/config.ini.
State (library, status) is read from ALPHANODE_STATE_DIR (in Docker — /data).
"""
import os
import sys
import json
import pickle
import difflib
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import apppaths                                          # noqa: E402
# resource root (dev — the repo, frozen — the bundle): where fetch_data.py and quantpylib/ live
for _p in (apppaths.RES_ROOT, apppaths.engine_dir()):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def _state_dir():
    return os.environ.get('ALPHANODE_STATE_DIR') or apppaths.state_dir()


def _data_path():
    return os.environ.get('ALPHANODE_DATA') or apppaths.data_path()


def _testsh(c):
    t = c.get('test') if isinstance(c.get('test'), dict) else {}
    return t.get('sharpe')


# ---- run: flag -> ALPHANODE_* variable (empty flag left untouched) --------------------------
_ENVMAP = [
    ('cpu', 'CPU_PERCENT'), ('universe', 'UNIVERSE'), ('pop', 'POP'), ('gens', 'GENS'),
    ('seed', 'SEED'), ('pause', 'PAUSE'), ('port', 'STATUS_PORT'), ('state_dir', 'STATE_DIR'),
    ('max_rounds', 'MAX_ROUNDS'), ('leaderboard', 'LEADERBOARD'), ('explore_every', 'EXPLORE_EVERY'),
    ('seed_from_library', 'SEED_FROM_LIBRARY'), ('target_vol', 'TARGET_VOL'), ('exec_cost', 'EXEC_COST'),
    ('max_depth', 'MAX_DEPTH'), ('max_size', 'MAX_SIZE'), ('tournament', 'TOURNAMENT'),
    ('elitism', 'ELITISM'), ('random_inject', 'RANDOM_INJECT'), ('crossover_prob', 'CROSSOVER_PROB'),
    ('parsimony', 'PARSIMONY'), ('corr_threshold', 'CORR_THRESHOLD'), ('corr_penalty', 'CORR_PENALTY'),
    ('hof_capacity', 'HOF_CAPACITY'), ('train_start', 'TRAIN_START'), ('val_start', 'VAL_START'),
    ('test_start', 'TEST_START'), ('test_end', 'TEST_END'), ('data', 'DATA'), ('config_ini', 'CONFIG_INI'),
]


def cmd_run(args):
    for flag, envk in _ENVMAP:
        v = getattr(args, flag, None)
        if v is not None and v != '':
            os.environ['ALPHANODE_' + envk] = str(v)
    os.environ.setdefault('ALPHANODE_DATA', _data_path())     # shared snapshot with status/export
    import node                                                # env is read at import
    node.main()


def cmd_fetch(args):
    out = args.out or _data_path()
    argv = ['fetch', '--top', str(args.top), '--min-years', str(args.min_years), '--out', out]
    if args.start:
        argv += ['--start', args.start]
    if args.end:
        argv += ['--end', args.end]
    if args.quote:
        argv += ['--quote', args.quote]
    argv += ['--concurrency', str(args.concurrency), '--timeout', str(args.timeout)]
    import fetch_data
    sys.argv = argv
    fetch_data.main()                                         # it calls os._exit() itself


# ---- top: rank the library (like the GUI leaderboard) --------------------------------------
def _load_library(state_dir):
    path = os.path.join(state_dir, 'library.jsonl')
    rows = []
    try:
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows, path


def _rank(rows, sort, min_test, n, diverse):
    keyf = _testsh if sort == 'test' else (lambda c: c.get('base'))
    rows = [c for c in rows if keyf(c) is not None]
    if min_test is not None:
        rows = [c for c in rows if _testsh(c) is not None and _testsh(c) > min_test]
    rows.sort(key=keyf, reverse=True)
    if not diverse:
        return rows[:n]
    kept = []                                                 # one alpha per family (<0.80 similarity)
    for c in rows[:600]:
        f = c.get('formula', '')
        if all(difflib.SequenceMatcher(None, f, k.get('formula', '')).ratio() < 0.80 for k in kept):
            kept.append(c)
        if len(kept) >= n:
            break
    return kept


def _fmt(v):
    return f'{v:+.2f}' if isinstance(v, (int, float)) else '—'


def cmd_top(args):
    rows, path = _load_library(_state_dir())
    if not rows:
        print(f'library empty or not found: {path}')
        return
    picked = _rank(rows, args.sort, args.min_test, args.n, not args.all)
    try:
        width = int(os.environ.get('COLUMNS') or os.get_terminal_size().columns)
    except (OSError, ValueError):
        width = 120
    fcol = max(30, width - 26)
    order = 'TEST OOS' if args.sort == 'test' else 'fitness min(train,val)'
    note = '' if args.sort != 'test' else '   ⚠ cherry-pick on held-out (number is inflated)'
    print(f'Top-{len(picked)} by {order}{note}   ·   {path}')
    print(f'{"#":>3}  {"fitness":>7}  {"TEST":>6}  formula')
    print('─' * min(width, 100))
    for i, c in enumerate(picked, 1):
        f = c.get('formula', '')
        if len(f) > fcol:
            f = f[:fcol - 1] + '…'
        print(f'{i:>3}  {_fmt(c.get("base")):>7}  {_fmt(_testsh(c)):>6}  {f}')


def cmd_status(args):
    sf = os.path.join(_state_dir(), 'status.json')
    try:
        st = json.load(open(sf, encoding='utf-8'))
    except OSError:
        print(f'status not found ({sf}) — node not started yet?')
        return
    print(f'state     : {st.get("state", "—")}')
    print(f'universe  : {st.get("universe", "—")}   vol {st.get("target_vol", "—")}')
    print(f'resources : {st.get("cpu_percent", "?")}%  ·  {st.get("n_jobs", "?")}/{st.get("cores", "?")} cores')
    print(f'rounds    : {st.get("rounds", 0)}   ·   formulas tried: {st.get("trials_total", 0):,}')
    print(f'found     : {st.get("found", 0)}   ·   best fitness {_fmt(st.get("best_base"))}  '
          f'TEST(OOS) {_fmt(st.get("best_test"))}')
    if st.get('current'):
        print(f'now       : {st["current"]}')
    best = st.get('best', [])[:args.n]
    if best:
        print(f'\ntop-{len(best)} (by fitness):')
        for i, c in enumerate(best, 1):
            f = c.get('formula', '')
            print(f'  {i:>2}  fit {_fmt(c.get("base")):>6}  TEST {_fmt(_testsh(c)):>6}  {f[:70]}')


def cmd_export(args):
    import hashlib
    import paper_export
    rows, path = _load_library(_state_dir())
    if args.formula:
        formula = args.formula
        champ = next((c for c in rows if c.get('formula') == formula), None)
    else:
        picked = _rank(rows, args.sort, None, args.rank, diverse=False)
        if len(picked) < args.rank:
            print(f'library has fewer than {args.rank} alphas (total {len(picked)})')
            return
        champ = picked[args.rank - 1]
        formula = champ.get('formula')
    if not formula:
        print('formula not specified and not found')
        return

    if args.universe:
        tickers = [x.strip().upper() for x in args.universe.split(',') if x.strip()]
    else:
        try:
            tickers = list(pickle.load(open(_data_path(), 'rb'))[0])
        except OSError as e:
            print(f'cannot read data ({_data_path()}): {e}')
            return
    name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
    out_root = args.out or apppaths.exports_dir()
    os.makedirs(out_root, exist_ok=True)
    meta = {k: champ.get(k) for k in ('train', 'val', 'test')} if champ else None
    dest = paper_export.build_bundle(
        formula, name, tickers, float(args.target_vol), float(args.exec_cost),
        args.start, out_root, meta=meta)
    print(f'✓ bundle built: {dest}')
    print(f'  run:  cd "{dest}" && pip install -r requirements.txt && python paper_trade.py')


def build_parser():
    p = argparse.ArgumentParser(prog='alphanode', description='AlphaNode CLI (headless alpha-search node)')
    sub = p.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('run', help='start continuous search (foreground)')
    r.add_argument('--cpu', type=int, help='5..95 — share of CPU (workers = %% × cores)')
    r.add_argument('--universe', help='all or BTCUSDT,ETHUSDT,...')
    r.add_argument('--pop', type=int, help='population size per round')
    r.add_argument('--gens', type=int, help='generations per round')
    r.add_argument('--seed', type=int, help='base seed')
    r.add_argument('--pause', type=float, help='pause between rounds, sec')
    r.add_argument('--port', type=int, help='status page port (0/empty — no server)')
    r.add_argument('--state-dir', dest='state_dir', help='where to write library/status (in Docker /data)')
    r.add_argument('--max-rounds', dest='max_rounds', type=int, help='0 = infinite')
    r.add_argument('--leaderboard', type=int, help='how many best to keep in the top')
    r.add_argument('--explore-every', dest='explore_every', type=int, help='every Nth round — from scratch')
    r.add_argument('--seed-from-library', dest='seed_from_library', choices=['0', '1'],
                   help='1 = warm-start from own library')
    r.add_argument('--target-vol', dest='target_vol', type=float)
    r.add_argument('--exec-cost', dest='exec_cost', type=float)
    r.add_argument('--max-depth', dest='max_depth', type=int)
    r.add_argument('--max-size', dest='max_size', type=int)
    r.add_argument('--tournament', type=int)
    r.add_argument('--elitism', type=int)
    r.add_argument('--random-inject', dest='random_inject', type=int)
    r.add_argument('--crossover-prob', dest='crossover_prob', type=float)
    r.add_argument('--parsimony', type=float)
    r.add_argument('--corr-threshold', dest='corr_threshold', type=float)
    r.add_argument('--corr-penalty', dest='corr_penalty', type=float)
    r.add_argument('--hof-capacity', dest='hof_capacity', type=int)
    r.add_argument('--train-start', dest='train_start')
    r.add_argument('--val-start', dest='val_start')
    r.add_argument('--test-start', dest='test_start')
    r.add_argument('--test-end', dest='test_end')
    r.add_argument('--data', help='path to data.pickle')
    r.add_argument('--config-ini', dest='config_ini', help='path to config.ini')
    r.set_defaults(func=cmd_run)

    f = sub.add_parser('fetch', help='download fresh Binance data (top-N USDT perps)')
    f.add_argument('--top', type=int, default=150)
    f.add_argument('--min-years', dest='min_years', type=float, default=3.0)
    f.add_argument('--start', default=None)
    f.add_argument('--end', default=None)
    f.add_argument('--out', default=None, help='default — the active data.pickle')
    f.add_argument('--quote', default='USDT')
    f.add_argument('--concurrency', type=int, default=6)
    f.add_argument('--timeout', type=float, default=120)
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser('top', help='top alphas found in the library')
    t.add_argument('--sort', choices=['fitness', 'test'], default='fitness',
                   help='rank by fitness min(train,val) or by TEST OOS (cherry-pick!)')
    t.add_argument('--min-test', dest='min_test', type=float, default=None,
                   help='show only alphas with TEST OOS > X')
    t.add_argument('-n', type=int, default=20, help='how many rows')
    t.add_argument('--all', action='store_true', help='no family dedup (raw top)')
    t.set_defaults(func=cmd_top)

    s = sub.add_parser('status', help='current node state')
    s.add_argument('-n', type=int, default=5, help='how many best to show')
    s.set_defaults(func=cmd_status)

    e = sub.add_parser('export', help='build a paper-trading bundle')
    g = e.add_mutually_exclusive_group()
    g.add_argument('--formula', help='specific formula')
    g.add_argument('--rank', type=int, default=1, help='Nth alpha by --sort (default 1)')
    e.add_argument('--sort', choices=['fitness', 'test'], default='fitness')
    e.add_argument('--universe', help='ticker list; default — all from data.pickle')
    e.add_argument('--target-vol', dest='target_vol', type=float, default=0.25)
    e.add_argument('--exec-cost', dest='exec_cost', type=float, default=0.001)
    e.add_argument('--start', default='2019-09-05')
    e.add_argument('--out', default=None, help='where to put the bundle (default exports/)')
    e.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
