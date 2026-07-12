"""AlphaNode — minimal desktop interface (Tkinter, stdlib, no dependencies).

Control panel for the background node. On the left — the FULL set of search settings (resources,
universe, population/generations, node mode, simulation/target-vol, genome, GA selection, fitness,
date segments) — everything the engine understands is tunable by hand and passed to the node via
ALPHANODE_* variables. On the right — live status, a progress chart and a leaderboard of found
alphas. Launches the node as a subprocess (node.py) and reads its state/status.json.

Run:  python alphanode/alphanode_gui.py
"""
import os
import sys
import json
import time
import queue
import signal
import pickle
import difflib
import hashlib
import threading
import subprocess
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)                             # for import apppaths on direct launch
import apppaths                                          # noqa: E402
PROJ = apppaths.PROJ
EVO = apppaths.engine_dir()
if EVO not in sys.path:
    sys.path.insert(0, EVO)                              # to pull in the engine for the equity chart
if apppaths.RES_ROOT not in sys.path:
    sys.path.insert(0, apppaths.RES_ROOT)               # for import quantpylib in the frozen build
NODE_PY = os.path.join(HERE, 'node.py')                 # dev: scripts via the real python
FETCH_PY = os.path.join(PROJ, 'fetch_data.py')
PORTFOLIO_PY = os.path.join(HERE, 'portfolio_build.py')
DATA_PICKLE = apppaths.user_data_pickle()               # where the data fetcher writes fresh data
STATE_DIR = apppaths.state_dir()
STATUS_FILE = os.path.join(STATE_DIR, 'status.json')
PORTFOLIO_JSON = os.path.join(STATE_DIR, 'portfolio.json')
PORTFOLIO_PNG = os.path.join(STATE_DIR, 'portfolio_equity.png')
SETTINGS = apppaths.settings_file()
CORES = os.cpu_count() or 4


def _child_cmd(role):
    """Command for the child process of role `role`: in the frozen build — the exe itself with
    --role, in dev — the real python with the script."""
    if apppaths.FROZEN:
        return [sys.executable, '--role', role]
    script = {'node': NODE_PY, 'fetch': FETCH_PY, 'portfolio': PORTFOLIO_PY}[role]
    return [sys.executable, '-u', script]

DEFAULTS = {
    # resources / universe
    'cpu': 50, 'universe_all': True,
    'universe_list': 'BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LINKUSDT',
    # search
    'pop': 200, 'gens': 25, 'seed': 1, 'pause': 5, 'port': 8787,
    # data
    'fetch_n': 150, 'fetch_years': 3,
    # node mode
    'explore_every': 4, 'seed_from_lib': True, 'max_rounds': 0, 'leaderboard': 20,
    # simulation
    'target_vol': 0.25, 'exec_cost': 0.001,
    # genome
    'max_depth': 6, 'max_size': 22,
    # selection (GA)
    'tournament': 5, 'elitism': 6, 'random_inject': 10, 'crossover_prob': 0.6,
    # fitness
    'parsimony': 0.010, 'corr_threshold': 0.70, 'corr_penalty': 0.5, 'hof_capacity': 15,
    # date segments (TRAIN < VAL < TEST)
    'train_start': '2019-09-05', 'val_start': '2021-11-01',
    'test_start': '2023-01-01', 'test_end': '2026-07-05',
}

# --- design palette (modern light, Linear/Stripe style) ---
BG = '#eef0f4'          # app background (cool light gray)
CARD = '#ffffff'        # cards
BORDER = '#e3e6ec'      # hairline borders
TXT = '#0f172a'         # text (slate-900)
MUT = '#64748b'         # muted (slate-500)
FAINT = '#94a3b8'       # even fainter (slate-400)
ACC = '#6366f1'         # accent (indigo-500)
ACC_HI = '#4f46e5'      # hover
ACC_DN = '#4338ca'      # pressed
ACC_SOFT = '#eef2ff'    # soft fill / row highlight (indigo-50)
POS = '#059669'         # gain (emerald-600)
NEG = '#e11d48'         # loss (rose-600)
HEAD_BG = '#f1f3f7'     # table headers / soft backgrounds
STRIPE = '#fafbfc'      # row zebra striping


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.logq = queue.Queue()
        self._panel_cache = {}                           # (instruments,start,end) -> (tk,panel,market,basket)
        self._plot_lock = threading.Lock()               # pyplot is global -> build one at a time
        self._plot_seq = 0
        self._metrics_cache = {}                          # formula -> {'long','short','win'} (on TEST)
        self._metrics_lock = threading.Lock()             # heavy computation — one at a time
        self._metrics_seq = 0                             # to discard stale background computations
        self._row_items = {}                              # formula -> table row id (to update cells)
        self._pf_proc = None                              # portfolio-build subprocess
        self._pf_img_ref = None                           # keep a ref to the equity PhotoImage (else GC)
        self._pf_doc = None                               # last portfolio result (for re-render on resize)
        self._pf_resize_after = None                      # debounce id for resize re-render
        self._pf_last_w = 0                               # last render width (skip tiny resizes)
        self._shown = []                                 # what is actually shown in the table (for clicks)
        self._lb_sort = 'base'                           # how to rank the leaderboard: base | test
        self._lb_min = None                              # threshold: show only TEST OOS > X (or None)
        self._lb_minact = 2.0                            # min trade activity (trades/asset/year on TEST)
        self._lib_cache = {'mtime': None, 'diverse': [], 'computing': False, 'dirty': False,
                           'ts': 0.0, 'sort': 'base', 'minv': None, 'minact': 2.0, 'computed': False}
        self._lb_target = 20                             # how many DISTINCT families to show
        self._tip_win = None                             # tooltip window + deferred display
        self._tip_after = None
        self.cfg = dict(DEFAULTS)
        self._load()
        self._style()
        self._build()
        self._poll()
        root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---------- settings (persist) ----------
    def _load(self):
        try:
            self.cfg.update(json.load(open(SETTINGS)))
        except Exception:
            pass

    @staticmethod
    def _gi(var, d):
        try:
            return int(float(var.get()))
        except Exception:
            return d

    @staticmethod
    def _gf(var, d):
        try:
            return float(var.get())
        except Exception:
            return d

    def _collect(self):
        d = DEFAULTS
        return dict(
            cpu=self._gi(self.v_cpu, d['cpu']),
            universe_all=bool(self.v_uniall.get()),
            universe_list=self.v_unilist.get().strip(),
            pop=self._gi(self.v_pop, d['pop']), gens=self._gi(self.v_gens, d['gens']),
            seed=self._gi(self.v_seed, d['seed']), pause=self._gi(self.v_pause, d['pause']),
            port=self._gi(self.v_port, d['port']), fetch_n=self._gi(self.v_fetchn, d['fetch_n']),
            fetch_years=self._gi(self.v_minyears, d['fetch_years']),
            explore_every=max(1, self._gi(self.v_explore, d['explore_every'])),
            seed_from_lib=bool(self.v_seedlib.get()),
            max_rounds=self._gi(self.v_maxrounds, d['max_rounds']),
            leaderboard=self._gi(self.v_leader, d['leaderboard']),
            target_vol=self._gf(self.v_vol, d['target_vol']),
            exec_cost=self._gf(self.v_exec, d['exec_cost']),
            max_depth=self._gi(self.v_depth, d['max_depth']),
            max_size=self._gi(self.v_size, d['max_size']),
            tournament=self._gi(self.v_tourn, d['tournament']),
            elitism=self._gi(self.v_elit, d['elitism']),
            random_inject=self._gi(self.v_inject, d['random_inject']),
            crossover_prob=self._gf(self.v_cx, d['crossover_prob']),
            parsimony=self._gf(self.v_pars, d['parsimony']),
            corr_threshold=self._gf(self.v_corrt, d['corr_threshold']),
            corr_penalty=self._gf(self.v_corrp, d['corr_penalty']),
            hof_capacity=self._gi(self.v_hof, d['hof_capacity']),
            train_start=self.v_train.get().strip(), val_start=self.v_val.get().strip(),
            test_start=self.v_test.get().strip(), test_end=self.v_end.get().strip(),
        )

    def _save(self):
        self.cfg.update(self._collect())
        try:
            json.dump(self.cfg, open(SETTINGS, 'w'), indent=2)
        except Exception:
            pass

    # ---------- style ----------
    def _style(self):
        import tkinter.font as tkfont
        self.root.title('AlphaNode')
        self.root.geometry('1100x860')
        self.root.minsize(980, 680)
        self.root.configure(bg=BG)

        fams = set(tkfont.families(self.root))

        def pick(prefs, dflt):
            for f in prefs:
                if f in fams:
                    return f
            return dflt
        self.UI = pick(['Inter', 'SF Pro Text', 'Helvetica Neue', 'Segoe UI', 'Ubuntu',
                        'Cantarell', 'Noto Sans', 'Roboto', 'DejaVu Sans'], 'TkDefaultFont')
        self.MONO = pick(['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', 'Menlo',
                          'Consolas', 'Ubuntu Mono', 'DejaVu Sans Mono'], 'TkFixedFont')
        for nm in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):  # nice font everywhere
            try:
                tkfont.nametofont(nm).configure(family=self.UI)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont('TkFixedFont').configure(family=self.MONO)
        except tk.TclError:
            pass

        F = self.UI
        s = ttk.Style()
        try:
            s.theme_use('clam')
        except tk.TclError:
            pass
        s.configure('.', background=BG, foreground=TXT, font=(F, 10))

        # cards: Card = transparent container, CardBox = card with a hairline border
        s.configure('Card.TFrame', background=CARD)
        s.configure('CardBox.TFrame', background=CARD, borderwidth=1, relief='solid', bordercolor=BORDER)
        s.configure('Card.TLabel', background=CARD, foreground=TXT)
        s.configure('Mut.TLabel', background=CARD, foreground=MUT)
        s.configure('Faint.TLabel', background=CARD, foreground=FAINT, font=(F, 8))
        s.configure('H.TLabel', background=CARD, foreground=FAINT, font=(F, 9, 'bold'))
        s.configure('Sec.TLabel', background=CARD, foreground=ACC, font=(F, 9, 'bold'))
        s.configure('Big.TLabel', background=CARD, foreground=TXT, font=(F, 22, 'bold'))

        # buttons: secondary (default), Accent (primary), Stop, Danger
        s.configure('TButton', background=CARD, foreground=TXT, borderwidth=1, relief='solid',
                    bordercolor=BORDER, focuscolor=CARD, padding=(10, 7), font=(F, 10))
        s.map('TButton', background=[('active', HEAD_BG), ('disabled', CARD)],
              bordercolor=[('active', FAINT)], foreground=[('disabled', FAINT)])
        s.configure('Accent.TButton', background=ACC, foreground='#ffffff', borderwidth=0,
                    focuscolor=ACC, padding=(10, 8), font=(F, 10, 'bold'))
        s.map('Accent.TButton', background=[('active', ACC_HI), ('pressed', ACC_DN), ('disabled', '#c3c7e2')])
        s.configure('Stop.TButton', background=HEAD_BG, foreground=TXT, borderwidth=1, relief='solid',
                    bordercolor=BORDER, focuscolor=HEAD_BG, padding=(10, 7))
        s.map('Stop.TButton', background=[('active', '#e6e9ef'), ('disabled', CARD)],
              foreground=[('disabled', FAINT)])
        s.configure('Danger.TButton', background=CARD, foreground=NEG, borderwidth=1, relief='solid',
                    bordercolor='#f2c9d3', focuscolor=CARD, padding=(10, 7))
        s.map('Danger.TButton', background=[('active', '#fdecef'), ('disabled', CARD)])

        # radio / check (indicator in accent color)
        s.configure('Card.TRadiobutton', background=CARD, foreground=TXT, focuscolor=CARD)
        s.map('Card.TRadiobutton', background=[('active', CARD)], indicatorcolor=[('selected', ACC)])
        s.configure('Card.TCheckbutton', background=CARD, foreground=TXT, focuscolor=CARD)
        s.map('Card.TCheckbutton', background=[('active', CARD)], indicatorcolor=[('selected', ACC)])

        # input fields / spinboxes
        for st in ('TEntry', 'TSpinbox'):
            s.configure(st, fieldbackground=CARD, background=CARD, foreground=TXT, arrowcolor=MUT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, padding=4)
            s.map(st, bordercolor=[('focus', ACC)], lightcolor=[('focus', ACC)], darkcolor=[('focus', ACC)])

        # slider / scroll
        s.configure('Horizontal.TScale', background=CARD, troughcolor='#e4e8ef', borderwidth=0)
        s.configure('Vertical.TScrollbar', background='#cdd4de', troughcolor=CARD, bordercolor=CARD,
                    arrowcolor=MUT, borderwidth=0, arrowsize=13)
        s.map('Vertical.TScrollbar', background=[('active', FAINT)])

        # table
        s.configure('Treeview', rowheight=30, fieldbackground=CARD, background=CARD,
                    foreground=TXT, borderwidth=0, font=(F, 10))
        s.configure('Treeview.Heading', font=(F, 9, 'bold'), foreground=MUT, background=HEAD_BG,
                    relief='flat', padding=(8, 8), bordercolor=BORDER)
        s.map('Treeview.Heading', background=[('active', '#e8ecf3')])
        s.map('Treeview', background=[('selected', ACC_SOFT)], foreground=[('selected', TXT)])

    def _card(self, parent):
        return ttk.Frame(parent, style='CardBox.TFrame', padding=16)

    # ---------- layout ----------
    def _build(self):
        tk.Frame(self.root, bg=ACC, height=3).pack(fill='x')            # accent bar at the top
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill='x', padx=20, pady=(16, 12))
        brand = tk.Frame(top, bg=BG)
        brand.pack(side='left')
        tk.Label(brand, text='Alpha', font=(self.UI, 18, 'bold'), bg=BG, fg=TXT).pack(side='left')
        tk.Label(brand, text='Node', font=(self.UI, 18, 'bold'), bg=BG, fg=ACC).pack(side='left')
        tk.Label(top, text='background search for trading strategies', bg=BG, fg=MUT,
                 font=(self.UI, 10)).pack(side='left', padx=(14, 0), pady=(7, 0))
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill='x')         # hairline under the header

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill='both', expand=True, padx=20, pady=16)
        body.columnconfigure(0, weight=0, minsize=250)   # width refined by the content itself (see _sync)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_settings(body)
        self._build_status(body)

    # ---------- left panel: ALL settings (scrollable) ----------
    def _build_settings(self, body):
        outer = ttk.Frame(body, style='CardBox.TFrame')
        outer.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        inner = ttk.Frame(canvas, style='Card.TFrame', padding=14)
        canvas.create_window((0, 0), window=inner, anchor='nw')

        def _sync(_e=None):     # width is set by the content ITSELF — correct even with HiDPI font scaling
            canvas.configure(width=inner.winfo_reqwidth(), scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _sync)
        self._bind_wheel(canvas)

        ttk.Label(inner, text='SEARCH SETTINGS', style='H.TLabel').pack(anchor='w', pady=(0, 10))

        # --- resources ---
        ttk.Label(inner, text='Resources (CPU share)', style='Mut.TLabel').pack(anchor='w')
        self.v_cpu = tk.IntVar(value=self.cfg['cpu'])
        self.lbl_cpu = ttk.Label(inner, text='', style='Card.TLabel', font=(self.UI, 11, 'bold'))
        self.lbl_cpu.pack(anchor='w', pady=(2, 0))
        sc = ttk.Scale(inner, from_=5, to=95, orient='horizontal', variable=self.v_cpu,
                       command=lambda e: self._cpu_lbl())
        sc.pack(fill='x', pady=(2, 12))
        self._cpu_lbl()
        cpu_tip = 'How many cores to give the search. More — faster, but higher load on the PC.'
        self._tip(self.lbl_cpu, cpu_tip)
        self._tip(sc, cpu_tip)

        # --- pairs universe ---
        ttk.Label(inner, text='Which pairs to trade', style='Mut.TLabel').pack(anchor='w')
        self.v_uniall = tk.BooleanVar(value=self.cfg['universe_all'])
        rb1 = ttk.Radiobutton(inner, text='All loaded pairs', style='Card.TRadiobutton',
                              variable=self.v_uniall, value=True, command=self._uni_toggle)
        rb1.pack(anchor='w')
        rb2 = ttk.Radiobutton(inner, text='Custom list:', style='Card.TRadiobutton',
                              variable=self.v_uniall, value=False, command=self._uni_toggle)
        rb2.pack(anchor='w')
        self.v_unilist = tk.StringVar(value=self.cfg['universe_list'])
        self.e_uni = ttk.Entry(inner, textvariable=self.v_unilist)
        self.e_uni.pack(fill='x', pady=(2, 4))
        self._uni_toggle()
        self._tip(rb1, 'Search across all downloaded pairs.')
        self._tip(rb2, 'Search only your own pairs (tickers, comma-separated).')
        self._tip(self.e_uni, 'Your pairs, comma-separated, e.g. BTCUSDT,ETHUSDT,SOLUSDT.')

        # --- market data (Binance) ---
        ttk.Label(inner, text='MARKET DATA (BINANCE)', style='Sec.TLabel').pack(anchor='w', pady=(14, 1))
        ttk.Label(inner, text='daily candles the search runs on', style='Mut.TLabel',
                  font=(self.UI, 8)).pack(anchor='w', pady=(0, 6))
        g = ttk.Frame(inner, style='Card.TFrame')
        g.pack(fill='x')
        g.columnconfigure(0, weight=1)
        self.v_fetchn = self._num(g, 'How many pairs (top by turnover)', self.cfg.get('fetch_n', 150), 0, 5, 530, 10,
                                  tip='How many of the most liquid pairs to download from Binance.')
        self.v_minyears = self._num(g, 'Min. history (years)', self.cfg.get('fetch_years', 3), 1, 0, 7, 1,
                                    tip='Take only pairs older than N years — young ones have too little data.')
        self.btn_fetch = ttk.Button(inner, text='⟳  Download fresh data from Binance', command=self._fetch_data)
        self.btn_fetch.pack(fill='x', pady=(8, 0))
        self._tip(self.btn_fetch, 'Download fresh daily candles from Binance (overwrites current data).')

        # --- search ---
        g = self._section(inner, 'SEARCH')
        self.v_pop = self._num(g, 'Population', self.cfg['pop'], 0, 4, 4000, 10,
                               tip='How many candidate formulas per generation. More — broader coverage, but slower.')
        self.v_gens = self._num(g, 'Generations', self.cfg['gens'], 1, 1, 500, 1,
                                tip='How many generations of evolution per round.')
        self.v_seed = self._num(g, 'Seed (base)', self.cfg['seed'], 2, 0, 999999, 1,
                                tip='Random seed. The same seed → a reproducible run.')
        self.v_pause = self._num(g, 'Pause, sec', self.cfg['pause'], 3, 0, 3600, 1,
                                 tip='Pause between rounds so the machine gets a breather.')
        self.v_port = self._num(g, 'Status port', self.cfg['port'], 4, 1024, 65535, 1,
                                tip='Port for the status web page (http://localhost:PORT).')

        # --- node mode ---
        g = self._section(inner, 'NODE MODE (continuous search)')
        self.v_explore = self._num(g, 'Explore every N-th', self.cfg['explore_every'], 0, 1, 100, 1,
                                   tip='Every N-th round — a search from scratch (for diversity). Lower N — more diverse.')
        self.v_maxrounds = self._num(g, 'Max. rounds (0=∞)', self.cfg['max_rounds'], 1, 0, 999999, 1,
                                     tip='How many rounds to run before stopping. 0 — run forever.')
        self.v_leader = self._num(g, 'Leaderboard size', self.cfg['leaderboard'], 2, 1, 200, 1,
                                  tip='How many best alphas to keep in the top list.')
        self.v_seedlib = self._chk(g, 'Warm-start from library', self.cfg['seed_from_lib'], 3,
                                   tip='Seed the new generation with the best found alphas (fine-tuning). Off — always from scratch.')

        # --- simulation ---
        g = self._section(inner, 'SIMULATION')
        self.v_vol = self._numf(g, 'Target-vol (ann.)', self.cfg['target_vol'], 0, 0.01, 3.0, 0.01,
                                tip='Target annual portfolio volatility — sets the scale of positions/leverage.')
        self.v_exec = self._numf(g, 'Fee (turnover)', self.cfg['exec_cost'], 1, 0.0, 0.05, 0.0005,
                                 tip='Fee per trade, as a fraction of turnover. 0.001 = 10 basis points.')

        # --- genome ---
        g = self._section(inner, 'GENOME (formula complexity)')
        self.v_depth = self._num(g, 'Max. depth', self.cfg['max_depth'], 0, 2, 12, 1,
                                 tip='Maximum nesting of the formula tree.')
        self.v_size = self._num(g, 'Max. nodes', self.cfg['max_size'], 1, 3, 80, 1,
                                tip='Maximum operations in a formula — the main complexity limiter.')

        # --- selection ---
        g = self._section(inner, 'SELECTION (GA)')
        self.v_tourn = self._num(g, 'Tournament size', self.cfg['tournament'], 0, 2, 50, 1,
                                 tip='How many candidates to compare during selection. More — stricter selection.')
        self.v_elit = self._num(g, 'Elitism', self.cfg['elitism'], 1, 0, 50, 1,
                                tip='How many best pass to the next generation unchanged.')
        self.v_inject = self._num(g, 'Random/generation', self.cfg['random_inject'], 2, 0, 200, 1,
                                  tip='How many fresh random formulas to inject each generation (influx of novelty).')
        self.v_cx = self._numf(g, 'Crossover share', self.cfg['crossover_prob'], 3, 0.0, 1.0, 0.05,
                               tip='Share of crossover vs mutations (0..1).')

        # --- fitness ---
        g = self._section(inner, 'FITNESS')
        self.v_pars = self._numf(g, 'Complexity penalty', self.cfg['parsimony'], 0, 0.0, 1.0, 0.005,
                                 tip='Penalty for formula size — against over-complexity.')
        self.v_corrt = self._numf(g, 'Correlation threshold', self.cfg['corr_threshold'], 1, 0.0, 1.0, 0.05,
                                  tip='From what correlation to treat alphas as duplicates (for dedup).')
        self.v_corrp = self._numf(g, 'Similarity penalty', self.cfg['corr_penalty'], 2, 0.0, 2.0, 0.1,
                                  tip='Penalty for similarity to an already found alpha — for diversity.')
        self.v_hof = self._num(g, 'Hall of Fame size', self.cfg['hof_capacity'], 3, 1, 100, 1,
                               tip='How many champions to keep as output per round.')

        # --- segments ---
        g = self._section(inner, 'DATE SEGMENTS  (TRAIN < VAL < TEST)')
        self.v_train = self._txt(g, 'TRAIN start', self.cfg['train_start'], 0,
                                 tip='Start of the training period (evolution runs on it).')
        self.v_val = self._txt(g, 'VAL start', self.cfg['val_start'], 1,
                               tip='Start of validation — a robustness check.')
        self.v_test = self._txt(g, 'TEST start', self.cfg['test_start'], 2,
                                tip='Start of the held-out test — an honest OOS, not part of selection.')
        self.v_end = self._txt(g, 'TEST end', self.cfg['test_end'], 3,
                               tip='End of the entire data period.')

        # --- buttons ---
        btns = ttk.Frame(inner, style='Card.TFrame')
        btns.pack(fill='x', pady=(16, 0))
        self.btn_start = ttk.Button(btns, text='▶  Start node', style='Accent.TButton', command=self.start)
        self.btn_start.pack(fill='x', pady=(0, 6), ipady=4)
        self.btn_stop = ttk.Button(btns, text='■  Stop', style='Stop.TButton', command=self.stop, state='disabled')
        self.btn_stop.pack(fill='x', pady=(0, 6), ipady=2)
        b_reset = ttk.Button(btns, text='Reset to defaults', command=self._reset)
        b_reset.pack(fill='x', pady=(0, 6))
        b_web = ttk.Button(btns, text='Open status in browser', command=self._open_web)
        b_web.pack(fill='x')
        b_wipe = ttk.Button(btns, text='🗑  Clear all history', style='Danger.TButton',
                            command=self._wipe_history)
        b_wipe.pack(fill='x', pady=(14, 0))
        self._tip(self.btn_start, 'Start the background search with the current settings.')
        self._tip(self.btn_stop, 'Gently stop the search (the current round will finish).')
        self._tip(b_reset, 'Return all settings to their default values.')
        self._tip(b_web, 'Open the status page in the browser.')
        self._tip(b_wipe, 'Delete all history and found alphas (with confirmation).')

    def _section(self, parent, title):
        ttk.Label(parent, text=title, style='Sec.TLabel').pack(anchor='w', pady=(14, 6))
        f = ttk.Frame(parent, style='Card.TFrame')
        f.pack(fill='x')
        f.columnconfigure(0, weight=1)
        return f

    def _row(self, parent, label, row, widget, tip):
        lbl = ttk.Label(parent, text=label, style='Mut.TLabel')
        lbl.grid(row=row, column=0, sticky='w', pady=3)
        widget.grid(row=row, column=1, sticky='e', pady=3)
        if tip:
            self._tip(lbl, tip)
            self._tip(widget, tip)

    def _num(self, parent, label, val, row, lo, hi, step, tip=None):
        v = tk.IntVar(value=int(val))
        sp = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=v, width=9)
        self._row(parent, label, row, sp, tip)
        return v

    def _numf(self, parent, label, val, row, lo, hi, step, tip=None):
        v = tk.DoubleVar(value=float(val))
        sp = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=v, width=9, format='%.4f')
        self._row(parent, label, row, sp, tip)
        return v

    def _txt(self, parent, label, val, row, tip=None):
        v = tk.StringVar(value=str(val))
        e = ttk.Entry(parent, textvariable=v, width=12)
        self._row(parent, label, row, e, tip)
        return v

    def _chk(self, parent, label, val, row, tip=None):
        v = tk.BooleanVar(value=bool(val))
        cb = ttk.Checkbutton(parent, text=label, style='Card.TCheckbutton', variable=v)
        cb.grid(row=row, column=0, columnspan=2, sticky='w', pady=(6, 3))
        if tip:
            self._tip(cb, tip)
        return v

    # ---------- tooltips (short hints on hover) ----------
    def _tip(self, widget, text):
        widget.bind('<Enter>', lambda e, t=text: self._tip_schedule(e, t), add='+')
        widget.bind('<Leave>', lambda e: self._tip_hide(), add='+')
        widget.bind('<ButtonPress>', lambda e: self._tip_hide(), add='+')

    def _tip_schedule(self, e, text):
        self._tip_hide()
        self._tip_xy = (e.x_root + 16, e.y_root + 18)
        self._tip_after = self.root.after(400, lambda: self._tip_show(text))

    def _tip_show(self, text):
        self._tip_after = None
        if self._tip_win or not text:
            return
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        try:
            win.attributes('-topmost', True)
        except tk.TclError:
            pass
        tk.Label(win, text=text, bg='#0f172a', fg='#e5e7eb', justify='left', font=(self.UI, 9),
                 padx=9, pady=6, wraplength=250, highlightbackground='#334155',
                 highlightthickness=1).pack()
        x, y = self._tip_xy
        win.wm_geometry(f'+{x}+{y}')
        self._tip_win = win

    def _tip_hide(self):
        if self._tip_after:
            self.root.after_cancel(self._tip_after)
            self._tip_after = None
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None

    def _bind_wheel(self, canvas):
        def _w(e):
            d = -1 if (getattr(e, 'num', None) == 4 or getattr(e, 'delta', 0) > 0) else 1
            canvas.yview_scroll(d, 'units')

        def _on(_e):
            canvas.bind_all('<Button-4>', _w)
            canvas.bind_all('<Button-5>', _w)
            canvas.bind_all('<MouseWheel>', _w)

        def _off(_e):
            for seq in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
                canvas.unbind_all(seq)
        canvas.bind('<Enter>', _on)
        canvas.bind('<Leave>', _off)

    # ---------- right panel: status / chart / leaderboard ----------
    def _build_status(self, body):
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        card = self._card(right)
        card.grid(row=0, column=0, sticky='ew')
        head = ttk.Frame(card, style='Card.TFrame')
        head.pack(fill='x')
        self.lbl_state = ttk.Label(head, text='● stopped', style='Card.TLabel',
                                   font=(self.UI, 12, 'bold'), foreground=MUT)
        self.lbl_state.pack(side='left')
        self.lbl_res = ttk.Label(head, text='', style='Mut.TLabel')
        self.lbl_res.pack(side='right')

        stats = ttk.Frame(card, style='Card.TFrame')
        stats.pack(fill='x', pady=(16, 0))
        self.s_rounds = self._stat(stats, 'rounds', 0)
        self.s_trials = self._stat(stats, 'formulas tried', 1)
        self.s_found = self._stat(stats, 'alphas found', 2)
        self.lbl_cur = ttk.Label(card, text='', style='Mut.TLabel', font=(self.MONO, 9))
        self.lbl_cur.pack(anchor='w', pady=(14, 0))

        chart_card = self._card(right)
        chart_card.grid(row=1, column=0, sticky='ew', pady=(16, 0))
        ttk.Label(chart_card, text='PROGRESS — FITNESS min(train,val) BY ROUND  ·  TEST kept held-out',
                  style='H.TLabel').pack(anchor='w', pady=(0, 8))
        self.chart = tk.Canvas(chart_card, height=170, bg=CARD, highlightthickness=0)
        self.chart.pack(fill='x')
        self.chart.bind('<Configure>', lambda e: self._draw_chart())

        card2 = self._card(right)
        card2.grid(row=2, column=0, sticky='nsew', pady=(16, 0))
        hrow = ttk.Frame(card2, style='Card.TFrame')
        hrow.pack(fill='x', pady=(0, 8))
        self._lb_head_text = self._lb_head_for('base')
        self.lbl_lb_head = ttk.Label(hrow, text=self._lb_head_text, style='H.TLabel')
        self.lbl_lb_head.pack(side='left', anchor='w')
        sortf = ttk.Frame(hrow, style='Card.TFrame')
        sortf.pack(side='right')
        ttk.Label(sortf, text='rank by:', style='Mut.TLabel').pack(side='left', padx=(0, 6))
        self.v_lbsort = tk.StringVar(value='base')
        for val, lab, tip in (
                ('base', 'fitness',
                 'Rank by the honest fitness min(train, val).\n'
                 'Selection without peeking at TEST — that is how the node works.'),
                ('test', 'TEST OOS',
                 'Rank by held-out TEST — "top by OOS".\n'
                 '⚠ This is cherry-pick on held-out data: alphas picked\n'
                 'this way have an inflated TEST (a selection effect).\n'
                 'Fine to look at, but NOT as a selection criterion.')):
            rb = ttk.Radiobutton(sortf, text=lab, value=val, variable=self.v_lbsort,
                                 style='Card.TRadiobutton', command=self._set_lb_sort)
            rb.pack(side='left', padx=(0, 4))
            self._tip(rb, tip)
        ttk.Label(sortf, text='  ·  TEST >', style='Mut.TLabel').pack(side='left', padx=(6, 4))
        self.v_lbmin = tk.StringVar(value='')
        e_min = ttk.Entry(sortf, textvariable=self.v_lbmin, width=5, justify='center')
        e_min.pack(side='left')
        e_min.bind('<Return>', lambda ev: self._set_lb_sort())
        e_min.bind('<FocusOut>', lambda ev: self._set_lb_sort())
        self._tip(e_min, 'Show only alphas with TEST OOS above the threshold.\n'
                         'E.g. 1  → only those with held-out Sharpe > 1.\n'
                         'Empty — no filter. Enter or click outside the field — apply.')
        ttk.Label(sortf, text='  ·  min tr/yr', style='Mut.TLabel').pack(side='left', padx=(6, 4))
        self.v_lbact = tk.StringVar(value='2')
        e_act = ttk.Entry(sortf, textvariable=self.v_lbact, width=4, justify='center')
        e_act.pack(side='left')
        e_act.bind('<Return>', lambda ev: self._set_lb_sort())
        e_act.bind('<FocusOut>', lambda ev: self._set_lb_sort())
        self._tip(e_act, 'Minimum trade activity: trades per asset per year (on TEST).\n'
                         'Hides "strategies" that barely trade — e.g. 10 trades over 3.5 years,\n'
                         'whose high TEST Sharpe is a lucky buy-and-hold on a few bets, not real trading.\n'
                         'E.g. 2 → each asset must be traded at least ~2×/year on average.\n'
                         'Empty/0 — no filter. Enter or click outside — apply.')
        wrap = ttk.Frame(card2, style='Card.TFrame')
        wrap.pack(fill='both', expand=True)
        cols = ('rank', 'fit', 'test', 'ls', 'act', 'win', 'formula')
        self.tree = ttk.Treeview(wrap, columns=cols, show='headings', height=12)
        for c, txt, w, anc in (('rank', '#', 40, 'center'), ('fit', 'fitness', 74, 'e'),
                               ('test', 'TEST OOS', 84, 'e'), ('ls', 'trades L/S', 84, 'center'),
                               ('act', 'tr/yr·a', 58, 'e'),
                               ('win', 'win%', 56, 'e'), ('formula', 'formula', 320, 'w')):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == 'formula'), minwidth=w)
        self._tip(self.lbl_lb_head, 'trades L/S = total number of long / short positions OPENED over TEST\n'
                                    '(a trade = crossing into long/short from flat or the opposite side);\n'
                                    'tr/yr·a = trades per asset per year (relative activity — the "min tr/yr"\n'
                                    'filter drops barely-trading alphas); win% = share of days with profit.\n'
                                    'All on TEST (OOS), on target weights (daily rebalance).')
        self.tree.tag_configure('pos', foreground=POS)
        self.tree.tag_configure('neg', foreground=NEG)
        self.tree.tag_configure('odd', background=STRIPE)
        self.tree.tag_configure('even', background=CARD)
        vsb = ttk.Scrollbar(wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', self._on_row_open)
        self.tree.bind('<Button-3>', self._on_row_menu)             # right-click — context menu
        self.tree.bind('<Control-c>', lambda e: self._copy_formula())
        self.tree.bind('<Control-C>', lambda e: self._copy_formula())
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label='Copy formula', command=self._copy_formula)
        self._menu.add_command(label='Copy formula + metrics', command=self._copy_full)
        self._menu.add_separator()
        self._menu.add_command(label='Show equity', command=self._open_selected_plot)

        # ---- PORTFOLIO panel (combine top-N by TEST via the real engine) ----
        card3 = self._card(right)
        card3.grid(row=3, column=0, sticky='ew', pady=(16, 0))
        self.pf_card = card3
        hp = ttk.Frame(card3, style='Card.TFrame')
        hp.pack(fill='x')
        ttk.Label(hp, text='PORTFOLIO — top-N by TEST OOS, combined via the real engine',
                  style='H.TLabel').pack(side='left')
        ctl = ttk.Frame(hp, style='Card.TFrame')
        ctl.pack(side='right')
        ttk.Label(ctl, text='top', style='Mut.TLabel').pack(side='left', padx=(0, 4))
        self.v_pfn = tk.IntVar(value=6)
        ttk.Spinbox(ctl, from_=2, to=20, width=4, textvariable=self.v_pfn).pack(side='left', padx=(0, 8))
        self.btn_pf = ttk.Button(ctl, text='▶ Build portfolio', style='Accent.TButton',
                                 command=self._build_portfolio)
        self.btn_pf.pack(side='left')
        self._tip(self.btn_pf, 'Runs the top-N alphas by TEST Sharpe through the project Portfolio\n'
                               'engine (real simulation, ~1–2 min in the background) and shows the\n'
                               'combined dollar-neutral equity on TEST.')
        self.lbl_pf = ttk.Label(card3, style='Faint.TLabel', wraplength=900,
                                text='⚠ selecting by TEST inflates the number (cherry-pick); the '
                                     'diversification gain — combined ≫ any single alpha — is the real part.')
        self.lbl_pf.pack(anchor='w', pady=(6, 2))
        self.lbl_pf_m = ttk.Label(card3, text='', style='Card.TLabel', font=(self.UI, 11, 'bold'))
        self.lbl_pf_m.pack(anchor='w')
        self.pf_img = tk.Label(card3, bg=CARD)
        self.pf_img.pack(fill='x', pady=(8, 0))
        card3.bind('<Configure>', self._on_pf_resize)         # re-render equity to the panel width
        self.root.after(500, self._load_portfolio_on_start)   # show last build, if any

    def _load_portfolio_on_start(self):
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception:                                # noqa: BLE001
            return
        self._render_portfolio(doc)

    def _stat(self, parent, label, col):
        f = ttk.Frame(parent, style='Card.TFrame')
        f.grid(row=0, column=col, sticky='w', padx=(0, 34))
        val = ttk.Label(f, text='0', style='Big.TLabel')
        val.pack(anchor='w')
        ttk.Label(f, text=label.upper(), style='Faint.TLabel').pack(anchor='w', pady=(2, 0))
        return val

    # ---------- helpers ----------
    def _cpu_lbl(self):
        pct = int(self.v_cpu.get())
        self.lbl_cpu.config(text=f'{pct}%  →  {max(1, round(pct/100*CORES))} of {CORES} cores')

    def _uni_toggle(self):
        self.e_uni.config(state='disabled' if self.v_uniall.get() else 'normal')

    def _open_web(self):
        webbrowser.open(f'http://localhost:{self._gi(self.v_port, 8787)}')

    def _reset(self):
        self.cfg = dict(DEFAULTS)
        try:
            os.remove(SETTINGS)
        except OSError:
            pass
        self._apply_cfg_to_widgets()

    def _count_lines(self, path):
        try:
            with open(path, encoding='utf-8') as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _wipe_history(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('Node is running',
                                   'Stop the node first (it writes to these files), then clear.',
                                   parent=self.root)
            return
        n_alphas = self._count_lines(os.path.join(STATE_DIR, 'library.jsonl'))
        n_rounds = self._count_lines(os.path.join(STATE_DIR, 'history.jsonl'))
        if not (n_alphas or n_rounds or os.path.exists(STATUS_FILE)):
            messagebox.showinfo('Empty', 'History is already empty — nothing to clear.', parent=self.root)
            return
        msg = ('Delete ALL run history? This action is irreversible.\n\n'
               f'• {n_alphas} found alphas  (library.jsonl)\n'
               f'• {n_rounds} rounds and the chart  (history.jsonl)\n'
               '• current status  (status.json)\n\n'
               'Search settings (the parameters on the left) will remain.')
        if not messagebox.askyesno('Full clear', msg, icon='warning',
                                    default='no', parent=self.root):
            return
        import glob
        removed = 0
        for name in ('library.jsonl', 'history.jsonl', 'status.json'):
            try:
                os.remove(os.path.join(STATE_DIR, name))
                removed += 1
            except OSError:
                pass
        for p in glob.glob(os.path.join(STATE_DIR, 'equity_view_*.png')):
            try:
                os.remove(p)
            except OSError:
                pass
        self._reset_ui_after_wipe()
        messagebox.showinfo('Done', 'History cleared. You can start the search from scratch.', parent=self.root)

    def _fetch_data(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('Node is running',
                                   'Stop the node before updating data — it uses the data for the search.',
                                   parent=self.root)
            return
        n = self._gi(self.v_fetchn, 150)
        yrs = self._gi(self.v_minyears, 3)
        if not messagebox.askyesno(
                'Download fresh data',
                f'Download the {n} highest-turnover Binance pairs (only those with history ≥ {yrs} years) '
                'and update the market data?\n\n'
                'Current data will be replaced. It will take a few minutes and needs internet.\n'
                'After the update the pairs universe will change — clear history and restart the search.',
                icon='warning', default='no', parent=self.root):
            return
        self._save()
        win = tk.Toplevel(self.root)
        win.title(f'Data update — top-{n} from Binance')
        win.configure(bg=CARD)
        win.geometry('760x440')
        txt = tk.Text(win, bg='#0f1115', fg='#d7dce3', font=('TkFixedFont', 9),
                      wrap='word', borderwidth=0)
        txt.pack(fill='both', expand=True, padx=12, pady=12)

        def add(s):
            if not win.winfo_exists():
                return
            txt.configure(state='normal')
            txt.insert('end', s)
            txt.see('end')
            txt.configure(state='disabled')

        add(f'Downloading the {n} highest-turnover Binance pairs (history ≥ {yrs} years)…\n\n')
        try:
            proc = subprocess.Popen(_child_cmd('fetch') + ['--top', str(n),
                                     '--min-years', str(yrs), '--out', DATA_PICKLE],
                                    cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                       # noqa: BLE001
            add(f'Failed to launch fetch_data.py: {e}\n')
            return
        q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)
        threading.Thread(target=_reader, daemon=True).start()
        self.btn_fetch.config(state='disabled')

        def pump():
            if not win.winfo_exists():
                self.btn_fetch.config(state='normal')   # the process will finish on its own, re-enable the button
                return
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        code = proc.poll()
                        add('\n' + ('✓ Done — data updated. Clear history and restart the search.'
                                    if code == 0 else f'✗ Error (code {code}). Data left untouched.') + '\n')
                        self.btn_fetch.config(state='normal')
                        self._lib_cache['mtime'] = None
                        return
                    add(line)
            except queue.Empty:
                pass
            win.after(150, pump)
        win.after(150, pump)

    def _reset_ui_after_wipe(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._treesig = None
        self._shown = []
        self._lib_cache = {'mtime': None, 'diverse': [], 'computing': False, 'dirty': False,
                           'ts': 0.0, 'sort': self._lb_sort, 'minv': self._lb_min,
                           'minact': self._lb_minact, 'computed': False}
        self._history = []
        self._draw_chart()
        self.s_rounds.config(text='0')
        self.s_trials.config(text='0')
        self.s_found.config(text='0')
        self.lbl_cur.config(text='')
        self.lbl_state.config(text='● stopped', foreground=MUT)

    def _apply_cfg_to_widgets(self):
        c = self.cfg
        self.v_cpu.set(c['cpu']); self._cpu_lbl()
        self.v_uniall.set(c['universe_all']); self.v_unilist.set(c['universe_list']); self._uni_toggle()
        self.v_pop.set(c['pop']); self.v_gens.set(c['gens']); self.v_seed.set(c['seed'])
        self.v_pause.set(c['pause']); self.v_port.set(c['port'])
        self.v_fetchn.set(c['fetch_n']); self.v_minyears.set(c['fetch_years'])
        self.v_explore.set(c['explore_every']); self.v_maxrounds.set(c['max_rounds'])
        self.v_leader.set(c['leaderboard']); self.v_seedlib.set(c['seed_from_lib'])
        self.v_vol.set(c['target_vol']); self.v_exec.set(c['exec_cost'])
        self.v_depth.set(c['max_depth']); self.v_size.set(c['max_size'])
        self.v_tourn.set(c['tournament']); self.v_elit.set(c['elitism'])
        self.v_inject.set(c['random_inject']); self.v_cx.set(c['crossover_prob'])
        self.v_pars.set(c['parsimony']); self.v_corrt.set(c['corr_threshold'])
        self.v_corrp.set(c['corr_penalty']); self.v_hof.set(c['hof_capacity'])
        self.v_train.set(c['train_start']); self.v_val.set(c['val_start'])
        self.v_test.set(c['test_start']); self.v_end.set(c['test_end'])

    def _set_running(self, running):
        self.btn_start.config(state='disabled' if running else 'normal')
        self.btn_stop.config(state='normal' if running else 'disabled')

    # ---------- start/stop ----------
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self._save()
        c = self.cfg
        os.makedirs(STATE_DIR, exist_ok=True)
        env = dict(os.environ)
        env.update(
            ALPHANODE_CPU_PERCENT=str(c['cpu']),
            ALPHANODE_UNIVERSE=('all' if c['universe_all'] else c['universe_list']),
            ALPHANODE_POP=str(c['pop']), ALPHANODE_GENS=str(c['gens']),
            ALPHANODE_SEED=str(c['seed']), ALPHANODE_PAUSE=str(c['pause']),
            ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_STATUS_PORT=str(c['port']),
            ALPHANODE_DATA=apppaths.data_path(),   # current snapshot (fresh/bundled)
            ALPHANODE_CONFIG_INI=apppaths.config_ini(),
            ALPHANODE_EXPLORE_EVERY=str(c['explore_every']),
            ALPHANODE_SEED_FROM_LIBRARY=('1' if c['seed_from_lib'] else '0'),
            ALPHANODE_MAX_ROUNDS=str(c['max_rounds']),
            ALPHANODE_LEADERBOARD=str(c['leaderboard']),
            ALPHANODE_TARGET_VOL=str(c['target_vol']), ALPHANODE_EXEC_COST=str(c['exec_cost']),
            ALPHANODE_MAX_DEPTH=str(c['max_depth']), ALPHANODE_MAX_SIZE=str(c['max_size']),
            ALPHANODE_TOURNAMENT=str(c['tournament']), ALPHANODE_ELITISM=str(c['elitism']),
            ALPHANODE_RANDOM_INJECT=str(c['random_inject']),
            ALPHANODE_CROSSOVER_PROB=str(c['crossover_prob']),
            ALPHANODE_PARSIMONY=str(c['parsimony']),
            ALPHANODE_CORR_THRESHOLD=str(c['corr_threshold']),
            ALPHANODE_CORR_PENALTY=str(c['corr_penalty']),
            ALPHANODE_HOF_CAPACITY=str(c['hof_capacity']),
            ALPHANODE_TRAIN_START=c['train_start'], ALPHANODE_VAL_START=c['val_start'],
            ALPHANODE_TEST_START=c['test_start'], ALPHANODE_TEST_END=c['test_end'],
        )
        self.proc = subprocess.Popen(_child_cmd('node'), env=env,
                                     cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._reader, daemon=True).start()
        self._set_running(True)

    def _reader(self):
        for line in self.proc.stdout:
            self.logq.put(line.rstrip())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)     # the node gently finishes the round and exits
            except Exception:
                self.proc.terminate()
        self.btn_stop.config(state='disabled')

    def _on_close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        finally:
            self.root.destroy()

    # ---------- status polling ----------
    def _poll(self):
        running = bool(self.proc and self.proc.poll() is None)
        self._set_running(running)
        st = {}
        try:
            st = json.load(open(STATUS_FILE))
        except Exception:
            pass
        if st:
            state = st.get('state', '—')
            color = {'running': POS, 'starting': ACC}.get(state, MUT)
            self.lbl_state.config(text=f'● {"running" if state=="running" else state}', foreground=color)
            vol = st.get('target_vol')
            vol_s = f' · vol {vol:g}' if isinstance(vol, (int, float)) else ''
            self.lbl_res.config(text=f'{st.get("cpu_percent","?")}% · {st.get("n_jobs","?")}/{st.get("cores","?")} cores '
                                     f'· {st.get("universe","")}{vol_s}')
            self.s_rounds.config(text=str(st.get('rounds', 0)))
            self.s_trials.config(text=f'{st.get("trials_total", 0):,}')
            self.s_found.config(text=str(st.get('found', len(st.get('best', [])))))
            self.lbl_cur.config(text=(st.get('current', '') + '   ' + st.get('gen', ''))[:120])
            self._refresh_leaderboard(st.get('best', []))
            self._history = st.get('history', [])
            self._draw_chart()
        if not running and (not st or st.get('state') != 'running'):
            if not (self.proc and self.proc.poll() is None):
                self.lbl_state.config(text='● stopped', foreground=MUT)
        try:
            while True:
                self.logq.get_nowait()
        except queue.Empty:
            pass
        self.root.after(1500, self._poll)

    def _draw_chart(self):
        cv = getattr(self, 'chart', None)
        if cv is None:
            return
        hist = getattr(self, '_history', []) or []
        cv.delete('all')
        w = max(cv.winfo_width(), 300)
        h = int(cv['height'])

        def _v(p):                                       # optimized fitness (old log — fallback to best_test)
            return p.get('best_base', p.get('best_test'))
        pts = [(p['round'], _v(p)) for p in hist if _v(p) is not None]
        last_test = next((p.get('best_test') for p in reversed(hist) if p.get('best_test') is not None), None)
        if len(pts) < 2:
            cv.create_text(w / 2, h / 2, text='chart will appear after a couple of rounds',
                           fill=MUT, font=('TkDefaultFont', 9))
            return
        ys = [v for _, v in pts]
        lo, hi = min(ys), max(ys)
        if hi - lo < 0.3:                                  # keep the line from flattening out
            m = (hi + lo) / 2
            lo, hi = m - 0.15, m + 0.15
        padL, padR, padT, padB = 56, 18, 18, 24
        n = len(pts)
        plotw, ploth = w - padL - padR, h - padT - padB
        base_y = padT + ploth

        def X(i):
            return padL + plotw * (i / (n - 1))

        def Y(v):
            return padT + ploth * (1 - (v - lo) / (hi - lo))

        for frac in (0.0, 0.5, 1.0):                       # grid + Y labels
            val = lo + (hi - lo) * frac
            y = Y(val)
            cv.create_line(padL, y, w - padR, y, fill='#edf0f5')
            cv.create_text(padL - 9, y, text=f'{val:+.2f}', anchor='e', fill=FAINT, font=('TkDefaultFont', 8))

        line = []
        for i, (_, v) in enumerate(pts):
            line += [X(i), Y(v)]
        cv.create_polygon(padL, base_y, *line, X(n - 1), base_y, fill=ACC_SOFT, outline='')  # fill
        cv.create_line(*line, fill=ACC, width=2, capstyle='round', joinstyle='round')
        lx, ly = X(n - 1), Y(ys[-1])
        cv.create_oval(lx - 4, ly - 4, lx + 4, ly + 4, fill=ACC, outline='#ffffff', width=2)
        cv.create_text(w - padR, padT - 6, text=f'fitness {ys[-1]:+.2f}', anchor='ne',
                       fill=ACC, font=('TkDefaultFont', 10, 'bold'))
        cv.create_text(padL, h - 6, text=f'round {pts[0][0]}', anchor='w', fill=FAINT, font=('TkDefaultFont', 8))
        cv.create_text(w - padR, h - 6, text=f'round {pts[-1][0]}', anchor='e', fill=FAINT, font=('TkDefaultFont', 8))
        if last_test is not None:                        # honest held-out — bottom center, no collisions
            cv.create_text((padL + w - padR) / 2, h - 6, text=f'champion TEST {last_test:+.2f} · held-out',
                           anchor='s', fill=FAINT, font=('TkDefaultFont', 8))

    def _dedup(self, best, target=15):
        """Show DISTINCT alphas: strictly at first (max diversity), and if there are too few rows —
        more loosely, so the table isn't empty (for a monoculture it shows variants of one family)."""
        result = list(best)
        for thresh in (0.80, 0.88, 0.95):
            kept = []
            for c in best:
                f = c.get('formula', '')
                if all(difflib.SequenceMatcher(None, f, k.get('formula', '')).ratio() < thresh for k in kept):
                    kept.append(c)
            result = kept
            if len(kept) >= target:
                break
        return result

    _LB_TESTKEY = staticmethod(
        lambda c: (c.get('test') if isinstance(c.get('test'), dict) else {}).get('sharpe'))

    def _lb_head_for(self, mode, minv=None, minact=None):
        flt = f'  ·  TEST > {minv:+.2f}' if minv is not None else ''
        flt += f'  ·  ≥{minact:g} tr/yr·a' if minact else ''
        if mode == 'test':
            return ('TOP BY TEST OOS  ·  ⚠ cherry-pick on held-out (number inflated)' + flt +
                    '  ·  double-click: equity  ·  right-click / Ctrl+C: copy')
        return ('BEST ALPHA FROM EACH FAMILY (by fitness min(train,val))  ·  TEST — OOS' + flt +
                '  ·  double-click: equity  ·  right-click / Ctrl+C: copy')

    def _read_lb_min(self):
        """Threshold from the field: empty/garbage -> None (no filter). A comma separator is also accepted."""
        raw = (self.v_lbmin.get() or '').strip().replace(',', '.')
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _read_lb_act(self):
        """Min trade activity from the field (trades/asset/year): empty/0/garbage -> None (no filter)."""
        raw = (self.v_lbact.get() or '').strip().replace(',', '.')
        if not raw:
            return None
        try:
            v = float(raw)
        except ValueError:
            return None
        return v if v > 0 else None

    def _set_lb_sort(self):
        mode = self.v_lbsort.get()
        minv = self._read_lb_min()
        minact = self._read_lb_act()
        if mode == self._lb_sort and minv == self._lb_min and minact == self._lb_minact:
            return
        self._lb_sort = mode
        self._lb_min = minv
        self._lb_minact = minact
        self._lb_head_text = self._lb_head_for(mode, minv, minact)
        self.lbl_lb_head.config(text=self._lb_head_text)
        self._start_lb_compute(force=True)               # immediate recompute for the new order/filter
        self._drain_lb()                                 # and render, even if the node is stopped

    def _start_lb_compute(self, force=False):
        lib = os.path.join(STATE_DIR, 'library.jsonl')
        try:
            mt = os.path.getmtime(lib)
        except OSError:
            return
        cache = self._lib_cache
        if cache['computing']:
            return
        if (not force and mt == cache['mtime'] and cache.get('sort') == self._lb_sort
                and cache.get('minv') == self._lb_min and cache.get('minact') == self._lb_minact):
            return
        cache['computing'] = True
        cache['ts'] = time.time()
        threading.Thread(target=self._compute_diverse,
                         args=(lib, mt, self._lb_sort, self._lb_min, self._lb_minact),
                         daemon=True).start()

    def _drain_lb(self, tries=20):
        """Render the table as soon as the background recompute is ready (works even when the node is stopped)."""
        cache = self._lib_cache
        if cache['dirty']:
            cache['dirty'] = False
            self._treesig = None
            self._render_lb(cache['diverse'])
            return
        if tries > 0 and cache['computing']:
            self.root.after(150, lambda: self._drain_lb(tries - 1))

    def _render_lb(self, best):
        """Fill the table and adjust the header (including when the filter let no one through)."""
        self._fill_tree(best)
        if not best and (self._lb_min is not None or self._lb_minact) and self._lib_cache.get('computed'):
            parts = []
            if self._lb_min is not None:
                parts.append(f'TEST OOS > {self._lb_min:+.2f}')
            if self._lb_minact:
                parts.append(f'≥ {self._lb_minact:g} tr/yr·a')
            self._lb_head_text = ('NO ALPHAS WITH ' + ' AND '.join(parts) +
                                  '  ·  lower the thresholds or clear the fields')
        else:
            self._lb_head_text = self._lb_head_for(self._lb_sort, self._lb_min, self._lb_minact)
        self.lbl_lb_head.config(text=self._lb_head_text)

    def _refresh_leaderboard(self, status_best):
        """Into the table — the best alpha FROM EACH family (across the whole library), not the top-20 clones.
        Computed in the background and cached by (mtime, sort mode, threshold) — otherwise O(N²) similarity would freeze the GUI."""
        cache = self._lib_cache
        now = time.time()
        if not cache['computing'] and now - cache['ts'] > 6:
            self._start_lb_compute()                     # restart on change of file / mode / threshold
        if cache['dirty']:
            cache['dirty'] = False
            self._treesig = None                         # force a redraw after recompute
            self._render_lb(cache['diverse'])
        elif not cache.get('computed'):
            self._fill_tree(status_best)                 # until computed — the top from the node (as before)

    def _compute_diverse(self, path, mtime, sort, minv, minact):
        rows = []
        try:
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except OSError:
            self._lib_cache['computing'] = False
            return
        keyf = self._LB_TESTKEY if sort == 'test' else (lambda c: c.get('base'))
        rows = [c for c in rows if keyf(c) is not None]
        if minv is not None:                             # threshold always by TEST OOS, regardless of mode
            tk_ = self._LB_TESTKEY
            rows = [c for c in rows if tk_(c) is not None and tk_(c) > minv]
        rows.sort(key=keyf, reverse=True)                # by fitness min(train,val) OR by TEST OOS
        if minact:                                       # drop barely-trading alphas (relative activity)
            rows = self._filter_active(rows, minact)
        kept = []
        for c in rows[:500]:                             # candidates — the top by the chosen metric
            f = c.get('formula', '')
            if all(difflib.SequenceMatcher(None, f, k.get('formula', '')).ratio() < 0.80 for k in kept):
                kept.append(c)
            if len(kept) >= self._lb_target:
                break
        self._lib_cache.update(diverse=kept, mtime=mtime, computing=False, dirty=True,
                               sort=sort, minv=minv, minact=minact, computed=True)

    def _fill_tree(self, best):
        best = self._dedup(best)
        sig = (len(best), best[0]['formula'] if best else '')
        if getattr(self, '_treesig', None) == sig:
            return
        self._treesig = sig
        self._shown = best                               # for clicks: row -> champion
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._row_items = {}
        for i, c in enumerate(best):
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            ts = t.get('sharpe')                         # honest held-out OOS — colored by it
            base = c.get('base')
            sign = 'pos' if (ts is not None and ts >= 0) else ('neg' if ts is not None else 'even')
            stripe = 'odd' if i % 2 else 'even'
            formula = c.get('formula', '')
            f = formula if len(formula) <= 78 else formula[:78] + '…'
            m = self._metrics_cache.get(formula)
            ls, act, win = self._fmt_metrics(m)
            item = self.tree.insert('', 'end', values=(
                i + 1, f'{base:+.2f}' if base is not None else '—',
                f'{ts:+.2f}' if ts is not None else '—', ls, act, win, f),
                tags=(sign, stripe))
            self._row_items[formula] = item
        self._start_metrics(best)                        # compute long/short/win in the background

    @staticmethod
    def _fmt_metrics(m):
        """('L/S', 'tr/yr·a', 'win%') strings from the cache: None=still computing, 'err'=failed."""
        if m is None:
            return '…', '…', '…'
        if m == 'err':
            return '—', '—', '—'
        a = m.get('act', 0.0)
        astr = f'{a:.1f}' if a < 10 else f'{a:.0f}'
        return f'{m["long"]:.0f}/{m["short"]:.0f}', astr, f'{m["win"] * 100:.0f}%'

    def _start_metrics(self, champs):
        """Background computation of long/short/win (on TEST) for the shown alphas; cached by formula."""
        todo = [c for c in champs if c.get('formula') and c['formula'] not in self._metrics_cache]
        if not todo:
            return
        self._metrics_seq += 1
        seq = self._metrics_seq
        threading.Thread(target=self._compute_metrics, args=(todo, seq), daemon=True).start()

    def _metrics_ctx(self):
        """Prepared context for trade-stat computation: panel/market, the TEST mask, and the
        universe size + span used for the activity rate. Built once per pass; may raise if the
        data/config is unavailable (callers fail open)."""
        import numpy as np
        cfg = self._build_plot_cfg()
        _tk, panel, market, _basket = self._get_market(cfg)
        ts0, ts1 = cfg['splits']['test']                 # TEST (OOS) window
        tmask = (market['index'] >= ts0) & (market['index'] < ts1)
        elig = market['base_elig']
        n_assets = int(elig[tmask].any(axis=0).sum()) or int(elig.shape[1])   # assets live on TEST
        years = max(float(np.count_nonzero(tmask)) / 365.0, 1e-9)
        return {'panel': panel, 'market': market, 'V': market['V'], 'elig': elig, 'tmask': tmask,
                'n_assets': max(1, n_assets), 'years': years, 'vol': cfg['vol'], 'exec': cfg['exec']}

    def _trade_stats(self, formula, ctx):
        """{long, short, win, act} for one formula on TEST — act = trades per asset per year
        (relative activity, universe/period independent). 'err' if it doesn't parse or never trades."""
        import numpy as np
        import pandas as pd
        from genome import parse
        from evaluator import eval_alpha_panel
        from fastsim import fast_sim
        market, V, elig, tmask = ctx['market'], ctx['V'], ctx['elig'], ctx['tmask']
        try:
            raw = eval_alpha_panel(parse(formula), ctx['panel'])[market['tk']].to_numpy(dtype=np.float64)
            A = pd.DataFrame(raw).ffill().to_numpy()
            E = elig & np.isfinite(A)
            fc = np.where(E, np.where(E, A, 0.0) / V, 0.0)
            chips = np.nansum(np.abs(fc), axis=1, keepdims=True)
            W = fc / np.where(chips == 0.0, 1.0, chips)                      # + long / − short
            side = np.where(W > 0.0005, 1, np.where(W < -0.0005, -1, 0))     # daily side [T,N]
            if not np.abs(side[tmask]).any():                               # no positions on TEST — invalid
                return 'err'
            # a "trade" = opening a position: cross into long/short from flat/opposite
            prev = np.vstack([np.zeros((1, side.shape[1])), side[:-1]])      # previous calendar day
            long_tr = int(((side == 1) & (prev != 1))[tmask].sum())         # long entries in TEST
            short_tr = int(((side == -1) & (prev != -1))[tmask].sum())      # short entries in TEST
            rt = fast_sim(raw, market, ctx['vol'], ctx['exec']).to_numpy()[tmask]
            active = np.abs(rt) > 1e-9                                       # days when something happened
            win = float((rt[active] > 0).mean()) if active.any() else 0.0
            act = (long_tr + short_tr) / ctx['n_assets'] / ctx['years']     # trades / asset / year
            return {'long': long_tr, 'short': short_tr, 'win': win, 'act': act}
        except Exception:                                                   # noqa: BLE001
            return 'err'

    def _filter_active(self, rows, minact, cap=140):
        """Keep candidates whose activity >= minact (trades/asset/year on TEST). Bounded and
        cache-backed: evaluate the top candidates until enough pass or the eval budget is spent;
        fail open (return rows unchanged) if data/config is unavailable."""
        with self._metrics_lock:
            try:
                ctx = self._metrics_ctx()
            except Exception:                            # noqa: BLE001  (no data/config)
                return rows
            want, out, evals = self._lb_target * 3, [], 0
            for c in rows:
                f = c.get('formula', '')
                m = self._metrics_cache.get(f)
                if not (isinstance(m, dict) and 'act' in m):
                    if evals >= cap:
                        break                            # budget spent — the rest are lower-ranked
                    m = self._trade_stats(f, ctx)
                    self._metrics_cache[f] = m
                    evals += 1
                if isinstance(m, dict) and m.get('act', 0.0) >= minact:
                    out.append(c)
                    if len(out) >= want:
                        break
            return out

    def _compute_metrics(self, champs, seq):
        with self._metrics_lock:
            try:
                ctx = self._metrics_ctx()
                for c in champs:
                    if seq != self._metrics_seq:         # the list changed — drop the stale computation
                        return
                    formula = c['formula']
                    m = self._metrics_cache.get(formula)
                    if not (isinstance(m, dict) and 'act' in m):     # not already done by the filter
                        self._metrics_cache[formula] = self._trade_stats(formula, ctx)
            except Exception:                            # noqa: BLE001  (no data/config — quietly)
                for c in champs:
                    self._metrics_cache.setdefault(c.get('formula', ''), 'err')
            finally:
                try:
                    self.root.after(0, lambda s=seq: self._apply_metrics(s))
                except (RuntimeError, tk.TclError):      # window already closed / no loop
                    pass

    def _apply_metrics(self, seq):
        """Set the computed long/short/win cells into the already shown rows (main thread)."""
        if seq != self._metrics_seq:
            return
        for formula, item in list(self._row_items.items()):
            if not self.tree.exists(item):
                continue
            ls, act, win = self._fmt_metrics(self._metrics_cache.get(formula))
            self.tree.set(item, 'ls', ls)
            self.tree.set(item, 'act', act)
            self.tree.set(item, 'win', win)

    # ---------- equity chart on click (TRAIN|VAL|TEST + B&H) ----------
    def _on_row_open(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        idx = self.tree.index(item)
        if 0 <= idx < len(self._shown):
            self._open_plot(self._shown[idx])

    # ---------- copy formula ----------
    def _selected_champ(self):
        item = self.tree.focus() or (self.tree.selection()[0] if self.tree.selection() else '')
        if not item:
            return None
        idx = self.tree.index(item)
        return self._shown[idx] if 0 <= idx < len(self._shown) else None

    def _on_row_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _to_clipboard(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()                               # so the buffer is handed to the X server right away
        self.lbl_lb_head.config(text=msg)
        self.root.after(1300, lambda: self.lbl_lb_head.config(text=self._lb_head_text))

    def _copy_formula(self):
        c = self._selected_champ()
        if c and c.get('formula'):
            self._to_clipboard(c['formula'], '✓ formula copied to clipboard')

    def _copy_full(self):
        c = self._selected_champ()
        if not c:
            return

        def sh(seg):
            v = (c.get(seg) or {}).get('sharpe')
            return f'{v:+.2f}' if v is not None else '—'
        txt = (f"{c.get('formula', '')}\n"
               f"fitness(base)={c.get('base')}  train={sh('train')}  val={sh('val')}  TEST(OOS)={sh('test')}")
        self._to_clipboard(txt, '✓ formula + metrics copied')

    def _open_selected_plot(self):
        c = self._selected_champ()
        if c:
            self._open_plot(c)

    # ---------- PORTFOLIO: combine top-N by TEST via the real engine ----------
    def _build_portfolio(self):
        if self._pf_proc and self._pf_proc.poll() is None:
            return                                       # already building
        n = self._gi(self.v_pfn, 6)
        self.btn_pf.config(state='disabled')
        self.lbl_pf_m.config(text='', foreground=MUT)
        self.lbl_pf.config(text=f'building portfolio from top-{n} by TEST (real engine, ~1–2 min)…')
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=apppaths.data_path(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini())
        try:
            self._pf_proc = subprocess.Popen(
                _child_cmd('portfolio') + ['--top', str(n), '--out', PORTFOLIO_JSON], env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.config(text=f'could not start portfolio build: {e}')
            self.btn_pf.config(state='normal')
            return
        threading.Thread(target=self._pf_reader, args=(self._pf_proc,), daemon=True).start()

    def _pf_reader(self, proc):
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if line:
                self.root.after(0, lambda s=line: self.lbl_pf.config(text=s))
        proc.wait()
        self.root.after(0, self._portfolio_done)

    def _portfolio_done(self):
        self.btn_pf.config(state='normal')
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.config(text=f'portfolio build did not finish: {e}')
            return
        self._render_portfolio(doc)

    def _render_portfolio(self, doc):
        if not doc.get('ok'):
            self.lbl_pf.config(text='portfolio build failed: ' + str(doc.get('error', ''))[:120])
            return
        self._pf_doc = doc                               # remember for re-render on resize
        m = doc.get('metrics') or {}
        b = doc.get('basket') or {}
        self.lbl_pf.config(text=f'top-{doc.get("n")} by TEST OOS combined via the engine  ·  '
                                f'TEST {doc.get("test", "")}  ·  built in {doc.get("built_secs", "?")}s  ·  '
                                '⚠ selected by TEST (optimistic); diversification gain is the robust part')
        sh = m.get('sharpe')
        self.lbl_pf_m.config(
            text=f'Sharpe {sh:+.2f}   ·   CAGR {m.get("cagr", 0) * 100:+.0f}%   ·   '
                 f'MaxDD {m.get("dd", 0) * 100:.0f}%      (vs buy&hold Sharpe {b.get("sharpe", 0):+.2f})',
            foreground=(POS if (sh is not None and sh >= 0) else NEG))
        threading.Thread(target=self._render_pf_equity, args=(doc, self._pf_width()),
                         daemon=True).start()

    def _pf_width(self):
        """Target equity-image width = current panel width (so it fills the space, expandable)."""
        w = self.pf_card.winfo_width()
        if w <= 1:                                       # not laid out yet
            w = self.tree.winfo_width() or 900
        return max(700, min(w - 34, 3400))

    def _on_pf_resize(self, event):
        if not self._pf_doc:
            return
        w = self._pf_width()
        if abs(w - self._pf_last_w) < 40:                # ignore tiny/noise resizes
            return
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
        self._pf_resize_after = self.root.after(         # debounce: re-render after resize settles
            250, lambda: threading.Thread(target=self._render_pf_equity,
                                          args=(self._pf_doc, self._pf_width()), daemon=True).start())

    def _render_pf_equity(self, doc, width=900):
        eq = doc.get('equity') or {}
        if not eq.get('dates'):
            return
        try:
            self._pf_last_w = width
            with self._plot_lock:
                import pandas as pd
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                x = pd.to_datetime(eq['dates'])
                w = width
                dpi = 100
                fig_h = min(3.8, max(2.4, w / dpi / 4.5))     # grow height gently with width
                fig = plt.figure(figsize=(w / dpi, fig_h), dpi=dpi)
                ax = fig.gca()
                ax.plot(x, eq['combined'], lw=2.0, color=ACC, label=f'Portfolio (top-{doc.get("n")})')
                ax.plot(x, eq['basket'], lw=1.2, color='#f9a825', ls=':', label='buy & hold (EW)')
                ax.set_yscale('log'); ax.grid(True, which='both', alpha=0.3)
                ax.legend(loc='upper left', fontsize=8)
                ax.set_title(f'combined equity — TEST ({doc.get("test", "")})', fontsize=9)
                ax.tick_params(labelsize=8)
                fig.tight_layout(); fig.savefig(PORTFOLIO_PNG, dpi=dpi, facecolor='white')
                plt.close(fig)
            self.root.after(0, self._show_pf_img)
        except Exception:                                # noqa: BLE001
            pass

    def _show_pf_img(self):
        try:
            img = tk.PhotoImage(file=PORTFOLIO_PNG)
            self._pf_img_ref = img                        # keep ref
            self.pf_img.config(image=img)
        except tk.TclError:
            pass

    def _build_plot_cfg(self):
        """The same config the node searched with (self.cfg = last run): pairs, vol/fee,
        TRAIN/VAL/TEST segments — so the curve and metrics match the leaderboard."""
        from config import load_config
        import pandas as pd
        cfg = load_config()
        c = self.cfg
        if not c.get('universe_all', True):
            lst = [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()]
            cfg['instruments'] = lst or cfg.get('instruments')
        cfg['vol'] = float(c.get('target_vol', cfg['vol']))
        cfg['exec'] = float(c.get('exec_cost', cfg['exec']))
        try:
            tr = pd.Timestamp(c['train_start'], tz='UTC'); va = pd.Timestamp(c['val_start'], tz='UTC')
            te = pd.Timestamp(c['test_start'], tz='UTC'); en = pd.Timestamp(c['test_end'], tz='UTC')
            cfg['splits'] = {'train': (tr, va), 'val': (va, te), 'test': (te, en)}
            cfg['start'] = tr.tz_localize(None).to_pydatetime()
            cfg['end'] = en.tz_localize(None).to_pydatetime()
        except Exception:
            pass
        return cfg

    def _get_market(self, cfg):
        from evaluator import build_panel, make_market, basket_returns
        key = (tuple(cfg['instruments']) if cfg.get('instruments') else 'all',
               str(cfg['start']), str(cfg['end']))
        cached = self._panel_cache.get(key)
        if cached is None:
            tk_, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
            cached = (tk_, panel, make_market(panel, tk_, raw), basket_returns(panel))
            self._panel_cache = {key: cached}            # keep only the last one (memory)
        return cached

    def _open_plot(self, champ):
        self._plot_seq += 1
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        img_w = int(min(1680, max(1000, sw * 0.80)))     # large chart, but within the screen
        img_h = int(img_w / 1.7)
        avail_h = int(sh * 0.90) - 200                   # room for the header/buttons
        if img_h > avail_h:
            img_h = max(480, avail_h)
            img_w = int(img_h * 1.7)
        dpi = 110
        holder = {'done': False, 'path': None, 'err': None, 'dpi': dpi,
                  'figsize': (img_w / dpi, img_h / dpi),
                  'out': os.path.join(STATE_DIR, f'equity_view_{self._plot_seq}.png')}
        win = tk.Toplevel(self.root)
        win.title('Equity — ' + champ.get('formula', '')[:60])
        win.configure(bg=CARD)
        win.geometry(f'{img_w + 44}x{img_h + 200}')

        head = tk.Frame(win, bg=CARD)
        head.pack(fill='x', padx=16, pady=(14, 6))

        def seg(name, m, accent=False):
            m = m or {}
            sh, cg, dd = m.get('sharpe'), m.get('cagr'), m.get('dd')
            txt = f'{name}:  Sharpe {sh:+.2f}' if sh is not None else f'{name}:  —'
            if cg is not None:
                txt += f'   CAGR {cg*100:+.0f}%'
            if dd is not None:
                txt += f'   DD {dd*100:.0f}%'
            tk.Label(head, text=txt, bg=CARD, fg=(NEG if accent else TXT),
                     font=('TkDefaultFont', 10, 'bold' if accent else 'normal')).pack(anchor='w')

        seg('TRAIN', champ.get('train'))
        seg('VAL', champ.get('val'))
        seg('TEST (held-out)', champ.get('test'), accent=True)
        tk.Label(head, text=champ.get('formula', ''), bg=CARD, fg=MUT, justify='left',
                 wraplength=img_w - 30, font=(self.MONO, 9)).pack(anchor='w', pady=(6, 0))
        btnrow = tk.Frame(head, bg=CARD)
        btnrow.pack(anchor='w', pady=(10, 0))
        ttk.Button(btnrow, text='📄  Paper Trade — build bundle', style='Accent.TButton',
                   command=lambda: self._paper_trade(champ)).pack(side='left')
        ttk.Button(btnrow, text='📥  Download signals (CSV)',
                   command=lambda: self._download_signals(champ)).pack(side='left', padx=(8, 0))

        body = tk.Frame(win, bg=CARD)
        body.pack(fill='both', expand=True, padx=16, pady=(4, 14))
        status = tk.Label(body, text='building equity (TRAIN | VAL | TEST + basket B&H)…',
                          bg=CARD, fg=MUT, font=('TkDefaultFont', 11))
        status.pack(pady=40)

        threading.Thread(target=self._compute_equity, args=(champ, holder), daemon=True).start()
        self.root.after(200, lambda: self._check_plot(win, holder, status, body))

    # ---------- download portfolio signals (CSV) ----------
    def _download_signals(self, champ):
        formula = champ.get('formula', '')
        if not formula:
            return
        name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save portfolio signals',
            defaultextension='.csv', initialfile=f'signals_{name}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        try:
            import numpy as np
            import pandas as pd
            from genome import parse
            from evaluator import eval_alpha_panel
            cfg = self._build_plot_cfg()
            _tk, panel, market, _basket = self._get_market(cfg)
            ap = eval_alpha_panel(parse(formula), panel)
            A = pd.DataFrame(ap[market['tk']].to_numpy(dtype=np.float64)).ffill().to_numpy()
            V = market['V']
            E = market['base_elig'] & np.isfinite(A)                # eligible & has a signal
            fc = np.where(E, A, 0.0) / V                            # inverse-vol (as in the engine)
            fc = np.where(E, fc, 0.0)
            chips = np.nansum(np.abs(fc), axis=1, keepdims=True)    # normalization by "chips"
            W = fc / np.where(chips == 0.0, 1.0, chips)             # target weight: + long / − short

            wide = pd.DataFrame(np.round(W, 6), index=market['index'], columns=market['tk'])
            wide.index.name = 'date'
            wide = wide[wide.abs().sum(axis=1) > 0]                 # without empty pre-listing days
            sp = cfg['splits']

            # human-readable tidy format: row = one position
            long = wide.reset_index().melt(id_vars='date', var_name='ticker', value_name='weight')
            long = long[long['weight'].abs() > 0.0005].copy()
            long['side'] = np.where(long['weight'] > 0, 'LONG', 'SHORT')
            long['weight_pct'] = long['weight'].map(lambda x: f'{x * 100:+.1f}%')
            d = long['date']
            long['segment'] = np.where(d < sp['val'][0], 'TRAIN',
                                       np.where(d < sp['test'][0], 'VAL', 'TEST'))
            long['_aw'] = long['weight'].abs()
            long = long.sort_values(['date', '_aw'], ascending=[True, False]).drop(columns='_aw')
            long = long[['date', 'segment', 'ticker', 'side', 'weight', 'weight_pct']]
            long.to_csv(path, index=False)

            last = wide.iloc[-1]
            pos = sorted([(t, float(v)) for t, v in last.items() if abs(v) > 0.0005],
                         key=lambda kv: -abs(kv[1]))
            self._signals_dialog(path, wide.index[-1].date(), pos, len(wide))
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Error', f'Failed to build signals: {e}', parent=self.root)

    def _signals_dialog(self, path, latest_date, positions, n_days):
        win = tk.Toplevel(self.root)
        win.title('Portfolio signals')
        win.configure(bg=CARD)
        win.geometry('440x560')
        frm = tk.Frame(win, bg=CARD)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        tk.Label(frm, text='📥  Signals saved', bg=CARD, fg=TXT,
                 font=(self.UI, 13, 'bold')).pack(anchor='w')
        tk.Label(frm, text=path, bg=CARD, fg=MUT, font=(self.MONO, 8),
                 wraplength=400, justify='left').pack(anchor='w', pady=(2, 12))
        tk.Label(frm, text=f'What to hold on the last day ({latest_date}):', bg=CARD, fg=TXT,
                 font=(self.UI, 10, 'bold')).pack(anchor='w')
        tk.Label(frm, text='+ long · − short · %  = share of portfolio', bg=CARD, fg=FAINT,
                 font=(self.UI, 8)).pack(anchor='w', pady=(0, 8))
        tbl = tk.Frame(frm, bg=CARD)
        tbl.pack(fill='both', expand=True)
        for i, (t, w) in enumerate(positions[:16]):
            side, col = ('LONG', POS) if w > 0 else ('SHORT', NEG)
            rbg = STRIPE if i % 2 else CARD
            r = tk.Frame(tbl, bg=rbg)
            r.pack(fill='x')
            tk.Label(r, text=side, bg=rbg, fg=col, font=(self.MONO, 9, 'bold'),
                     width=6, anchor='w').pack(side='left', padx=(4, 0))
            tk.Label(r, text=t, bg=rbg, fg=TXT, font=(self.MONO, 9), anchor='w').pack(side='left')
            tk.Label(r, text=f'{w * 100:+.1f}%', bg=rbg, fg=col, font=(self.MONO, 9, 'bold'),
                     anchor='e').pack(side='right', padx=(0, 6))
        tk.Label(frm, text=f'Full history ({n_days} days) — in the CSV: date, ticker, side, weight_pct.',
                 bg=CARD, fg=MUT, font=(self.UI, 8), wraplength=400, justify='left').pack(anchor='w', pady=(10, 0))
        ttk.Button(frm, text='Close', command=win.destroy).pack(anchor='e', pady=(10, 0))

    # ---------- paper trade: export the bundle + run ----------
    def _paper_trade(self, champ):
        formula = champ.get('formula', '')
        if not formula:
            return
        try:
            sys.path.insert(0, HERE)
            import paper_export
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror('Paper Trade', f'The generator failed to load: {e}', parent=self.root)
            return
        c = self.cfg
        if c.get('universe_all', True):
            try:
                tickers = list(pickle.load(open(apppaths.data_path(), 'rb'))[0])
            except Exception as e:                       # noqa: BLE001
                messagebox.showerror('Paper Trade', f'Cannot read the loaded data: {e}', parent=self.root)
                return
        else:
            tickers = [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()]
        if not tickers:
            messagebox.showwarning('Paper Trade', 'The pairs universe is empty.', parent=self.root)
            return
        name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
        out_root = apppaths.exports_dir()
        try:
            path = paper_export.build_bundle(
                formula, name, tickers, float(c.get('target_vol', 0.25)),
                float(c.get('exec_cost', 0.001)), str(c.get('train_start', '2019-09-05')),
                out_root, meta=champ)
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror('Paper Trade', f'Bundle build error: {e}', parent=self.root)
            return
        self._paper_dialog(path, len(tickers))

    def _paper_dialog(self, path, n):
        win = tk.Toplevel(self.root)
        win.title('Paper-trading bundle ready')
        win.configure(bg=CARD)
        win.geometry('660x280')
        frm = tk.Frame(win, bg=CARD)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        tk.Label(frm, text='📄  Bundle built', bg=CARD, fg=TXT,
                 font=('TkDefaultFont', 13, 'bold')).pack(anchor='w')
        tk.Label(frm, text=f'{n} pairs · engine + strategy.py + paper_trade.py + README.md',
                 bg=CARD, fg=MUT).pack(anchor='w', pady=(2, 10))
        tk.Label(frm, text=path, bg=CARD, fg=ACC, font=('TkFixedFont', 9),
                 wraplength=600, justify='left').pack(anchor='w')
        tk.Label(frm, text='Run paper trading FORWARD on new data — that is the honest check. '
                           'Live is not included in the bundle (see README).',
                 bg=CARD, fg=MUT, wraplength=600, justify='left').pack(anchor='w', pady=(10, 14))
        row = tk.Frame(frm, bg=CARD)
        row.pack(fill='x')
        ttk.Button(row, text='📂 Open folder', command=lambda: self._open_folder(path)).pack(side='left')
        ttk.Button(row, text='▶ Run now', style='Accent.TButton',
                   command=lambda: (win.destroy(), self._run_bundle(path))).pack(side='left', padx=8)
        ttk.Button(row, text='Close', command=win.destroy).pack(side='right')

    def _open_folder(self, path):
        for opener in ('xdg-open', 'open'):
            try:
                subprocess.Popen([opener, path])
                return
            except Exception:                            # noqa: BLE001
                continue

    def _run_bundle(self, path):
        win = tk.Toplevel(self.root)
        win.title('Paper-trade — step')
        win.configure(bg=CARD)
        win.geometry('780x460')
        txt = tk.Text(win, bg='#0f1115', fg='#d7dce3', font=('TkFixedFont', 9),
                      wrap='word', borderwidth=0)
        txt.pack(fill='both', expand=True, padx=12, pady=12)

        def add(s):
            if not win.winfo_exists():
                return
            txt.configure(state='normal')
            txt.insert('end', s)
            txt.see('end')
            txt.configure(state='disabled')

        add('$ python paper_trade.py force\n\n')
        if apppaths.FROZEN:                              # own interpreter (numpy/pandas inside)
            cmd = [sys.executable, '--role', 'runpy', os.path.join(path, 'paper_trade.py'), 'force']
        else:
            cmd = [sys.executable, '-u', 'paper_trade.py', 'force']
        try:
            proc = subprocess.Popen(cmd, cwd=path,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                           # noqa: BLE001
            add(f'Failed to launch: {e}\n')
            return
        q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)
        threading.Thread(target=_reader, daemon=True).start()

        def pump():
            if not win.winfo_exists():
                return
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        code = proc.poll()
                        add('\n' + ('✓ Step done. The account is in paper_state.json (in the bundle folder).'
                                    if code == 0 else f'✗ Error (code {code}).') + '\n')
                        return
                    add(line)
            except queue.Empty:
                pass
            win.after(200, pump)
        win.after(200, pump)

    def _compute_equity(self, champ, holder):
        with self._plot_lock:                            # pyplot is global — one at a time
            try:
                from genome import parse
                from evaluator import simulate_returns
                import report
                cfg = self._build_plot_cfg()
                tk_, panel, market, basket = self._get_market(cfg)
                r = simulate_returns(parse(champ['formula']), tk_, panel, market, cfg['vol'], cfg['exec'])
                if r is None:
                    holder['err'] = 'the formula yields no valid returns on this data'
                else:
                    ts = (champ.get('test') or {}).get('sharpe')
                    label = 'strategy' + (f' · TEST Sharpe {ts:+.2f}' if ts is not None else '')
                    report.plot_equity({label: r}, basket, cfg['splits'], holder['out'],
                                       'Growth of $1 (NET, log):  TRAIN | VAL | TEST   vs   EW basket (buy & hold)',
                                       figsize=holder['figsize'], dpi=holder['dpi'])
                    holder['path'] = holder['out']
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

    def _check_plot(self, win, holder, status, body):
        if not win.winfo_exists():
            try:
                os.remove(holder['out'])
            except OSError:
                pass
            return
        if not holder['done']:
            self.root.after(200, lambda: self._check_plot(win, holder, status, body))
            return
        if holder['err']:
            status.config(text='Error: ' + holder['err'], fg=NEG)
            return
        try:
            photo = tk.PhotoImage(file=holder['path'])
            status.destroy()
            lbl = tk.Label(body, image=photo, bg=CARD)
            lbl.image = photo                            # keep a reference, otherwise GC eats it
            lbl.pack(fill='both', expand=True)
        except Exception as e:                           # noqa: BLE001
            status.config(text=f'Failed to show the chart: {e}', fg=NEG)
        finally:
            try:
                os.remove(holder['out'])                 # png is already in PhotoImage memory
            except OSError:
                pass


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
