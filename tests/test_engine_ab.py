"""L1 protection invariant: the Cython-compiled engine is BIT-IDENTICAL to the sources.

The five core modules (primitives, genome, evolution, evaluator, config) ship to customers
only as .so built by packaging/cythonize_engine.py; the dev tree runs the .py sources. Any
semantic drift between the two forms — a Cython build against edited sources, a compiler
directive changing arithmetic or iteration order, a stale extension shadowing a fixed .py —
would corrupt mining results invisibly: shipped nodes would mine different champions than
the dev engine reproduces, and nothing in production would ever flag it. This file guards
that by running the SAME tiny seeded evolve in two subprocesses (one importing the sources,
one importing packaging/cyext) and requiring their champion/history payloads to be exactly
equal, float for float (JSON repr round-trips doubles, so text equality == bit identity).
"""
import glob
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig

import pytest

pytestmark = pytest.mark.slow

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYEXT = os.path.join(REPO, 'packaging', 'cyext')
ENGINE_MODULES = ('primitives', 'genome', 'evolution', 'evaluator', 'config')
DATA_PICKLE = os.path.join(REPO, 'data.pickle')
CONFIG_INI = os.path.join(REPO, 'evolution', 'config.ini')

# The worker mirrors how the shipped app resolves the engine: AlphaNode.spec puts the
# extension dir FIRST on pathex, so sys.path ordering alone selects the form under test.
# ALPHANODE_DATA / ALPHANODE_CONFIG_INI must be in the environment before any engine
# import (config reads them at import time) — the parent passes them via env.
WORKER_SOURCE = '''\
# A/B worker: one seeded mini-evolve on either the source engine or the compiled one.
# argv: <repo> <variant: src|cyx> <out_json>
import json
import os
import sys

repo, variant, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
paths = [os.path.join(repo, 'evolution'), repo]
if variant == 'cyx':
    paths.insert(0, os.path.join(repo, 'packaging', 'cyext'))
sys.path[:0] = paths

import config as C
import evolution as E
import genome as G
import primitives as P
import evaluator as EV

which = {m.__name__: ('so' if m.__file__.endswith('.so') else 'py')
         for m in (C, E, G, P, EV)}
expect = 'so' if variant == 'cyx' else 'py'
assert all(v == expect for v in which.values()), \\
    'variant %r loaded the wrong module forms: %r' % (variant, which)

cfg = C.load_config()
cfg.update(pop=24, gens=3, seed=1234, n_jobs=1, window_polish=True,
           hof_cap=8, elitism=2, random_inject=3)

hof, history, cache = E.evolve(cfg, log=lambda *a: None)

payload = {
    'hof': [{
        'canon': h['canon'],
        'base': float(h['base']),
        'train_sharpe': float(h['train_sharpe']),
        'val_sharpe': float(h['val_sharpe']),
        'test_sharpe': float(h['test']['sharpe']),
    } for h in hof],
    'history': [{k: (v if isinstance(v, (int, str)) else float(v))
                 for k, v in row.items()} for row in history],
    'cache_size': len(cache),
}
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({'variant': variant, 'modules': which, 'payload': payload}, f,
              sort_keys=True)
'''


def _missing_extensions():
    """Engine modules with no importable .so for THIS interpreter in packaging/cyext."""
    suffix = sysconfig.get_config_var('EXT_SUFFIX') or '.so'
    missing = []
    for name in ENGINE_MODULES:
        if not (os.path.exists(os.path.join(CYEXT, name + suffix))
                or glob.glob(os.path.join(CYEXT, name + '.*.so'))):
            missing.append(name)
    return missing


@pytest.fixture(scope='module')
def cyext_ready():
    """Ensure all five compiled extensions exist, building them once if needed."""
    if not _missing_extensions():
        return CYEXT
    if importlib.util.find_spec('Cython') is None:
        pytest.skip('Cython is not installed; cannot build the compiled engine')
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, 'packaging', 'cythonize_engine.py')],
            cwd=REPO, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        pytest.skip('cythonize_engine.py timed out')
    still_missing = _missing_extensions()
    if proc.returncode != 0 or still_missing:
        pytest.skip('cythonize_engine.py failed (rc=%s, missing=%s): %s'
                    % (proc.returncode, still_missing,
                       (proc.stderr or proc.stdout)[-500:]))
    return CYEXT


def _run_variant(variant, worker_path, out_path, env):
    proc = subprocess.run(
        [sys.executable, str(worker_path), REPO, variant, str(out_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        '%s worker failed (rc=%s):\n%s' % (variant, proc.returncode,
                                           (proc.stderr or proc.stdout)[-2000:]))
    with open(out_path, encoding='utf-8') as f:
        return json.load(f)


def test_compiled_engine_bit_identical_to_source_engine(cyext_ready, sandbox, tmp_path):
    """The seeded mini-evolution must produce byte-for-byte equal champions and history
    whether the engine is imported from evolution/*.py or packaging/cyext/*.so."""
    if not os.path.exists(DATA_PICKLE):
        pytest.skip('data.pickle not present; engine cannot evaluate formulas')

    worker = tmp_path / 'ab_worker.py'
    worker.write_text(WORKER_SOURCE, encoding='utf-8')

    env = dict(os.environ)
    env['ALPHANODE_DATA'] = DATA_PICKLE
    env['ALPHANODE_CONFIG_INI'] = CONFIG_INI
    env['ALPHANODE_STATE_DIR'] = str(sandbox)
    env.pop('PYTHONPATH', None)          # nothing may pre-seed either engine form

    src = _run_variant('src', worker, tmp_path / 'src.json', env)
    cyx = _run_variant('cyx', worker, tmp_path / 'cyx.json', env)

    # Each side really exercised the form it claims to (the worker asserts this too).
    assert set(src['modules'].values()) == {'py'}, src['modules']
    assert set(cyx['modules'].values()) == {'so'}, cyx['modules']

    # A run that found nothing would make the equality below vacuous.
    assert src['payload']['hof'], 'seeded run produced an empty hall of fame'
    assert len(src['payload']['history']) == 3

    # json round-trips doubles via repr (shortest form that parses back to the same bits),
    # so equal canonical dumps == bit-identical floats. NaNs serialize as 'NaN' on both
    # sides, which is exactly the tolerance we want for them.
    src_text = json.dumps(src['payload'], sort_keys=True)
    cyx_text = json.dumps(cyx['payload'], sort_keys=True)
    assert src_text == cyx_text, (
        'compiled engine diverged from the source engine on an identical seeded round — '
        'the shipped .so would mine different results than the dev tree:\n'
        'src: %s\ncyx: %s' % (src_text[:1500], cyx_text[:1500]))
