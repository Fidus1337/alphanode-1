"""Compile the engine core to native extensions for the shipped build (protection L1).

Exactly these five are the searchable IP — the primitive set, the genome representation,
the GA loop, the fitness evaluator and the config schema:

    primitives  genome  evolution  evaluator  config

fastsim.py MUST stay pure Python: its kernel is @njit-compiled by numba at runtime, and a
Cython-compiled fastsim would silently fall back to the ~20x slower numpy path. Glue modules
(report, evolved_strategy, experiments, timeframe) hold no secrets and stay bytecode.

Output goes to packaging/cyext/<name>.cpython-*.so, which AlphaNode.spec puts FIRST on
pathex so PyInstaller resolves these module names to the extensions instead of the .py in
evolution/. Never compiled in-place: a stale .so shadowing an edited .py in the dev tree
would hijack every dev run.

Usage:  .venv/bin/python packaging/cythonize_engine.py
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
SRC = os.path.join(PROJ, 'evolution')
WORK = os.path.join(HERE, 'build', 'cyext-src')             # .py copies + generated .c live here
OUT = os.path.join(HERE, 'cyext')                           # only the finished .so land here
MODULES = ('primitives', 'genome', 'evolution', 'evaluator', 'config')

# docstrings=False strips every engine docstring out of the binaries — they narrate the
# whole search algorithm and must not ship.
SETUP = """\
import Cython.Compiler.Options as _opts
_opts.docstrings = False
from Cython.Build import cythonize
from setuptools import setup

# The __main__ guard is LOAD-BEARING on Windows/macOS: cythonize(nthreads=...) parallelizes
# over multiprocessing, and both platforms spawn (re-import this file) instead of forking —
# an unguarded setup() re-runs in every worker and the build dies bootstrapping. Linux (fork)
# never noticed, which is exactly how it shipped broken for the other two.
if __name__ == '__main__':
    setup(
        ext_modules=cythonize(
            {mods!r},
            compiler_directives={{'language_level': 3}},
            nthreads=4,
        ),
        script_args=['build_ext', '--build-lib', {out!r}],
    )
"""


def main():
    for d in (WORK, OUT):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    for m in MODULES:
        shutil.copy2(os.path.join(SRC, m + '.py'), WORK)
    with open(os.path.join(WORK, 'setup.py'), 'w', encoding='utf-8') as f:
        f.write(SETUP.format(mods=[m + '.py' for m in MODULES], out=OUT))

    print(f'== cythonizing {len(MODULES)} engine modules ==')
    subprocess.run([sys.executable, 'setup.py'], cwd=WORK, check=True)

    built = sorted(glob.glob(os.path.join(OUT, '*.so'))       # Linux/macOS
                   + glob.glob(os.path.join(OUT, '*.pyd')))   # Windows
    names = {os.path.basename(p).split('.')[0] for p in built}
    missing = [m for m in MODULES if m not in names]
    if missing:
        sys.exit(f'cythonize_engine: missing extensions for {missing}')

    # Drop the symbol/debug tables — less to read in a disassembler, smaller files.
    # Apple's strip is not GNU binutils: it has no --strip-unneeded; -x (drop local
    # symbols) is its closest equivalent for a shared object.
    if shutil.which('strip') and not sys.platform.startswith('win'):
        _flag = '-x' if sys.platform == 'darwin' else '--strip-unneeded'
        subprocess.run(['strip', _flag, *built], check=True)

    # The compiled modules must be importable and self-identify as extensions BEFORE the
    # bundle build trusts them. Run in a child so this process never holds the imports.
    probe = (
        "import sys, os; sys.path[:0] = [{out!r}, {src!r}, {proj!r}];"
        "os.environ.setdefault('ALPHANODE_CONFIG_INI', os.path.join({src!r}, 'config.ini'));"
        "import primitives, genome, evolution, evaluator, config;"
        "bad = [m.__name__ for m in (primitives, genome, evolution, evaluator, config)"
        "       if not m.__file__.endswith(('.so', '.pyd'))];"
        "assert not bad, f'imported from source, not compiled: {{bad}}';"
        "cfg = config.load_config();"
        # the mp.Pool in evolve() pickles _weval (a cyfunction, by qualname) and Node trees
        # every generation — a toolchain that breaks either must fail HERE, not in the frozen
        # app's first search round
        "import pickle, random;"
        "assert pickle.loads(pickle.dumps(evolution._weval)) is evolution._weval;"
        "t = genome.random_tree(random.Random(0), 4);"
        "assert pickle.loads(pickle.dumps(t)).canon() == t.canon();"
        "print('  probe: five extensions, load_config ok (pop', cfg['pop'], '),'"
        "      ' cyfunction+Node pickling ok')"
    ).format(out=OUT, src=SRC, proj=PROJ)
    subprocess.run([sys.executable, '-c', probe], check=True, cwd=HERE)

    for p in built:
        print(f'  {os.path.basename(p)}  {os.path.getsize(p) // 1024} KB')
    print(f'== ok: {OUT} ==')


if __name__ == '__main__':
    main()
