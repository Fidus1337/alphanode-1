"""Single entry point of the built application (PyInstaller).

The same executable can launch in different roles — the GUI spawns its own child processes via
`sys.executable --role <role>` (in the built form there's no plain python nearby, so we launch
ourselves):

    <exe>                      → GUI (default)
    <exe> --role node          → background search node (node.main)
    <exe> --role fetch [args]  → Binance data fetcher (fetch_data.main)
    <exe> --role runpy F [a…]  → run python file F of a paper-trade bundle (for the "run" button)

In development mode this file is not used (the GUI calls scripts through real python).
"""
import os
import sys


def _fix_std_streams():
    """In a windowed build on Windows sys.stdout/stderr = None -> print() crashes. Child roles
    (node/fetch) write a log that the parent reads through a pipe: we reconnect the stream to fd 1/2,
    otherwise to /dev/null. On Linux the streams are live, so this branch is harmless."""
    for name, fd in (('stdout', 1), ('stderr', 2)):
        if getattr(sys, name, None) is None:
            try:
                stream = os.fdopen(fd, 'w', buffering=1)
            except OSError:
                stream = open(os.devnull, 'w')
            setattr(sys, name, stream)


def _prep_path():
    """Make alphanode/, the resource root, and evolution/ importable (in the bundle and in dev)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here,):
        if p not in sys.path:
            sys.path.insert(0, p)
    import apppaths                                       # noqa: E402  (after inserting here into path)
    for p in (apppaths.RES_ROOT, os.path.join(apppaths.RES_ROOT, 'evolution')):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def _selfcheck():
    """Windowless diagnostics of the built bundle: imports, engine, data, Tk. Writes selfcheck.log to
    cwd and exits with code 0/1 (in a windowed build stdout may be unavailable — CI checks the code and
    the log). Used during build and in CI (<exe> --role selfcheck)."""
    import io
    import traceback
    buf = io.StringIO()

    def out(*a):
        line = ' '.join(str(x) for x in a)
        print(line)
        buf.write(line + '\n')

    try:
        _selfcheck_body(out)
    except Exception:                                    # noqa: BLE001
        buf.write('SELFCHECK FAILED\n' + traceback.format_exc())
        try:
            with open('selfcheck.log', 'w', encoding='utf-8') as f:
                f.write(buf.getvalue())
        except OSError:
            pass
        print(buf.getvalue(), file=sys.stderr)
        sys.exit(1)
    try:
        with open('selfcheck.log', 'w', encoding='utf-8') as f:
            f.write(buf.getvalue())
    except OSError:
        pass
    sys.exit(0)


def _selfcheck_body(out):
    import os
    import pickle
    import apppaths
    out('frozen      :', apppaths.FROZEN)
    out('res_root    :', apppaths.RES_ROOT)
    out('user_dir    :', apppaths.USER_DIR)
    out('config_ini  :', apppaths.config_ini(), os.path.exists(apppaths.config_ini()))
    dp = apppaths.data_path()
    out('data_path   :', dp, os.path.exists(dp))

    import numpy, pandas, matplotlib                      # noqa: F401
    out('numpy/pandas/mpl:', numpy.__version__, pandas.__version__, matplotlib.__version__)

    from config import load_config
    cfg = load_config()
    out('load_config : ok, instruments =', 'all' if cfg.get('instruments') is None
        else len(cfg['instruments']))

    from evaluator import build_panel                     # noqa: F401
    from evolved_strategy import make_evolved             # noqa: F401
    from quantpylib.simulator.alpha import Portfolio      # noqa: F401
    out('engine imports: ok')

    # The data fetcher role pulls the Binance wrapper (import pytz). Exercise it here so a missing
    # transitive dep (e.g. pytz) fails the build in CI instead of shipping a broken --role fetch.
    import fetch_data                                      # noqa: F401
    import signal_service                                  # noqa: F401
    out('fetch/signal imports: ok')

    # exercise the fast fitness kernel on a tiny synthetic market: forces numba to compile inside the
    # frozen process, so a bundle that failed to include numba is visible here (as a graceful numpy
    # fallback — never a crash, thanks to _run_kernel's guard). Reports which path is active.
    import fastsim
    _T, _N = 60, 3
    _C = numpy.cumprod(numpy.full((_T, _N), 1.003), axis=0)
    _R = numpy.zeros((_T, _N)); _R[1:] = _C[1:] / _C[:-1] - 1.0
    _mk = {'C': _C, 'R': _R, 'V': numpy.full((_T, _N), 0.02),
           'base_elig': numpy.ones((_T, _N), bool),
           'index': pandas.date_range('2020-01-01', periods=_T, freq='D', tz='UTC'), 'tk': ['A', 'B', 'C']}
    fastsim.fast_sim(numpy.tile(numpy.linspace(-1.0, 1.0, _N), (_T, 1)), _mk, 0.30, 0.001)
    out('fast_sim    : ok, numba', 'ACTIVE' if fastsim._kernel_jit is not None else 'fallback(numpy)')

    if os.path.exists(dp):
        with open(dp, 'rb') as f:
            tk_, _oh = pickle.load(f)
        out('dataset     :', len(tk_), 'pairs')
    else:
        out('dataset     : no embedded data.pickle (download it in the app)')

    import paper_export
    out('paper EVO   :', paper_export.EVO, os.path.isdir(paper_export.EVO))
    out('paper QUANT :', paper_export.QUANT, os.path.isdir(paper_export.QUANT))

    if os.environ.get('DISPLAY') or sys.platform.startswith('win'):
        import customtkinter as ctk
        import alphanode_gui
        # a CTk root, as main() builds: App styles it with CTk options a plain tk.Tk lacks.
        # Building the UI in BOTH themes is the point — it exercises every widget twice and would
        # catch a bundle whose customtkinter assets (themes/fonts) never made it into the build.
        r = ctk.CTk()
        r.withdraw()
        app = alphanode_gui.App(r)
        app._set_theme('dark' if app.cfg.get('theme') == 'light' else 'light')
        r.update()
        out('gui build   : ok, both themes, child_cmd(node) =',
            alphanode_gui._child_cmd('node')[:2], '…')
        out('ctk         :', ctk.__version__, '· theme assets',
            os.path.isdir(os.path.join(os.path.dirname(ctk.__file__), 'assets', 'themes')))
        r.destroy()
    else:
        out('gui build   : skipped (no DISPLAY)')
    out('SELFCHECK OK')


def main():
    import multiprocessing
    multiprocessing.freeze_support()                     # portfolio build uses a process pool
    _fix_std_streams()
    _prep_path()
    argv = sys.argv
    role = os.environ.get('ALPHANODE_ROLE')
    if len(argv) >= 3 and argv[1] == '--role':
        role = argv[2]
        del argv[1:3]                                    # leave clean argv for the child code

    if role == 'node':
        import node
        node.main()
    elif role == 'fetch':
        import fetch_data
        fetch_data.main()                                # it calls os._exit() itself
    elif role == 'portfolio':
        import portfolio_build
        portfolio_build.main()                           # combines top-N by TEST via the real engine
    elif role == 'signal':
        import signal_service
        signal_service.main()                            # local live-signal HTTP API (JSON, localhost)
    elif role == 'cli':
        import cli
        cli.main(argv[1:])                               # remaining argv -> CLI subcommands
    elif role == 'selfcheck':
        _selfcheck()
    elif role == 'runpy':
        if len(argv) < 2:
            print('runpy: no file given', file=sys.stderr)
            sys.exit(2)
        import runpy
        target = os.path.abspath(argv[1])
        sys.argv = [target] + list(argv[2:])             # as if the script itself was launched
        os.chdir(os.path.dirname(target))
        runpy.run_path(target, run_name='__main__')
    else:
        import alphanode_gui
        alphanode_gui.main()


if __name__ == '__main__':
    main()
