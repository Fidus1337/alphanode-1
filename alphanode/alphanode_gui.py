"""AlphaNode — desktop interface (CustomTkinter).

Control panel for the background node. On the left — the FULL set of search settings (resources,
universe, population/generations, node mode, simulation/target-vol, genome, GA selection, fitness,
date segments) — everything the engine understands is tunable by hand and passed to the node via
ALPHANODE_* variables. On the right — live status, a progress chart and a leaderboard of found
alphas. Launches the node as a subprocess (node.py) and reads its state/status.json.

Theming: every colour comes from PALETTE[light|dark] and is published as the module-level constants
below (BG, CARD, TXT, …). Switching the theme re-applies the palette and rebuilds the window — the
parts we don't own (ttk.Treeview, the Canvas chart, matplotlib PNGs) can't restyle themselves live,
so a rebuild is both simpler and the only way to keep them consistent.

CustomTkinter notes: CTk widgets reject .config() (use .configure()) and name the text colour
text_color, not foreground. The leaderboard stays a ttk.Treeview — CustomTkinter has no table.

NO COLOUR EMOJI in widget text — labels are plain words. Beyond taste, a colour emoji is rendered by
Noto Color Emoji, and that font inside a CustomTkinter widget SEGFAULTS Tk on Linux/Xft (plain
tk/ttk survives it, which is why the pre-CTk build could carry emoji and this one cannot). The X
error is asynchronous, so it surfaces at whatever unrelated Tcl call runs next — the traceback will
point somewhere innocent. Monochrome BMP marks (▶ ■ ● ✓ ✕ ⚠) are safe and are all we use.

Run:  python alphanode/alphanode_gui.py
"""
import os
import sys
import csv
import json
import time
import queue
from datetime import datetime, timezone, timedelta
import signal
import pickle
import difflib
import hashlib
import threading
import subprocess

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

import customtkinter as ctk

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
SIGNAL_PY = os.path.join(HERE, 'signal_service.py')     # local live-signal API
METRICS_PY = os.path.join(HERE, 'metrics_worker.py')    # leaderboard trade stats (own process)
PDF_PY = os.path.join(HERE, 'pdf_worker.py')            # analytics PDF dashboard (own process)
SIGNAL_PORT = 8799                                      # BASE port: each service takes the next free one
DATA_PICKLE = apppaths.user_data_pickle()               # where the data fetcher writes fresh data
# First-run starter universe (no data snapshot yet): 10 liquid majors, matches fetch_data.DEFAULT_SYMBOLS.
BOOTSTRAP_SYMBOLS = 'BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT'
STATE_DIR = apppaths.state_dir()
STATUS_FILE = os.path.join(STATE_DIR, 'status.json')
SIGNALS_JSON = os.path.join(STATE_DIR, 'signals.json')  # registry of served APIs (survives a restart)
PORTFOLIO_JSON = os.path.join(STATE_DIR, 'portfolio.json')
PORTFOLIO_PNG = os.path.join(STATE_DIR, 'portfolio_equity.png')
SETTINGS = apppaths.settings_file()
CORES = os.cpu_count() or 4


TF_CHOICES = ('1d', '4h', '1h', '15m')

def _tf_suffix(tf):
    """File suffix for per-timeframe state: '' for the historical daily files, '_1h' etc. else."""
    return '' if (tf or '1d') == '1d' else f'_{tf}'


def _tf_clean(tf):
    """Coerce a stored timeframe to a supported one — settings saved before a timeframe was
    dropped from the product ('5m') must fall back to daily instead of crashing the pipeline."""
    t = (tf or '1d').strip().lower()
    return t if t in TF_CHOICES else '1d'


def _child_cmd(role):
    """Command for the child process of role `role`: in the frozen build — the exe itself with
    --role, in dev — the real python with the script."""
    if apppaths.FROZEN:
        return [sys.executable, '--role', role]
    script = {'node': NODE_PY, 'fetch': FETCH_PY, 'portfolio': PORTFOLIO_PY,
              'signal': SIGNAL_PY, 'metrics': METRICS_PY, 'pdfreport': PDF_PY,
              'rescore': os.path.join(HERE, 'rescore_library.py'),
              'forward': os.path.join(HERE, 'forward_track.py')}[role]
    return [sys.executable, '-u', script]

DEFAULTS = {
    # resources / universe
    'cpu': 50, 'universe_all': True,
    'universe_list': 'BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LINKUSDT',
    # search
    'pop': 200, 'gens': 25, 'seed': 1, 'pause': 5, 'port': 8787,
    # data
    'fetch_n': 150, 'fetch_years': 3,
    'timeframe': '1d',      # bar size for the whole pipeline: 1d | 4h | 1h | 15m
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
    'fit_blocks': 0,        # robust multi-block fitness (experimental); 0 = legacy min(TRAIN,VAL)
    # date segments (TRAIN < VAL < TEST)
    'train_start': '2019-09-05', 'val_start': '2021-11-01',
    'test_start': '2023-01-01', 'test_end': '2026-07-05',
    # appearance
    'theme': '',            # 'light' | 'dark' | '' = follow the OS on first run
    'settings_open': False,  # settings pane hidden by default; toggled by the header button
    'lb_mode': 'all',       # leaderboard: 'all' = every alpha | 'families' = best per family (deduped)
    'card_order': [],       # dashboard card order (drag a card to reorder); [] = default layout
    'split_w': 0,           # settings|dashboard sash, 100%-DPI px; 0 = size by content
    'chart_h': 0,           # progress-chart height, 100%-DPI px; 0 = default (170)
    'pf_h': 0,              # portfolio equity-plot height, px; 0 = automatic (from width)
}

# --- design palette: Linear/Stripe style, light + dark ---
# One entry per colour role. _apply_palette() publishes the active theme into the module-level
# constants below, which the whole file reads — so a widget just says fg=TXT and stays theme-correct.
PALETTE = {
    'light': dict(
        BG='#edeff5',        # app background (cool light gray)
        CARD='#ffffff',      # cards
        BORDER='#e7eaf1',    # hairline borders (controls); cards keep a whisper of it
        TXT='#111a2e',       # text (slate-900, a touch softer)
        MUT='#66738a',       # muted (slate-500)
        FAINT='#97a3b8',     # even fainter (slate-400)
        ACC='#6366f1',       # accent (indigo-500)
        ACC_HI='#4f46e5',    # hover
        ACC_DN='#4338ca',    # pressed
        ACC_SOFT='#eceffe',  # soft fill / row highlight (indigo-50)
        POS='#059669',       # gain (emerald-600)
        NEG='#e11d48',       # loss (rose-600)
        HEAD_BG='#f3f5f9',   # tonal surfaces: tiles, buttons, fields
        HEAD_HI='#eaedf4',   # their hover
        STRIPE='#f9fafd',    # row zebra striping
        GRID='#eef1f6',      # chart gridlines
        CARD_BW=1,           # cards on white need a hairline to separate
        TIP_BG='#111a2e', TIP_FG='#e5e7eb', TIP_BD='#334155',    # tooltips (dark on light)
    ),
    'dark': dict(
        BG='#0a0c11',        # a step deeper than the cards — the panels read as raised surfaces
        CARD='#151926',
        BORDER='#242a3a',
        TXT='#e7eaf2',
        MUT='#9fa9bc',
        FAINT='#6e7a90',
        ACC='#818cf8',       # indigo-400: the 500 is too dim on a dark card
        ACC_HI='#a5b4fc',
        ACC_DN='#6366f1',
        ACC_SOFT='#262d52',
        POS='#34d399',       # emerald-400 / rose-400: the 600s fail contrast on dark
        NEG='#fb7185',
        HEAD_BG='#1d2233',
        HEAD_HI='#262c40',
        STRIPE='#1a1f2d',
        GRID='#212637',
        CARD_BW=0,           # borderless cards: depth comes from the surface tones alone
        TIP_BG='#2b313f', TIP_FG='#e8ebf2', TIP_BD='#3d4557',    # lighter than the card, not darker
    ),
}
# Published by _apply_palette(); declared here so the names exist at import time.
BG = CARD = BORDER = TXT = MUT = FAINT = ACC = ACC_HI = ACC_DN = ACC_SOFT = ''
POS = NEG = HEAD_BG = HEAD_HI = STRIPE = GRID = TIP_BG = TIP_FG = TIP_BD = ''
CARD_BW = 0


def _apply_palette(theme):
    """Publish PALETTE[theme] into this module's globals and tell CustomTkinter which mode its own
    widgets should draw in. Returns the resolved theme name ('light'/'dark')."""
    if theme not in PALETTE:
        theme = 'light'
    globals().update(PALETTE[theme])
    ctk.set_appearance_mode('Dark' if theme == 'dark' else 'Light')
    return theme


def _system_theme():
    """What the OS is set to — the default for a first run, so we open in the user's own mode."""
    try:
        import darkdetect
        return 'dark' if (darkdetect.theme() or '').lower() == 'dark' else 'light'
    except Exception:                                       # noqa: BLE001 — optional, any failure -> light
        return 'light'


def _mix(c1, c2, t):
    """Blend two '#rrggbb' colours; t=0 -> c1, t=1 -> c2. Canvas has no alpha — this is it."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _rrect(cv, x0, y0, x1, y1, r, **kw):
    """A rounded rectangle on a tk.Canvas (smoothed polygon — Tk has no native one)."""
    pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return cv.create_polygon(pts, smooth=True, **kw)


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.logq = queue.Queue()
        self._panel_cache = {}                           # (instruments,start,end) -> (tk,panel,market,basket)
        self._plot_lock = threading.Lock()               # pyplot is global -> build one at a time
        self._plot_seq = 0
        self._metrics_cache = {}                          # formula -> {'long','short','win'} (on TEST)
        self._metrics_lock = threading.Lock()             # one worker batch at a time
        self._metrics_proc = None                         # the metrics child process
        self._metrics_seq = 0                             # to discard stale background computations
        self._row_items = {}                              # formula -> table row id (to update cells)
        self._pf_proc = None                              # portfolio-build subprocess
        self._fwd_proc = None                             # forward-track step subprocess
        self._fwd_entries = []                            # rows currently shown in the FORWARD table
        self._sigs = []                                   # running signal-API services (one per port)
        self._sig_health = {}                             # port -> status text (written by poll workers)
        self._sig_status_lbl = {}                         # port -> status Label (main thread only)
        self._sig_shown = None                            # ports currently rendered (rebuild only on change)
        self._sig_pending = None                          # services found by _sig_restore, for the tick
        self._pf_img_ref = None                           # keep a ref to the equity PhotoImage (else GC)
        self._pf_doc = None                               # last portfolio result (for re-render on resize)
        self._pf_resize_after = None                      # debounce id for resize re-render
        self._pf_last_w = 0                               # last render width (skip tiny resizes)
        self._shown = []                                 # what is actually shown in the table (for clicks)
        self._sort_col = 'fit'                            # leaderboard sort column (click a header)
        self._sort_desc = True                            # descending (best first)
        self._lb_select = 'fit'                           # POPULATION key: 'fit' = min(train,val); 'test' = held-out OOS
        self._lb_mode = 'all'                            # 'all' = every alpha | 'families' = best per family
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self._lb_target = 20                             # 'families' mode: how many DISTINCT families to keep
        self._vis_after = None                           # debounce handle for lazy per-viewport metrics
        self._fonts = {}                                  # (family,size,weight) -> named Tk font
        self._tip_win = None                             # tooltip window + deferred display
        self._tip_after = None
        self._fetching = False                           # a fetch_data child is running (one at a time)
        self.cfg = dict(DEFAULTS)
        self._load()
        self._lb_mode = self.cfg.get('lb_mode') or 'all'   # remembered across restarts
        self.cfg['theme'] = _apply_palette(self.cfg.get('theme') or _system_theme())
        self._init_window()
        self._style()
        self._build()
        self._poll()
        self._sig_tick()                                  # live status of the served signal APIs
        threading.Thread(target=self._sig_restore, daemon=True).start()   # re-adopt ones left running
        self.root.after(900, self._maybe_bootstrap)       # first run: no data -> fetch 10 majors
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
            timeframe=_tf_clean(self.v_tf.get()),
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
            fit_blocks=self._gi(self.v_fitblocks, d['fit_blocks']),
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
    def _px(self, n):
        """A size in CustomTkinter units (pixels at 100%) as a raw Tk font size.

        CTkFont measures in PIXELS and CTk multiplies by widget_scaling; a plain Tk/ttk font tuple
        measures in POINTS and is multiplied by `tk scaling` instead. Same tuple, different unit —
        so ttk and Canvas text must be asked for in negative (= pixel) sizes, pre-scaled by hand,
        or they come out ~2.3x the size of everything else on a HiDPI screen."""
        return -max(1, int(round(n * self.SCALE)))

    def _init_window(self):
        """One-time window setup. Kept out of _style() — that runs again on every theme switch, and
        re-applying geometry there would snap a window the user had resized back to the default."""
        # Tk reports the display's own scaling (pixels per point; 1.333 at 96 dpi). CustomTkinter
        # defaults to 1.0 on Linux and would draw a doll's-house UI next to the point-sized ttk
        # widgets, so hand it the display's real factor.
        self.SCALE = min(max(round(self.root.tk.call('tk', 'scaling') / 1.3333, 3), 1.0), 3.0)
        ctk.set_widget_scaling(self.SCALE)
        ctk.set_window_scaling(self.SCALE)
        self.root.title('AlphaNode')
        self.root.geometry('1100x860')                   # CTk scales this by window_scaling
        self.root.minsize(980, 680)                      # raw, like geometry: CTk scales it too, and
        #                                                  pre-scaling made the floor 1.75x too big

    def _style(self):
        """Fonts + the ttk styles for the widgets CustomTkinter has no answer for (the leaderboard
        table and the numeric spinboxes). Everything else is styled per-widget from the palette."""
        self.root.configure(fg_color=BG)

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
            s.theme_use('clam')                          # the only built-in theme that honours colours
        except tk.TclError:
            pass
        s.configure('.', background=CARD, foreground=TXT, font=(F, self._px(13)))

        # numeric spinboxes — CustomTkinter has no spinbox, so these stay ttk (tonal, like _entry)
        s.configure('TSpinbox', fieldbackground=HEAD_BG, background=HEAD_BG, foreground=TXT,
                    arrowcolor=MUT, bordercolor=BORDER, lightcolor=HEAD_BG, darkcolor=HEAD_BG,
                    borderwidth=1, padding=int(4 * self.SCALE), insertcolor=TXT,
                    font=self._font(F, 13))
        s.map('TSpinbox', bordercolor=[('focus', ACC)], lightcolor=[('focus', ACC)],
              darkcolor=[('focus', ACC)])

        # comboboxes (Timeframe, portfolio "by …") — clam's default is a light-gray box that
        # glares on a dark card, and in 'readonly' the text is drawn as a SELECTION, which is
        # what read as a white smear. Same tonal look as the spinboxes/entries.
        s.configure('TCombobox', fieldbackground=HEAD_BG, background=HEAD_BG, foreground=TXT,
                    arrowcolor=MUT, bordercolor=BORDER, lightcolor=HEAD_BG, darkcolor=HEAD_BG,
                    borderwidth=1, padding=int(4 * self.SCALE), insertcolor=TXT,
                    font=self._font(F, 13))
        s.map('TCombobox',
              fieldbackground=[('readonly', HEAD_BG), ('disabled', CARD)],
              background=[('readonly', HEAD_BG), ('active', HEAD_HI)],
              foreground=[('readonly', TXT), ('disabled', FAINT)],
              selectbackground=[('readonly', HEAD_BG)],
              selectforeground=[('readonly', TXT)],
              bordercolor=[('focus', ACC)], lightcolor=[('focus', ACC)],
              darkcolor=[('focus', ACC)], arrowcolor=[('disabled', FAINT)])
        # the drop-down list is a plain Tk listbox created by ttk — reachable only via the
        # option database; re-adding on a theme switch overrides the previous values
        for opt, val in (('background', HEAD_BG), ('foreground', TXT),
                         ('selectBackground', ACC), ('selectForeground', '#ffffff'),
                         ('borderWidth', 0), ('font', self._font(F, 13))):
            self.root.option_add(f'*TCombobox*Listbox.{opt}', val)

        # the leaderboard — CustomTkinter has no table, so this stays a ttk.Treeview.
        # rowheight/fonts are deliberately roomier than ttk's defaults: this table IS the app.
        # bordercolor matters: clam draws a frame around the field, which reads as a stray light
        # rectangle on a dark card unless it matches the card
        s.configure('Treeview', rowheight=int(32 * self.SCALE), fieldbackground=CARD, background=CARD,
                    foreground=TXT, borderwidth=0, relief='flat', bordercolor=CARD,
                    lightcolor=CARD, darkcolor=CARD, font=(self.MONO, self._px(12)))
        s.configure('Treeview.Heading', font=(F, self._px(10.5), 'bold'), foreground=FAINT,
                    background=CARD, relief='flat',
                    padding=(int(8 * self.SCALE), int(9 * self.SCALE)), bordercolor=CARD)
        s.map('Treeview.Heading', background=[('active', HEAD_BG)])
        s.map('Treeview', background=[('selected', ACC_SOFT)], foreground=[('selected', TXT)])
        s.layout('Treeview.Item',                        # drop the indent reserved for tree handles
                 [('Treeitem.padding', {'sticky': 'nswe', 'children':
                  [('Treeitem.text', {'side': 'left', 'sticky': ''})]})])

    # ---------- cheap widgets ----------
    # CustomTkinter draws everything on canvases: a CTkLabel is three X widgets (frame + canvas +
    # label) and costs ~2x a tk.Label to create, for pixels that are identical when the background
    # is flat. Widget count is what startup time tracks here, so CTk is reserved for the widgets
    # whose look it actually changes — buttons, entry, slider, switch, checkbox, scrollbar, cards —
    # and static text / plain containers use these.
    def _font(self, family, size, weight='normal'):
        """A NAMED font, created once per (family, size, weight) and referenced by name.

        A font tuple makes Tk resolve the family again for every widget it is handed to; a named
        font is resolved once. On this display that is ~2ms vs ~4ms per label — with ~70 labels it
        is worth the cache. Names live in Tk, so the cache survives a theme rebuild."""
        key = (family, size, weight)
        f = self._fonts.get(key)
        if f is None:
            # Hold the Font OBJECT, not just its name: tkinter's Font.__del__ deletes the named font
            # from Tk, and every widget still pointing at it silently falls back to the default.
            f = tkfont.Font(name=f'AN{len(self._fonts)}', family=family, size=self._px(size),
                            weight=weight, exists=False)
            self._fonts[key] = f
        return f

    def _lbl(self, parent, text='', text_color=None, font=None, bg=None, **kw):
        """Static text. Same call shape as CTkLabel, one X widget instead of three."""
        fam, size, *rest = font if font else (self.UI, 13)
        if 'wraplength' in kw:                           # CTk scales it for you; tk does not
            kw['wraplength'] = int(kw['wraplength'] * self.SCALE)
        kw.setdefault('anchor', 'w')                     # tk centres by default, so a label wider
        kw.setdefault('justify', 'left')                 # than its slot loses BOTH ends, not one
        return tk.Label(parent, text=text, fg=(text_color or TXT), bg=(bg or CARD),
                        font=self._font(fam, size, rest[0] if rest else 'normal'), **kw)

    def _box(self, parent, bg=None, **kw):
        """A plain layout container (what CTkFrame(fg_color='transparent') was for)."""
        return tk.Frame(parent, bg=(bg or CARD), **kw)

    def _card(self, parent, **kw):
        """A card: the surface every panel sits on. Stays CTk — the rounded corner is the point.
        Dark theme drops the border entirely (CARD_BW=0): depth comes from surface tones."""
        return ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=CARD_BW,
                            corner_radius=14, **kw)

    def _pad(self, card):
        """Inner padding frame for a card (CTkFrame has no padding option)."""
        f = self._box(card)
        f.pack(fill='both', expand=True, padx=18, pady=16)
        return f

    # ---------- layout ----------
    def _build(self):
        # Everything lives inside _shell so a theme switch can drop and rebuild the whole UI without
        # touching the root's other children (open dialogs, tooltips).
        self._shell = self._box(self.root, bg=BG)
        self._shell.pack(fill='both', expand=True)
        bar = tk.Canvas(self._shell, height=3, bg=ACC, highlightthickness=0)   # accent gradient bar
        bar.pack(fill='x')

        def _grad(e, cv=bar):
            cv.delete('all')
            n = 64
            for i in range(n):
                cv.create_rectangle(e.width * i / n, 0, e.width * (i + 1) / n + 1, 4,
                                    fill=_mix(ACC, ACC_DN, i / (n - 1)), outline='')
        bar.bind('<Configure>', _grad)
        top = self._box(self._shell, bg=BG)
        top.pack(fill='x', padx=20, pady=(14, 11))
        brand = self._box(top, bg=BG)
        brand.pack(side='left')
        ms = int(30 * self.SCALE)                        # logo mark: rounded square with an alpha
        mark = tk.Canvas(brand, width=ms, height=ms, bg=BG, highlightthickness=0)
        _rrect(mark, 1, 1, ms - 1, ms - 1, int(9 * self.SCALE), fill=ACC, outline='')
        mark.create_text(ms / 2, ms / 2 - 1 * self.SCALE, text='α', fill='#ffffff',
                         font=self._font(self.UI, 17, 'bold'))
        mark.pack(side='left', padx=(0, 10), pady=(2, 0))
        self._lbl(brand, text='Alpha', font=(self.UI, 24, 'bold'), text_color=TXT, bg=BG).pack(side='left')
        self._lbl(brand, text='Node', font=(self.UI, 24, 'bold'), text_color=ACC, bg=BG).pack(side='left')
        self._lbl(top, text='background search for trading strategies', text_color=MUT,
                     font=(self.UI, 13), bg=BG).pack(side='left', padx=(14, 0), pady=(6, 0))
        self._build_theme_pick(top)
        # header controls: the node is driven from here — defaults just work, the full settings
        # panel stays hidden until the Settings button is pressed
        self.btn_settings = self._btn(top, '⚙  Settings', self._toggle_settings, kind='soft',
                                      height=34, width=112)
        self.btn_settings.pack(side='right', padx=(0, 16), pady=(2, 0))
        self.btn_stop = self._btn(top, '■  Stop', self.stop, kind='soft', height=34, width=88)
        self.btn_stop.configure(state='disabled')
        self.btn_stop.pack(side='right', padx=(0, 8), pady=(2, 0))
        self.btn_start = self._btn(top, '▶  Start node', self.start, kind='accent',
                                   height=34, width=136)
        self.btn_start.pack(side='right', padx=(0, 8), pady=(2, 0))
        self._tip(self.btn_start, 'Start the background search with the current settings.')
        self._tip(self.btn_stop, 'Gently stop the search (the current round will finish).')
        self._tip(self.btn_settings, 'Show / hide the search settings panel.')
        self._box(self._shell, bg=BORDER, height=1).pack(fill='x')       # hairline

        # settings | dashboard live in a PanedWindow so the split is mouse-draggable; the sash
        # position (and the chart height below) persist in gui_settings.json in raw 100%-DPI units
        body = tk.PanedWindow(self._shell, orient='horizontal', bg=BG, bd=0,
                              sashwidth=max(8, int(8 * self.SCALE)), sashpad=0, sashrelief='flat',
                              opaqueresize=True)
        body.pack(fill='both', expand=True, padx=20, pady=16)
        self._paned = body

        self._build_settings(body)
        self._build_status(body)
        self._apply_settings_vis()
        body.bind('<ButtonRelease-1>', self._sash_save)
        body.bind('<Double-1>', self._sash_reset)

    # ---------- theme ----------
    def _build_theme_pick(self, top):
        # A switch rather than a segmented button: CTkSegmentedButton has one text_color for both
        # states, so the selected label would lose its contrast against the accent fill.
        self.v_dark = tk.BooleanVar(value=self.cfg.get('theme') == 'dark')
        self.sw_theme = ctk.CTkSwitch(
            top, text=('Dark' if self.v_dark.get() else 'Light'), variable=self.v_dark,
            command=self._on_theme_pick, font=(self.UI, 13), text_color=MUT,
            progress_color=ACC, button_color=CARD, button_hover_color=CARD,
            fg_color=HEAD_BG, border_color=BORDER, switch_width=40, switch_height=20)
        self.sw_theme.pack(side='right', pady=(5, 0))
        self._tip(self.sw_theme, 'Light / dark appearance. The chart and the equity images\n'
                                 'are redrawn to match.')

    def _on_theme_pick(self):
        self._set_theme('dark' if self.v_dark.get() else 'light')

    def _set_theme(self, theme):
        """Re-palette and rebuild the window. A rebuild (rather than a live restyle) is what keeps
        the non-CTk parts — Treeview, the Canvas chart, the matplotlib PNGs — in the same theme."""
        if theme == self.cfg.get('theme'):
            return
        self.cfg['theme'] = theme
        self._save()                                     # keep the current field values across it
        self._tip_hide()
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
            self._pf_resize_after = None
        _apply_palette(theme)
        self._style()
        self._shell.destroy()
        self._pf_last_w = 0                              # force the equity image to re-render
        self._treesig = None                             # and the table to re-fill
        self._sig_shown = None
        self._build()
        self._set_running(bool(self.proc and self.proc.poll() is None))
        self._draw_chart()
        self._render_signal_rows()
        if self._pf_doc:
            self._render_portfolio(self._pf_doc)

    # ---------- left panel: ALL settings (scrollable, hidden by default) ----------
    def _toggle_settings(self):
        if self.cfg.get('settings_open'):
            self._sash_save()                            # remember the width the user chose
        self.cfg['settings_open'] = not self.cfg.get('settings_open')
        self._apply_settings_vis()
        self._save()

    def _apply_settings_vis(self):
        """Show/hide the settings pane (tk paned '-hide': the pane and its sash vanish together).
        The default view is just the dashboard; the choice persists across restarts."""
        shown = bool(self.cfg.get('settings_open'))
        try:
            self._paned.paneconfigure(self._settings_outer, hide=not shown)
        except tk.TclError:
            pass
        if shown and self.cfg.get('split_w'):
            self.root.after(120, self._sash_restore)
        self.btn_settings.configure(border_color=(ACC if shown else BORDER),
                                    text_color=(ACC if shown else TXT))

    # ---------- draggable split (settings | dashboard) ----------
    def _sash_restore(self):
        try:
            self._paned.sash_place(0, int(self.cfg['split_w'] * self.SCALE), 1)
        except tk.TclError:
            pass

    def _sash_save(self, _e=None):
        if not self.cfg.get('settings_open'):            # no visible sash — nothing to remember
            return
        try:
            x = self._paned.sash_coord(0)[0]
        except tk.TclError:
            return
        w = round(x / self.SCALE)
        if w != self.cfg.get('split_w'):
            self.cfg['split_w'] = w
            self._save()

    def _sash_reset(self, e):
        if not self._paned.identify(e.x, e.y):           # double-click somewhere else — ignore
            return
        self.cfg['split_w'] = 0
        nat = self._settings_inner.winfo_reqwidth() + int(48 * self.SCALE)   # content + paddings
        try:
            self._paned.sash_place(0, nat, 1)
        except tk.TclError:
            pass
        self._save()

    def _build_settings(self, body):
        # Always built (every tk variable must exist for _collect/start), but shown only while
        # cfg['settings_open'] — see _apply_settings_vis().
        outer = self._card(body)
        self._settings_outer = outer
        body.add(outer, minsize=int(230 * self.SCALE), stretch='never')
        # hand-rolled scroller rather than CTkScrollableFrame: the width has to come from the content
        # itself (_sync), which stays correct under HiDPI font scaling — a fixed width would clip.
        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0)
        vsb = ctk.CTkScrollbar(outer, orientation='vertical', command=canvas.yview,
                               fg_color=CARD, button_color=BORDER, button_hover_color=FAINT, width=14)
        canvas.configure(yscrollcommand=vsb.set)
        # the scrollbar packs FIRST: when the pane is dragged narrower than the content, whoever
        # packed last is squeezed out first — it must be the canvas that clips, not the scrollbar
        vsb.pack(side='right', fill='y', padx=(0, 6), pady=14)
        canvas.pack(side='left', fill='both', expand=True, padx=(14, 0), pady=14)
        inner = self._box(canvas)
        self._settings_inner = inner                     # natural width for the sash double-click reset
        canvas.create_window((0, 0), window=inner, anchor='nw')

        def _sync(_e=None):     # width is set by the content ITSELF — correct even with HiDPI font scaling
            canvas.configure(width=inner.winfo_reqwidth(), scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _sync)
        self._bind_wheel(canvas)

        self._head(inner, 'SEARCH SETTINGS').pack(anchor='w', pady=(0, 10))

        # --- resources ---
        self._lbl(inner, text='Resources (CPU share)', text_color=MUT,
                     font=(self.UI, 13)).pack(anchor='w')
        self.v_cpu = tk.IntVar(value=self.cfg['cpu'])
        self.lbl_cpu = self._lbl(inner, text='', text_color=TXT, font=(self.UI, 15, 'bold'))
        self.lbl_cpu.pack(anchor='w', pady=(2, 0))
        sc = ctk.CTkSlider(inner, from_=5, to=95, variable=self.v_cpu, command=lambda e: self._cpu_lbl(),
                           button_color=ACC, button_hover_color=ACC_HI, progress_color=ACC,
                           fg_color=HEAD_BG, height=14)
        sc.pack(fill='x', pady=(4, 12))
        self._cpu_lbl()
        cpu_tip = 'How many cores to give the search. More — faster, but higher load on the PC.'
        self._tip(self.lbl_cpu, cpu_tip)
        self._tip(sc, cpu_tip)

        # --- pairs universe ---
        self._lbl(inner, text='Which pairs to trade', text_color=MUT,
                     font=(self.UI, 13)).pack(anchor='w', pady=(0, 2))
        self.v_uniall = tk.BooleanVar(value=self.cfg['universe_all'])
        rb1 = self._radio(inner, 'All loaded pairs', self.v_uniall, True)
        rb1.pack(anchor='w', pady=2)
        rb2 = self._radio(inner, 'Custom list:', self.v_uniall, False)
        rb2.pack(anchor='w', pady=2)
        self.v_unilist = tk.StringVar(value=self.cfg['universe_list'])
        self.e_uni = self._entry(inner, self.v_unilist)
        self.e_uni.pack(fill='x', pady=(3, 4))
        self._uni_toggle()
        self._tip(rb1, 'Search across all downloaded pairs.')
        self._tip(rb2, 'Search only your own pairs (tickers, comma-separated).')
        self._tip(self.e_uni, 'Your pairs, comma-separated, e.g. BTCUSDT,ETHUSDT,SOLUSDT.')

        # --- market data (Binance) ---
        self._lbl(inner, text='MARKET DATA (BINANCE)', text_color=ACC,
                     font=(self.UI, 12, 'bold')).pack(anchor='w', pady=(14, 1))
        self._lbl(inner, text='candles the search runs on (pick the bar size below)', text_color=MUT,
                     font=(self.UI, 11)).pack(anchor='w', pady=(0, 6))
        g = self._box(inner)
        g.pack(fill='x')
        g.columnconfigure(0, weight=1)
        self.v_fetchn = self._num(g, 'How many pairs (top by turnover)', self.cfg.get('fetch_n', 150), 0, 5, 530, 10,
                                  tip='How many of the most liquid pairs to download from Binance.')
        self.v_minyears = self._num(g, 'Min. history (years)', self.cfg.get('fetch_years', 3), 1, 0, 7, 1,
                                    tip='Take only pairs older than N years — young ones have too little data.')
        tfrow = self._box(inner)
        tfrow.pack(fill='x', pady=(6, 0))
        self._lbl(tfrow, text='Timeframe (bar size)', text_color=TXT,
                     font=(self.UI, 13)).pack(side='left')
        self.v_tf = tk.StringVar(value=_tf_clean(self.cfg.get('timeframe', '1d')))
        tf_box = ttk.Combobox(tfrow, textvariable=self.v_tf, values=TF_CHOICES,
                              state='readonly', width=6)
        tf_box.pack(side='right')
        tf_box.bind('<<ComboboxSelected>>', self._on_tf_change)
        self._tip(tf_box, 'Bar size for the WHOLE pipeline: search, metrics, signals.\n'
                          '1d — the classic daily engine. Intraday (4h/1h/15m):\n'
                          '• each timeframe keeps its own data snapshot (data_<tf>.pickle)\n'
                          '  and its own alpha library — download data for it first;\n'
                          '• picking a timeframe fills in its recommended date segments\n'
                          '  and pair count (intraday history is shorter and denser);\n'
                          '• portfolio build and paper-trade are daily-only for now.')
        self.lbl_tf_note = self._lbl(inner, text='', text_color=MUT, font=(self.UI, 11),
                                     anchor='w', justify='left')
        self.lbl_tf_note.pack(anchor='w', pady=(3, 0))
        self.btn_fetch = self._btn(inner, 'Download fresh data from Binance', self._fetch_data)
        self.btn_fetch.pack(fill='x', pady=(10, 0))
        self._tip(self.btn_fetch, 'Download fresh candles at the selected timeframe from Binance\n'
                                  '(overwrites that timeframe\'s current data).')

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
                                   tip='Every N-th round — a search from scratch (for diversity); the other\n'
                                       'rounds refine the library champions (warm-start). N=1 means EVERY\n'
                                       'round explores from scratch and refinement never runs. Recommended: 3-4.')
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
        self.v_fitblocks = self._num(g, 'Robust blocks (0 = legacy)', self.cfg.get('fit_blocks', 5),
                                     4, 0, 12, 1,
                                     tip='Robust fitness: the selection span is cut into K regime\n'
                                         'blocks; each block\'s Sharpe is shrunk by its standard\n'
                                         'error and the fitness is the near-worst block — a formula\n'
                                         'must work everywhere, not shine once. 0 = the legacy\n'
                                         'min(TRAIN, VAL) fitness.')

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

        # --- buttons (Start/Stop live in the header — always visible) ---
        btns = self._box(inner)
        btns.pack(fill='x', pady=(16, 0))
        b_reset = self._btn(btns, 'Reset to defaults', self._reset)
        b_reset.pack(fill='x', pady=(0, 6))
        b_wipe = self._btn(btns, 'Clear all history', self._wipe_history, kind='danger')
        b_wipe.pack(fill='x', pady=(14, 0))
        self._tip(b_reset, 'Return all settings to their default values.')
        self._tip(b_wipe, 'Delete all history and found alphas (with confirmation).')

    # ---------- widget factories (one place where the palette meets CustomTkinter) ----------
    # Tonal buttons (soft fills, no outlines) — the filled surface IS the affordance.
    _BTN = {                                             # kind -> (fill, hover, text)
        'plain':  lambda: (HEAD_BG, HEAD_HI, TXT),
        'accent': lambda: (ACC, ACC_HI, '#ffffff'),
        'soft':   lambda: (HEAD_BG, HEAD_HI, TXT),
        'danger': lambda: (_mix(NEG, CARD, 0.88), _mix(NEG, CARD, 0.80), NEG),
    }

    def _btn(self, parent, text, command, kind='plain', height=32, **kw):
        fill, hover, fg = self._BTN[kind]()
        return ctk.CTkButton(parent, text=text, command=command, height=height, corner_radius=10,
                             fg_color=fill, hover_color=hover, text_color=fg, border_width=0,
                             text_color_disabled=FAINT,
                             font=(self.UI, 11, 'bold' if kind == 'accent' else 'normal'), **kw)

    def _entry(self, parent, var, width=None):
        kw = {'width': width} if width else {}
        return ctk.CTkEntry(parent, textvariable=var, height=30, corner_radius=9,
                            fg_color=HEAD_BG, border_color=BORDER, border_width=1,
                            text_color=TXT, font=(self.UI, 13), **kw)

    def _radio(self, parent, text, var, value):
        return ctk.CTkRadioButton(parent, text=text, variable=var, value=value,
                                  command=self._uni_toggle, font=(self.UI, 13), text_color=TXT,
                                  fg_color=ACC, hover_color=ACC_HI, border_color=FAINT,
                                  radiobutton_width=17, radiobutton_height=17)

    def _head(self, parent, text):
        """A panel heading — the small caps line above every card's content."""
        return self._lbl(parent, text=text, text_color=FAINT, font=(self.UI, 12, 'bold'))

    def _section(self, parent, title):
        row = self._box(parent)
        row.pack(anchor='w', fill='x', pady=(16, 6))
        tick = self._box(row, bg=ACC, width=max(2, int(3 * self.SCALE)),
                         height=int(13 * self.SCALE))
        tick.pack(side='left', padx=(0, 7))
        self._lbl(row, text=title, text_color=ACC, font=(self.UI, 12, 'bold')).pack(side='left')
        f = self._box(parent)
        f.pack(fill='x')
        f.columnconfigure(0, weight=1)
        return f

    def _row(self, parent, label, row, widget, tip):
        lbl = self._lbl(parent, text=label, text_color=MUT, font=(self.UI, 13))
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
        e = self._entry(parent, v, width=104)
        self._row(parent, label, row, e, tip)
        return v

    def _chk(self, parent, label, val, row, tip=None):
        v = tk.BooleanVar(value=bool(val))
        cb = ctk.CTkCheckBox(parent, text=label, variable=v, font=(self.UI, 13), text_color=TXT,
                             fg_color=ACC, hover_color=ACC_HI, border_color=FAINT,
                             checkbox_width=18, checkbox_height=18, corner_radius=5, border_width=2)
        cb.grid(row=row, column=0, columnspan=2, sticky='w', pady=(6, 3))
        if tip:
            self._tip(cb, tip)
        return v

    # ---------- dialogs ----------
    def _dialog(self, title, geometry):
        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.configure(fg_color=CARD)
        win.geometry(geometry)
        win.after(200, lambda: win.lift())               # CTkToplevel can open behind the main window
        return win

    def _console(self, win):
        """The log view of a child process — a terminal, so it stays dark in both themes."""
        txt = ctk.CTkTextbox(win, fg_color='#0f1115', text_color='#d7dce3', font=(self.MONO, 15),
                             wrap='word', border_width=0, corner_radius=8,
                             scrollbar_button_color='#333a46')
        txt.pack(fill='both', expand=True, padx=12, pady=12)
        return txt

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
        win = tk.Toplevel(self.root)                     # plain Toplevel: CTkToplevel can't go borderless
        win.wm_overrideredirect(True)
        try:
            win.attributes('-topmost', True)
        except tk.TclError:
            pass
        tk.Label(win, text=text, bg=TIP_BG, fg=TIP_FG, justify='left', font=(self.UI, 12),
                 padx=9, pady=6, wraplength=250, highlightbackground=TIP_BD,
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

    def _bind_wheel(self, canvas, through=False):
        """Wheel-scroll `canvas` while the pointer is anywhere inside it. With through=True,
        events over widgets that scroll themselves (Treeview / Text) are left to them."""
        def _w(e):
            if through:
                w = e.widget
                while w is not None and w is not canvas:
                    if isinstance(w, (ttk.Treeview, tk.Text)):
                        return
                    w = getattr(w, 'master', None)
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
    def _vgrip(self, parent, row, on_drag, on_reset, tip):
        """A full-width draggable gap between two cards: the ENTIRE space between the blocks is
        the handle (a thin accent bar lights up on hover), not a hairline you have to hunt for."""
        outer = tk.Frame(parent, bg=BG, height=max(16, int(16 * self.SCALE)),
                         cursor='sb_v_double_arrow')
        outer.grid(row=row, column=0, sticky='ew')
        handle = tk.Frame(outer, bg=BORDER, height=max(4, int(4 * self.SCALE)),
                          width=int(64 * self.SCALE), cursor='sb_v_double_arrow')
        handle.place(relx=0.5, rely=0.5, anchor='center')
        for w in (outer, handle):
            w.bind('<B1-Motion>', on_drag)
            w.bind('<ButtonRelease-1>', lambda e: self._save())
            w.bind('<Double-1>', on_reset)
            w.bind('<Enter>', lambda e: handle.configure(bg=ACC))
            w.bind('<Leave>', lambda e: handle.configure(bg=BORDER))
        self._tip(outer, tip)
        return outer

    def _build_status(self, body):
        # The dashboard column SCROLLS: on a short window the cards keep their natural heights
        # and the scrollbar moves between blocks; on a tall one the inner frame is stretched to
        # the viewport and the leaderboard absorbs the slack exactly as before (grid weight).
        outer = self._box(body, bg=BG)
        body.add(outer, minsize=int(420 * self.SCALE), stretch='always')
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ctk.CTkScrollbar(outer, orientation='vertical', command=canvas.yview,
                               fg_color=BG, button_color=BORDER, button_hover_color=FAINT,
                               width=12)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y', padx=(6, 0))
        canvas.pack(side='left', fill='both', expand=True)
        right = self._box(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=right, anchor='nw')

        def _fit(_e=None):
            w, h = canvas.winfo_width(), canvas.winfo_height()
            H = max(h, right.winfo_reqheight())
            canvas.itemconfigure(item, width=w, height=H)
            canvas.configure(scrollregion=(0, 0, w, H))
        canvas.bind('<Configure>', _fit)
        right.bind('<Configure>', _fit)      # cards appear/grow -> refresh the scrollregion
        self._bind_wheel(canvas, through=True)
        self._dash_canvas = canvas
        self._dash_right = right                         # row weights are set by _regrid_cards
        right.columnconfigure(0, weight=1)

        card = self._card(right)
        card.grid(row=0, column=0, sticky='ew')
        pad = self._pad(card)
        head = self._box(pad)
        head.pack(fill='x')
        self.pill_state = ctk.CTkFrame(head, fg_color=HEAD_BG, corner_radius=999, border_width=0)
        self.pill_state.pack(side='left')
        self.lbl_state = self._lbl(self.pill_state, text='● stopped', font=(self.UI, 14, 'bold'),
                                   text_color=MUT, bg=HEAD_BG)
        self.lbl_state.pack(padx=int(13 * self.SCALE), pady=int(5 * self.SCALE))
        self.lbl_res = self._lbl(head, text='', text_color=MUT, font=(self.UI, 13))
        self.lbl_res.pack(side='right', pady=(4, 0))

        stats = self._box(pad)
        stats.pack(fill='x', pady=(14, 0))
        self.s_rounds = self._stat(stats, 'rounds', 0)
        self.s_trials = self._stat(stats, 'formulas tried', 1)
        self.s_found = self._stat(stats, 'alphas found', 2)
        self.lbl_cur = self._lbl(pad, text='', text_color=MUT, font=(self.MONO, 12),
                                    anchor='w', justify='left')
        self.lbl_cur.pack(anchor='w', fill='x', pady=(12, 0))
        # LIVE LOG — the node's human-readable activity feed (status.json 'events')
        logwrap = ctk.CTkFrame(pad, fg_color=STRIPE, corner_radius=10, border_width=0)
        logwrap.pack(fill='x', pady=(10, 0))
        self.logbox = tk.Text(logwrap, height=7, bg=STRIPE, fg=MUT, bd=0, highlightthickness=0,
                              font=(self.MONO, 11), wrap='word', state='disabled',
                              cursor='arrow', padx=6, pady=4)
        for tag, col in (('round', TXT), ('llm', ACC), ('best', POS), ('polish', ACC_HI),
                         ('warn', NEG), ('err', NEG), ('ts', FAINT), ('i', MUT)):
            self.logbox.tag_configure(tag, foreground=col)
        self.logbox.pack(fill='both', expand=True, padx=8, pady=6)
        self._events_last = None
        self._log_placeholder()

        self._build_signals_card(right)                  # row 1 — hidden while nothing is served

        chart_card = self._card(right)
        chart_card.grid(row=2, column=0, sticky='ew', pady=(16, 0))
        cpad = self._pad(chart_card)
        self._head(cpad, 'PROGRESS — FITNESS min(train,val) BY ROUND  ·  TEST kept held-out').pack(
            anchor='w', pady=(0, 8))
        ch = int((self.cfg.get('chart_h') or 170) * self.SCALE)
        self.chart = tk.Canvas(cpad, height=ch, bg=CARD, highlightthickness=0, cursor='hand2')
        self.chart.pack(fill='x')
        self.chart.bind('<Configure>', lambda e: self._draw_chart())
        self.chart.bind('<Double-1>', self._progress_interactive)  # live zoomable copy
        self._tip(self.chart, 'Double-click — interactive chart: zoom with the mouse wheel,\n'
                              'pan with the toolbar; adds the TEST curve and library growth.')
        # the whole gap below this card is the drag handle (see _vgrip in the row layout)

        self._chart_grip = self._vgrip(right, 3, self._on_chart_drag, self._chart_reset,
                                       'Drag: the chart above grows/shrinks, the leaderboard '
                                       'absorbs it.\nDouble-click — default chart height.')
        card2 = self._card(right)
        card2.grid(row=4, column=0, sticky='nsew')
        p2 = self._pad(card2)
        hrow = self._box(p2)
        hrow.pack(fill='x', pady=(0, 8))
        self._lb_head_text = self._lb_head_text_for('fit')
        self.lbl_lb_head = self._head(hrow, self._lb_head_text)
        # Packed BEFORE the heading: the heading is a long line, and whoever packs first wins the
        # space — pack it first and the button gets squeezed to nothing on a narrow window.
        # width/height are raw: CTk scales them itself (set_widget_scaling), unlike a tk pixel.
        self.btn_lb_csv = self._btn(hrow, 'CSV', self._export_library, height=24, width=52)
        self.btn_lb_csv.pack(side='right', padx=(10, 0))
        # All ↔ Families toggle. ON collapses the table to the best alpha per family (the old view);
        # OFF (default) shows every alpha the node has mined. A CTkSwitch, not a segmented button:
        # 6.0.0's segmented button has no selected_text_color, so its active label loses contrast.
        self.v_lbfam = tk.BooleanVar(value=(self._lb_mode == 'families'))
        self.sw_lbfam = ctk.CTkSwitch(hrow, text='families only', variable=self.v_lbfam,
                                      command=self._toggle_lb_mode, onvalue=True, offvalue=False,
                                      font=(self.UI, 11), text_color=MUT, progress_color=ACC,
                                      button_color=TXT, fg_color=HEAD_BG, switch_width=34,
                                      switch_height=16)
        self.sw_lbfam.pack(side='right', padx=(0, 4))
        self.lbl_lb_head.pack(side='left', anchor='w')
        self._tip(self.btn_lb_csv, 'Download EVERY alpha the node has mined — the whole library,\n'
                                   'no dedup, no TEST filter, with all TRAIN/VAL/TEST numbers.')
        self._tip(self.sw_lbfam, 'OFF: show every alpha in the library (scroll the full list).\n'
                                 'ON: collapse to the best alpha per family (distinct formulas),\n'
                                 'the old compact view. Sorting by any column works in both.')
        wrap = self._box(p2)
        wrap.pack(fill='both', expand=True)
        cols = ('rank', 'fit', 'test', 'dd', 'cagr', 'srt', 'ls', 'act', 'win', 'id', 'formula')
        self.tree = ttk.Treeview(wrap, columns=cols, show='headings', height=12)
        self._HEAD = {}
        # Widths fit the WIDEST real value plus the sort arrow the heading grows by (' ▼'), and are
        # scaled with the display: a Treeview column is raw pixels while its text follows the DPI,
        # which is what cut "3069/2100" down to "3069/:" and clipped the "tr/yr·a" heading itself.
        for c, txt, w, anc in (('rank', '#', 40, 'center'), ('fit', 'fitness', 86, 'e'),
                               ('test', 'TEST OOS', 86, 'e'), ('dd', 'maxDD', 74, 'e'),
                               ('cagr', 'CAGR', 72, 'e'), ('srt', 'sortino', 80, 'e'),
                               ('ls', 'trades L/S', 100, 'center'),
                               ('act', 'tr/yr·a', 72, 'e'),
                               ('win', 'win%', 62, 'e'), ('id', 'ID', 116, 'w'),
                               ('formula', 'formula', 260, 'w')):
            self._HEAD[c] = txt
            kw = {} if c in ('rank', 'id') else {'command': (lambda c=c: self._sort_by(c))}
            w = int(w * self.SCALE)
            self.tree.heading(c, text=txt, **kw)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == 'formula'), minwidth=w)
        self._update_headings()                          # show the sort arrow on the active column
        self._tip(self.lbl_lb_head, 'maxDD = worst peak-to-trough drawdown; CAGR = annualized growth;\n'
                                    'sortino = like Sharpe but only downside vol counts (upside is free);\n'
                                    'trades L/S = total number of long / short positions OPENED over TEST\n'
                                    '(a trade = crossing into long/short from flat or the opposite side);\n'
                                    'tr/yr·a = trades per asset per year (relative activity — the "min tr/yr"\n'
                                    'filter drops barely-trading alphas); win% = share of days with profit.\n'
                                    'All on TEST (OOS), on target weights (daily rebalance).')
        self.tree.tag_configure('pos', foreground=POS)
        self.tree.tag_configure('neg', foreground=NEG)
        self.tree.tag_configure('odd', background=STRIPE)
        self.tree.tag_configure('even', background=CARD)
        vsb = ctk.CTkScrollbar(wrap, orientation='vertical', command=self.tree.yview, fg_color=CARD,
                               button_color=BORDER, button_hover_color=FAINT, width=14)
        self._vsb = vsb
        # formulas render FULL length: the column is sized to the widest row and the horizontal
        # bar scrolls to the tail (_fit_formula_col); measuring needs the tree's own font
        self._tree_font = tkfont.Font(family=self.MONO, size=self._px(12))
        self._lb_need_px = 0
        hsb = ctk.CTkScrollbar(wrap, orientation='horizontal', command=self.tree.xview,
                               fg_color=CARD, button_color=BORDER, button_hover_color=FAINT,
                               height=14)
        self.tree.configure(yscrollcommand=self._on_tree_scroll,   # scroll -> load metrics for the viewport
                            xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y', padx=(4, 0))
        hsb.pack(side='bottom', fill='x', pady=(4, 0))
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Configure>', lambda e: self._fit_formula_col())
        self.tree.bind('<Double-1>', self._on_row_open)
        self.tree.bind('<Button-3>', self._on_row_menu)             # right-click — context menu
        self.tree.bind('<Control-c>', lambda e: self._copy_formula())
        self.tree.bind('<Control-C>', lambda e: self._copy_formula())
        self._menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TXT, activebackground=ACC_SOFT,
                             activeforeground=TXT, borderwidth=0, font=(self.UI, 13))
        self._menu.add_command(label='Copy formula', command=self._copy_formula)
        self._menu.add_command(label='Copy formula + metrics', command=self._copy_full)
        self._menu.add_separator()
        self._menu.add_command(label='Export table (CSV)…', command=self._export_visible)
        self._menu.add_command(label='Export full library (CSV)…', command=self._export_library)
        self._menu.add_separator()
        self._menu.add_command(label='Show equity', command=self._open_selected_plot)

        # ---- PORTFOLIO panel (combine top-N via the real engine; TEST- or fitness-ranked) ----
        self._vgrip(right, 5, self._on_pf_grip, self._pf_grip_reset,
                    'Drag up — a taller portfolio equity plot (the leaderboard shrinks);\n'
                    'drag down — more room for the leaderboard. Double-click — automatic height.')
        card3 = self._card(right)
        card3.grid(row=6, column=0, sticky='ew')
        self.pf_card = card3
        p3 = self._pad(card3)
        hp = self._box(p3)
        hp.pack(fill='x')
        self._head(hp, 'PORTFOLIO — top-N combined via the real engine').pack(side='left')
        ctl = self._box(hp)
        ctl.pack(side='right')
        self._lbl(ctl, text='top', text_color=MUT, font=(self.UI, 13)).pack(side='left', padx=(0, 5))
        self.v_pfn = tk.IntVar(value=6)
        ttk.Spinbox(ctl, from_=2, to=20, width=4, textvariable=self.v_pfn).pack(side='left', padx=(0, 8))
        self._lbl(ctl, text='by', text_color=MUT, font=(self.UI, 13)).pack(side='left', padx=(0, 5))
        self.v_pfsel = tk.StringVar(value='TEST')
        sel_box = ttk.Combobox(ctl, textvariable=self.v_pfsel, values=('TEST', 'fitness'),
                               state='readonly', width=7)
        sel_box.pack(side='left', padx=(0, 8))
        self._tip(sel_box, 'How the top-N members are picked from the library:\n'
                           '• TEST — by held-out TEST Sharpe: what actually worked on 2023+.\n'
                           '  ⚠ the shown combined TEST becomes optimistic (the same window\n'
                           '  picked the members) — validate with Paper before trusting it.\n'
                           '• fitness — by min(train,val): TEST never enters selection, so\n'
                           '  the combined TEST numbers are honest out-of-sample.')
        self.btn_pf = self._btn(ctl, '▶ Build portfolio', self._build_portfolio, kind='accent')
        self.btn_pf.pack(side='left')
        self._tip(self.btn_pf, 'Runs the top-N library alphas (ranked per the "by" selector)\n'
                               'through the project Portfolio engine (real simulation, ~1–2 min\n'
                               'in the background) and shows the combined dollar-neutral equity\n'
                               'on TEST.')
        self.btn_pf_csv = self._btn(ctl, 'CSV', self._pf_download_signals, width=76)
        self.btn_pf_csv.configure(state='disabled')
        self.btn_pf_csv.pack(side='left', padx=(8, 0))
        self.btn_pf_paper = self._btn(ctl, 'Paper', self._pf_paper_trade, width=86)
        self.btn_pf_paper.configure(state='disabled')
        self.btn_pf_paper.pack(side='left', padx=(6, 0))
        self.btn_pf_sig = self._btn(ctl, 'Serve', self._pf_serve_signal, width=86)
        self.btn_pf_sig.configure(state='disabled')
        self.btn_pf_sig.pack(side='left', padx=(6, 0))
        self.btn_pf_pdf = self._btn(ctl, 'PDF', self._pf_pdf_report, width=64)
        self.btn_pf_pdf.configure(state='disabled')
        self.btn_pf_pdf.pack(side='left', padx=(6, 0))
        self.btn_pf_track = self._btn(ctl, 'Track', self._pf_fwd_enroll, width=76)
        self.btn_pf_track.configure(state='disabled')
        self.btn_pf_track.pack(side='left', padx=(6, 0))
        self._tip(self.btn_pf_track, 'Enroll this exact portfolio into the FORWARD TRACK below:\n'
                                     'the strategy is frozen (formulas + universe + vol/fee) and\n'
                                     'paper-stepped once per closed daily bar, append-only.')
        self._tip(self.btn_pf_pdf, 'Portfolio analytics dashboard as PDF: KPIs, equity,\n'
                                   'exposure and turnover, weight structure, monthly\n'
                                   'returns and conclusions (TEST period).')
        self._tip(self.btn_pf_csv, 'Download a CSV of the combined portfolio signals — the target\n'
                                   'weight per asset per bar over TRAIN/VAL/TEST (each row labeled\n'
                                   'with its segment), with the asset\'s OHLCV on that bar —\n'
                                   'same as for a single alpha. Intraday timeframes: TEST only.')
        self._tip(self.btn_pf_paper, 'Build a self-contained paper-trading bundle for the whole\n'
                                     'portfolio (all N alphas combined via the real Portfolio engine)\n'
                                     'to run daily on live Binance data — same as for a single alpha.')
        self._tip(self.btn_pf_sig, 'Start a local signal API for the whole portfolio — serves the\n'
                                   'combined live target positions as JSON on localhost. Each\n'
                                   'service takes the next free port from 8799 and appears in the\n'
                                   'SIGNAL API card above (URL, log, "free the port").')
        self.lbl_pf = self._lbl(p3, text_color=FAINT, font=(self.UI, 12), wraplength=900,
                                   anchor='w', justify='left',
                                   text='⚠ selecting by TEST inflates the number (cherry-pick); the '
                                        'diversification gain — combined ≫ any single alpha — is the real part.')
        self.lbl_pf.pack(anchor='w', fill='x', pady=(8, 2))
        self.lbl_pf_m = self._lbl(p3, text='', text_color=TXT, font=(self.UI, 16, 'bold'),
                                     anchor='w')
        self.lbl_pf_m.pack(anchor='w')
        self.pf_img = tk.Label(p3, bg=CARD, borderwidth=0, cursor='hand2')
        self.pf_img.pack(fill='x', pady=(8, 0))
        self.pf_img.bind('<Double-1>', self._pf_interactive)  # live zoomable copy of the chart
        self._tip(self.pf_img, 'Double-click — interactive chart: zoom with the mouse wheel,\n'
                               'pan, reset. The card itself keeps the auto-fitted image.')
        card3.bind('<Configure>', self._on_pf_resize)         # re-render equity to the panel width
        self.root.after(500, self._load_portfolio_on_start)   # show last build, if any

        # ---- FORWARD TRACK (append-only paper stepping of enrolled strategies) ----
        card4 = self._card(right)
        card4.grid(row=7, column=0, sticky='ew', pady=(14, 0))
        p4 = self._pad(card4)
        hf = self._box(p4)
        hf.pack(fill='x')
        self._head(hf, 'FORWARD TRACK — daily paper steps, append-only').pack(side='left')
        fctl = self._box(hf)
        fctl.pack(side='right')
        self.btn_fwd_step = self._btn(fctl, '▶ Step now', self._fwd_step, width=110)
        self.btn_fwd_step.pack(side='left')
        self._tip(self.btn_fwd_step, 'Run one paper step for every enrolled strategy: download the\n'
                                     'latest CLOSED daily bars from Binance, recompute the target\n'
                                     'positions with the real engine, mark-to-market, rebalance, fees.\n'
                                     'Runs automatically once per closed bar while the app is open.')
        self.btn_fwd_chart = self._btn(fctl, 'Chart', self._fwd_chart, width=76)
        self.btn_fwd_chart.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_chart, 'Forward equity of the selected row — only live steps,\n'
                                      'nothing recomputed backwards.')
        self.btn_fwd_sig = self._btn(fctl, 'Signals', self._fwd_signals, width=86)
        self.btn_fwd_sig.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_sig, 'Step-by-step log of the selected strategy: the held book\n'
                                    '(signed % of equity per asset), the executed rebalances,\n'
                                    'P&L and fees of every live step — exportable as CSV.')
        self.btn_fwd_arch = self._btn(fctl, 'Archive', self._fwd_archive, width=90)
        self.btn_fwd_arch.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_arch, 'Stop stepping the selected strategy. Its history stays in\n'
                                     'state/forward.json (append-only) — the row just leaves the table.')
        self.lbl_fwd = self._lbl(p4, text='', text_color=FAINT, font=(self.UI, 12),
                                 wraplength=900, anchor='w', justify='left')
        self.lbl_fwd.pack(anchor='w', fill='x', pady=(8, 2))
        fcols = ('id', 'kind', 'enrolled', 'days', 'equity', 'ret', 'sharpe', 'dd', 'last')
        self.fwd_tree = ttk.Treeview(p4, columns=fcols, show='headings', height=4)
        for c, txt, w, anch in (('id', 'STRATEGY', 240, 'w'), ('kind', 'KIND', 96, 'w'),
                                ('enrolled', 'ENROLLED', 100, 'center'), ('days', 'STEPS', 64, 'e'),
                                ('equity', 'EQUITY', 100, 'e'), ('ret', 'RETURN', 90, 'e'),
                                ('sharpe', 'SHARPE', 80, 'e'), ('dd', 'MAXDD', 80, 'e'),
                                ('last', 'LAST STEP', 110, 'center')):
            self.fwd_tree.heading(c, text=txt)
            self.fwd_tree.column(c, width=int(w * self.SCALE), anchor=anch,
                                 stretch=(c == 'id'))
        self.fwd_tree.pack(fill='x', pady=(4, 0))
        self.fwd_tree.bind('<Double-1>', lambda _e: self._fwd_chart())
        self.root.after(900, self._fwd_refresh)
        if not getattr(self, '_fwd_tick_on', False):          # _build reruns on theme switch —
            self._fwd_tick_on = True                          # keep exactly one tick loop
            self.root.after(20_000, self._fwd_tick)

        # ---- the dashboard cards are REORDERABLE: drag one by its padding / header strip ----
        self._cards = {'status': card, 'signals': self.sig_card, 'chart': chart_card,
                       'leaderboard': card2, 'portfolio': card3, 'forward': card4}
        self._wire_card_drag('status', pad, head, stats)
        self._wire_card_drag('chart', cpad)
        self._wire_card_drag('leaderboard', p2, hrow)
        self._wire_card_drag('portfolio', p3, hp)
        self._wire_card_drag('forward', p4, hf)
        self._regrid_cards()

    # ---------- reorderable dashboard cards ----------
    DASH_CARDS = ('status', 'signals', 'chart', 'leaderboard', 'portfolio', 'forward')

    def _card_order(self):
        saved = [k for k in (self.cfg.get('card_order') or []) if k in self.DASH_CARDS]
        return saved + [k for k in self.DASH_CARDS if k not in saved]

    def _regrid_cards(self):
        """Grid the dashboard cards in the user's order. The chart's resize grip always rides
        right below the chart card; the leaderboard's row soaks up the window slack wherever
        it lands; a card hidden via grid_remove (signals while idle) keeps its slot hidden."""
        right = self._dash_right
        hidden = {n for n, w in self._cards.items() if not w.winfo_manager()}
        for r in range(2 * len(self._cards)):
            right.rowconfigure(r, weight=0)
        row, after_grip = 0, False
        for i, name in enumerate(self._card_order()):
            w = self._cards[name]
            w.grid(row=row, column=0, sticky=('nsew' if name == 'leaderboard' else 'ew'),
                   pady=((0 if (i == 0 or after_grip) else 16), 0))
            if name in hidden:
                w.grid_remove()                          # grid() above re-showed it — undo
            if name == 'leaderboard':
                right.rowconfigure(row, weight=1)
            after_grip = False
            row += 1
            if name == 'chart':
                self._chart_grip.grid(row=row, column=0, sticky='ew')
                row += 1
                after_grip = True                        # the grip itself is the 16px gap

    def _wire_card_drag(self, name, *widgets):
        for w in widgets:
            w.bind('<ButtonPress-1>', lambda e, n=name: self._card_press(e, n))
            w.bind('<B1-Motion>', self._card_motion)
            w.bind('<ButtonRelease-1>', self._card_release)

    def _card_press(self, e, name):
        self._cdrag = {'name': name, 'y0': e.y_root, 'live': False}

    def _card_motion(self, e):
        d = getattr(self, '_cdrag', None)
        if d is None:
            return
        if not d['live']:
            if abs(e.y_root - d['y0']) < int(12 * self.SCALE):
                return                                   # a stray click, not a drag yet
            d['live'] = True
            try:                                         # accent border marks the card in flight
                self._cards[d['name']].configure(border_width=2, border_color=ACC)
            except tk.TclError:
                pass
        order = self._card_order()
        vis = [n for n in order if n != d['name'] and self._cards[n].winfo_manager()]
        place = len(vis)
        for i, n in enumerate(vis):                      # insert before the first card whose
            w = self._cards[n]                           # midpoint the pointer is above
            if e.y_root < w.winfo_rooty() + w.winfo_height() / 2:
                place = i
                break
        new = vis[:place] + [d['name']] + vis[place:]
        for h in (n for n in order if n != d['name'] and n not in vis):   # weave hidden back
            nxt = next((s for s in order[order.index(h) + 1:] if s in new), None)
            new.insert(new.index(nxt) if nxt is not None else len(new), h)
        if new != order:
            self.cfg['card_order'] = new
            self._regrid_cards()                         # live feedback while dragging

    def _card_release(self, _e):
        d = getattr(self, '_cdrag', None)
        self._cdrag = None
        if d and d.get('live'):
            try:
                self._cards[d['name']].configure(border_width=CARD_BW, border_color=BORDER)
            except tk.TclError:
                pass
            self._save()

    def _load_portfolio_on_start(self):
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception:                                # noqa: BLE001
            return
        self._render_portfolio(doc)

    def _reset_portfolio_ui(self):
        """Clear the Portfolio panel (after a history wipe, or when nothing is built)."""
        self._pf_doc = None
        self._pf_last_w = 0
        self.lbl_pf.configure(text='no portfolio yet — set "top" and click "Build portfolio".')
        self.lbl_pf_m.configure(text='')
        self.pf_img.config(image='')
        self._pf_img_ref = None
        for b in (self.btn_pf_csv, self.btn_pf_paper, self.btn_pf_sig, self.btn_pf_track):
            b.configure(state='disabled')

    def _stat(self, parent, label, col):
        """A stat tile: big number + caption on its own soft rounded surface."""
        tile = ctk.CTkFrame(parent, fg_color=HEAD_BG, corner_radius=12, border_width=0)
        tile.grid(row=0, column=col, sticky='w', padx=(0, 12))
        f = self._box(tile, bg=HEAD_BG)
        f.pack(padx=int(16 * self.SCALE), pady=int(9 * self.SCALE))
        val = self._lbl(f, text='0', text_color=TXT, font=(self.UI, 28, 'bold'),
                        bg=HEAD_BG, anchor='w')
        val.pack(anchor='w')
        self._lbl(f, text=label.upper(), text_color=FAINT, font=(self.UI, 10, 'bold'),
                  bg=HEAD_BG, anchor='w').pack(anchor='w')
        return val

    # ---------- helpers ----------
    def _cpu_lbl(self):
        pct = int(self.v_cpu.get())
        self.lbl_cpu.configure(text=f'{pct}%  →  {max(1, round(pct/100*CORES))} of {CORES} cores')

    def _uni_toggle(self):
        self.e_uni.configure(state='disabled' if self.v_uniall.get() else 'normal')

    def _reset(self):
        keep = {k: self.cfg.get(k) for k in ('theme', 'settings_open')}   # appearance, not search
        self.cfg = dict(DEFAULTS)
        self.cfg.update(keep)
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
        n_alphas = self._count_lines(self._lib_file())
        n_rounds = self._count_lines(os.path.join(STATE_DIR, f'history{_tf_suffix(self._tf())}.jsonl'))
        if not (n_alphas or n_rounds or os.path.exists(STATUS_FILE)):
            messagebox.showinfo('Empty', 'History is already empty — nothing to clear.', parent=self.root)
            return
        msg = ('Delete ALL run history? This action is irreversible.\n\n'
               f'• {n_alphas} found alphas  (library.jsonl)\n'
               f'• {n_rounds} rounds and the chart  (history.jsonl)\n'
               '• current status  (status.json)\n'
               '• the built portfolio  (portfolio.json)\n\n'
               'Search settings (the parameters on the left) will remain.')
        if not messagebox.askyesno('Full clear', msg, icon='warning',
                                    default='no', parent=self.root):
            return
        import glob
        removed = 0
        wipe = ['status.json', 'portfolio.json']         # every timeframe's library/history goes too
        for pat in ('library*.jsonl', 'history*.jsonl'):
            wipe += [os.path.basename(p) for p in glob.glob(os.path.join(STATE_DIR, pat))]
        for name in wipe:
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
        try:
            os.remove(PORTFOLIO_PNG)                      # the portfolio equity image
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
                f'as {self._tf()} candles and update the market data?\n\n'
                f'The current {self._tf()} snapshot will be replaced. It will take a few minutes '
                '(intraday: longer, much more data) and needs internet.\n'
                'After the update the pairs universe will change — clear history and restart the search.',
                icon='warning', default='no', parent=self.root):
            return
        self._save()
        self._run_fetch(['--top', str(n), '--min-years', str(yrs), '--interval', self._tf(),
                         '--out', self._data_file()],
                        f'Data update — top-{n} from Binance',
                        f'Downloading the {n} highest-turnover Binance pairs (history ≥ {yrs} years)…\n\n',
                        '✓ Done — data updated. Clear history and restart the search.')

    def _run_fetch(self, args, title, intro, done_msg, on_success=None):
        """Console dialog + fetch_data subprocess; shared by the manual data update and the
        first-run bootstrap. on_success fires (on the Tk thread) only on exit code 0."""
        if self._fetching:
            return
        self._fetching = True
        win = self._dialog(title, '760x440')
        txt = self._console(win)

        def add(s):
            if not win.winfo_exists():
                return
            txt.configure(state='normal')
            txt.insert('end', s)
            txt.see('end')
            txt.configure(state='disabled')

        add(intro)
        try:
            proc = subprocess.Popen(_child_cmd('fetch') + args,
                                    cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                       # noqa: BLE001
            add(f'Failed to launch fetch_data.py: {e}\n')
            self._fetching = False
            return
        q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)
        threading.Thread(target=_reader, daemon=True).start()
        self.btn_fetch.configure(state='disabled')

        def pump():
            if not win.winfo_exists():
                self.btn_fetch.configure(state='normal')   # the process will finish on its own, re-enable the button
                self._fetching = False
                return
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        code = proc.poll()
                        add('\n' + (done_msg if code == 0
                                    else f'✗ Error (code {code}). Data left untouched.') + '\n')
                        self.btn_fetch.configure(state='normal')
                        self._fetching = False
                        self._lib_cache['mtime'] = None
                        if code == 0 and on_success is not None:
                            win.after(700, on_success)
                        return
                    add(line)
            except queue.Empty:
                pass
            win.after(150, pump)
        win.after(150, pump)

    def _maybe_bootstrap(self):
        """First run: no market data next to the node -> download a starter universe on our own."""
        if os.path.exists(self._data_file()) or (self.proc and self.proc.poll() is None):
            return
        self._bootstrap_data()

    def _bootstrap_data(self, on_success=None):
        tf = self._tf()
        intro = ('No market data found next to the node.\n'
                 'Downloading a starter universe of 10 majors — BTC, ETH, SOL, XRP, BNB, DOGE, '
                 f'ADA, LINK, AVAX, LTC — as {tf} candles from Binance…\n\n'
                 'You can widen the universe any time: Settings → MARKET DATA → '
                 '"Download fresh data from Binance".\n\n')
        if tf != '1d':
            intro += ('Note: intraday history is shorter — if the search has nothing to evaluate, '
                      'set later TRAIN/VAL dates in Settings.\n\n')
        self._run_fetch(['--symbols', BOOTSTRAP_SYMBOLS, '--interval', tf, '--out', self._data_file()],
                        f'First run — market data bootstrap ({tf})', intro,
                        '✓ Starter data ready.' + ('' if on_success
                                                   else ' Press START to begin the search.'),
                        on_success=on_success)

    def _reset_ui_after_wipe(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._treesig = None
        self._shown = []
        self._lb_need_px = 0                              # collapse the formula column back
        self._fit_formula_col()
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self._history = []
        self._draw_chart()
        self.s_rounds.configure(text='0')
        self.s_trials.configure(text='0')
        self.s_found.configure(text='0')
        self.lbl_cur.configure(text='')
        self._state_pill('● stopped', MUT)
        self._reset_portfolio_ui()                        # clear the Portfolio panel too

    def _apply_cfg_to_widgets(self):
        c = self.cfg
        self.v_cpu.set(c['cpu']); self._cpu_lbl()
        self.v_uniall.set(c['universe_all']); self.v_unilist.set(c['universe_list']); self._uni_toggle()
        self.v_pop.set(c['pop']); self.v_gens.set(c['gens']); self.v_seed.set(c['seed'])
        self.v_pause.set(c['pause']); self.v_port.set(c['port'])
        self.v_fetchn.set(c['fetch_n']); self.v_minyears.set(c['fetch_years'])
        self.v_tf.set(_tf_clean(c.get('timeframe', '1d'))); self._tf_note()
        self.v_explore.set(c['explore_every']); self.v_maxrounds.set(c['max_rounds'])
        self.v_leader.set(c['leaderboard']); self.v_seedlib.set(c['seed_from_lib'])
        self.v_vol.set(c['target_vol']); self.v_exec.set(c['exec_cost'])
        self.v_depth.set(c['max_depth']); self.v_size.set(c['max_size'])
        self.v_tourn.set(c['tournament']); self.v_elit.set(c['elitism'])
        self.v_inject.set(c['random_inject']); self.v_cx.set(c['crossover_prob'])
        self.v_pars.set(c['parsimony']); self.v_corrt.set(c['corr_threshold'])
        self.v_corrp.set(c['corr_penalty']); self.v_hof.set(c['hof_capacity'])
        self.v_fitblocks.set(c.get('fit_blocks', 5))
        self.v_train.set(c['train_start']); self.v_val.set(c['val_start'])
        self.v_test.set(c['test_start']); self.v_end.set(c['test_end'])

    def _set_running(self, running):
        self.btn_start.configure(state='disabled' if running else 'normal')
        self.btn_stop.configure(state='normal' if running else 'disabled')

    def _state_pill(self, text, color):
        """The status pill: text + a soft wash of the state colour behind it."""
        tint = HEAD_BG if color == MUT else _mix(color, CARD, 0.87)
        self.pill_state.configure(fg_color=tint)
        self.lbl_state.configure(text=text, fg=color, bg=tint)

    # ---------- timeframe-aware paths ----------
    def _on_tf_change(self, _evt=None):
        """Picking a timeframe fills in its recommended date segments and pair count (from
        evolution/timeframe.py). These are only sensible defaults — the user can still edit the
        fields. Finer bars get a shorter, later window (intraday history is denser and heavier)."""
        try:
            from timeframe import resolve as _rtf
            t = _rtf(self.v_tf.get())
        except Exception:                                # noqa: BLE001
            return
        seg = t.segments
        self.v_train.set(seg['train_start']); self.v_val.set(seg['val_start'])
        self.v_test.set(seg['test_start']); self.v_end.set(seg['test_end'])
        self.v_fetchn.set(t.max_pairs)
        self._tf_note()

    def _tf_note(self):
        """Refresh the one-line hint under the timeframe selector (no field changes)."""
        try:
            from timeframe import resolve as _rtf
            t = _rtf(self.v_tf.get())
        except Exception:                                # noqa: BLE001
            return
        note = ('daily engine — full history' if t.name == '1d' else
                f'{t.name} bars · recommended history from {t.history} · '
                f'download {t.name} data first, then Start')
        try:
            self.lbl_tf_note.configure(text=note)
        except (AttributeError, tk.TclError):
            pass

    def _tf(self):
        """The configured bar size ('1d','4h','1h','15m'); the whole pipeline follows it."""
        return _tf_clean(self.cfg.get('timeframe'))

    def _data_file(self):
        """Per-timeframe data snapshot: the classic data.pickle for 1d, data_<tf>.pickle else."""
        root, ext = os.path.splitext(apppaths.data_path())
        return f'{root}{_tf_suffix(self._tf())}{ext}'

    def _lib_file(self):
        """Per-timeframe alpha library: alphas mined on different bar sizes never mix."""
        return os.path.join(STATE_DIR, f'library{_tf_suffix(self._tf())}.jsonl')

    def _tf_gate(self, what):
        """True (and explains itself) when `what` is daily-only and an intraday tf is active."""
        if self._tf() == '1d':
            return False
        messagebox.showinfo(
            'Daily only (for now)',
            f'{what} runs through the real quantpylib engine, which is tuned for daily bars — '
            f'on {self._tf()} its numbers would be silently wrong.\n\n'
            'On intraday timeframes use the search, the leaderboard, equity charts, CSV signals '
            'and PDF reports. Portfolio/paper support is the next phase.', parent=self.root)
        return True

    # ---------- start/stop ----------
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self._save()
        c = self.cfg
        os.makedirs(STATE_DIR, exist_ok=True)
        if not os.path.exists(self._data_file()):
            # first run without a snapshot: fetch the starter universe, then continue START
            self._bootstrap_data(on_success=self.start)
            return
        env = dict(os.environ)
        env.update(
            ALPHANODE_CPU_PERCENT=str(c['cpu']),
            ALPHANODE_UNIVERSE=('all' if c['universe_all'] else c['universe_list']),
            ALPHANODE_POP=str(c['pop']), ALPHANODE_GENS=str(c['gens']),
            ALPHANODE_SEED=str(c['seed']), ALPHANODE_PAUSE=str(c['pause']),
            ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_STATUS_PORT=str(c['port']),
            ALPHANODE_TF=self._tf(),
            ALPHANODE_DATA=self._data_file(),      # per-timeframe snapshot (fresh/bundled)
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
            ALPHANODE_FIT_BLOCKS=str(c['fit_blocks']),
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
        self.btn_stop.configure(state='disabled')

    def _on_close(self):
        try:
            if self._sigs:                               # they outlive the GUI unless asked to stop
                if messagebox.askyesno(
                        'Signal API', f'{len(self._sigs)} signal API service(s) are still running.\n\n'
                        'Stop them and free their ports?\n\n'
                        'No — leave them serving; AlphaNode picks them up again on the next start.',
                        parent=self.root):
                    self._stop_all_signals()
                else:
                    self._sig_save()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            if self._metrics_proc and self._metrics_proc.poll() is None:
                self._metrics_proc.terminate()           # a batch can outlive the window otherwise
        finally:
            self.root.destroy()

    # ---------- local signal API (serve live positions of a formula / portfolio) ----------
    # Several services run side by side: each takes the next free port from SIGNAL_PORT upward and
    # gets a row in the SIGNAL API card on the main screen (URL + log path + "free the port").
    # A service is a detached process: it survives the GUI, so the registry is persisted to
    # signals.json and re-adopted (verified over /health) on the next start.
    def _build_signals_card(self, right):
        card = self._card(right)
        self.sig_card = card
        card.grid(row=1, column=0, sticky='ew', pady=(16, 0))
        pad = self._pad(card)
        hs = self._box(pad)
        hs.pack(fill='x')
        self.lbl_sig_head = self._head(hs, 'SIGNAL API — running services')
        self.lbl_sig_head.pack(side='left')
        self.btn_sig_all = self._btn(hs, '✕ Free all ports', self._stop_all_signals_ui,
                                     kind='danger', height=26, width=118)
        self.btn_sig_all.pack(side='right')
        self._tip(self.btn_sig_all, 'Stop every running signal API and release its port.')
        self._sig_rows = self._box(pad)
        self._sig_rows.pack(fill='x', pady=(8, 0))
        self._wire_card_drag('signals', pad, hs)
        card.grid_remove()                               # shown only while something is being served

    @staticmethod
    def _pid_alive(pid):
        """Is this PID still running? (os.kill(pid, 0) is an existence check on POSIX — but on
        Windows os.kill TERMINATES on any signal, so ask the kernel there instead.)"""
        if not pid:
            return False
        try:
            if os.name == 'nt':
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFO
                if not h:
                    return False
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(h)
                return bool(ok) and code.value == 259                              # STILL_ACTIVE
            os.kill(int(pid), 0)
            return True
        except Exception:                                # noqa: BLE001
            return False

    @staticmethod
    def _kill_pid(pid):
        try:
            if os.name == 'nt':                          # see _pid_alive: no signals on Windows
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, creationflags=0x08000000)      # CREATE_NO_WINDOW
            else:
                os.kill(int(pid), signal.SIGTERM)
        except Exception:                                # noqa: BLE001
            pass

    @staticmethod
    def _pid_on_port(port):
        """Best effort: who listens on `port`. Last resort for freeing a port held by a service we
        have neither a process handle nor a PID for (e.g. started by an older build)."""
        try:
            if os.name == 'nt':
                out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                                     text=True, creationflags=0x08000000).stdout
                for ln in out.splitlines():
                    f = ln.split()
                    if len(f) >= 5 and f[3].upper() == 'LISTENING' and f[1].endswith(f':{port}'):
                        return int(f[-1])
            else:
                out = subprocess.run(['lsof', '-ti', f'tcp:{port}', '-sTCP:LISTEN'],
                                     capture_output=True, text=True).stdout.split()
                if out:
                    return int(out[0])
        except Exception:                                # noqa: BLE001
            pass
        return None

    def _sig_save(self):
        """Persist the registry so a restarted (or crashed) GUI can find these services again."""
        keys = ('port', 'pid', 'label', 'log', 'n_formulas', 'n_tickers', 'started')
        try:
            json.dump([{k: s.get(k) for k in keys} for s in self._sigs],
                      open(SIGNALS_JSON, 'w', encoding='utf-8'), indent=1)
        except Exception:                                # noqa: BLE001
            pass

    def _sig_probe(self, port, timeout=0.6):
        """/health of `port` — the dict if an AlphaNode signal API answers there, else None."""
        try:
            import urllib.request
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=timeout) as r:
                h = json.load(r)
            return h if isinstance(h, dict) and 'name' in h and 'computing' in h else None
        except Exception:                                # noqa: BLE001
            return None

    def _sig_restore(self):
        """Background: find signal APIs that outlived the GUI. Scans the port range rather than
        trusting signals.json alone — that way a service survives even a lost registry; the file
        only restores the nice-to-haves (label, log path). Hands the result to the main thread."""
        meta = {}
        try:
            for e in json.load(open(SIGNALS_JSON, encoding='utf-8')):
                meta[int(e.get('port') or 0)] = e
        except Exception:                                # noqa: BLE001
            pass
        found = []
        for p in range(SIGNAL_PORT, SIGNAL_PORT + 10):
            h = self._sig_probe(p)
            if not h:
                continue
            m = meta.get(p, {})
            log = m.get('log')
            if not log or not os.path.exists(log):
                cand = os.path.join(STATE_DIR, f'signal_{p}.log')
                log = cand if os.path.exists(cand) else None
            # a service from an older build has no pid in /health — ask the OS who holds the port
            pid = h.get('pid') or m.get('pid') or self._pid_on_port(p)
            found.append({'port': p, 'pid': pid, 'proc': None, 'fh': None,
                          'label': h.get('name') or m.get('label') or f'signal_{p}', 'log': log,
                          'n_formulas': int(h.get('n_formulas') or m.get('n_formulas') or 0),
                          'n_tickers': int(h.get('n_tickers') or m.get('n_tickers') or 0),
                          'started': m.get('started'), 'adopted': True})
        self._sig_pending = found or None                 # no Tk from a worker — _sig_tick picks it up

    def _sig_adopt(self, found):
        """Main thread: add the re-found services to the registry (the tick draws them)."""
        have = {s['port'] for s in self._sigs}
        for e in found:
            if e['port'] in have:
                continue
            self._sigs.append(e)
            self._sig_health[e['port']] = 'reconnecting…'
        self._sig_save()

    def _free_signal_port(self, tries=50):
        """First free TCP port at/after SIGNAL_PORT that we aren't already using."""
        import socket
        busy = {s['port'] for s in self._sigs}
        for p in range(SIGNAL_PORT, SIGNAL_PORT + tries):
            if p in busy:
                continue
            with socket.socket() as sk:
                # same option the service's HTTP server binds with (allow_reuse_address) — without it
                # a port we just freed reads as busy while old connections linger in TIME_WAIT
                sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sk.bind(('127.0.0.1', p))
                    return p
                except OSError:                           # taken by someone else — try the next
                    continue
        return None

    def _serve_signal(self, formulas, label):
        if self._tf_gate('The live signal API'):
            return
        formulas = [f for f in (formulas or []) if f and f.strip()]
        if not formulas:
            return
        c = self.cfg
        if c.get('universe_all', True):
            try:
                tickers = list(pickle.load(open(apppaths.data_path(), 'rb'))[0])
            except Exception as e:                        # noqa: BLE001
                messagebox.showerror('Signal API', f'Cannot read the loaded data: {e}', parent=self.root)
                return
        else:
            tickers = [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()]
        if not tickers:
            messagebox.showwarning('Signal API', 'The pairs universe is empty.', parent=self.root)
            return
        if any(s['label'] == label for s in self._sigs):   # already serving this one — just show it
            self._render_signal_rows()
            return
        port = self._free_signal_port()
        if port is None:
            messagebox.showerror('Signal API', 'No free port available.', parent=self.root)
            return
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini(),
                   ALPHANODE_SIGNAL_FORMULAS=json.dumps(formulas), ALPHANODE_SIGNAL_NAME=label,
                   ALPHANODE_SIGNAL_TICKERS=','.join(tickers),
                   ALPHANODE_SIGNAL_PORT=str(port), ALPHANODE_SIGNAL_REFRESH='900')
        log_path = os.path.join(STATE_DIR, f'signal_{port}.log')
        try:
            fh = open(log_path, 'w', buffering=1)
            proc = subprocess.Popen(                       # each service logs to its own file
                _child_cmd('signal'), env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=fh, stderr=subprocess.STDOUT)
        except Exception as e:                            # noqa: BLE001
            messagebox.showerror('Signal API', f'Could not start the signal service: {e}', parent=self.root)
            return
        self._sigs.append({'port': port, 'proc': proc, 'pid': proc.pid, 'fh': fh, 'log': log_path,
                           'label': label, 'n_formulas': len(formulas), 'n_tickers': len(tickers),
                           'started': time.strftime('%Y-%m-%d %H:%M')})
        self._sig_health[port] = 'starting — fetching live data, computing the first signal…'
        self._sig_save()
        self._render_signal_rows()

    def _pf_serve_signal(self):
        doc = self._pf_doc
        formulas = (doc or {}).get('formulas_full')
        if not formulas:
            messagebox.showinfo('Signal API', 'Build the portfolio first.', parent=self.root)
            return
        self._serve_signal(list(formulas), f'portfolio_top{doc.get("n", len(formulas))}')

    def _stop_signal(self, entry):
        """Stop ONE service, free its port, drop it from the registry. Works both for services we
        started (process handle) and for ones adopted from an earlier session (PID only)."""
        try:
            proc = entry.get('proc')
            if proc is not None and proc.poll() is None:
                proc.terminate()
            elif proc is None:                            # adopted: no handle, kill by PID
                pid = entry.get('pid') or self._pid_on_port(entry['port'])
                if pid:
                    self._kill_pid(pid)
        except Exception:                                 # noqa: BLE001
            pass
        try:
            if entry.get('fh'):
                entry['fh'].close()
        except Exception:                                 # noqa: BLE001
            pass
        self._sig_health.pop(entry['port'], None)
        if entry in self._sigs:
            self._sigs.remove(entry)
        self._sig_save()

    def _stop_all_signals(self):
        for entry in list(self._sigs):
            self._stop_signal(entry)

    def _stop_signal_ui(self, entry):
        self._stop_signal(entry)
        self._render_signal_rows()

    def _stop_all_signals_ui(self):
        if not self._sigs or messagebox.askyesno(
                'Signal API', f'Stop all {len(self._sigs)} service(s) and free their ports?',
                parent=self.root):
            self._stop_all_signals()
            self._render_signal_rows()

    def _sig_tick(self):
        """Every 3s: refresh the health of each service. Main thread — the HTTP call itself is
        handed to a worker (see _sig_poll_worker)."""
        pending, self._sig_pending = self._sig_pending, None
        if pending:
            self._sig_adopt(pending)
        for s in list(self._sigs):
            proc, pid = s.get('proc'), s.get('pid')
            if proc is not None:
                dead = proc.poll() is not None
            else:                                         # adopted: unknown PID -> let /health speak
                dead = bool(pid) and not self._pid_alive(pid)
            if dead:
                self._sig_health[s['port']] = '○ stopped (the process exited) — port is free'
            else:
                threading.Thread(target=self._sig_poll_worker, args=(s['port'],), daemon=True).start()
        ports = tuple(s['port'] for s in self._sigs)
        if ports != self._sig_shown:                      # the set changed -> rebuild the rows
            self._render_signal_rows()
        else:                                             # same set -> only refresh the status text
            for p, lbl in list(self._sig_status_lbl.items()):
                if lbl.winfo_exists():
                    lbl.configure(text=self._sig_health.get(p, 'starting…'))
        self.root.after(3000, self._sig_tick)

    def _render_signal_rows(self):
        """Rebuild the service list on the main screen. Main thread only."""
        holder = getattr(self, '_sig_rows', None)
        if holder is None or not holder.winfo_exists():
            return
        for w in holder.winfo_children():
            w.destroy()
        self._sig_status_lbl = {}
        self._sig_shown = tuple(s['port'] for s in self._sigs)
        if not self._sigs:
            self.sig_card.grid_remove()                   # nothing served -> the card gets out of the way
            return
        self.sig_card.grid()
        n = len(self._sigs)
        self.lbl_sig_head.configure(text=f'SIGNAL API — {n} running service{"s" if n > 1 else ""}  ·  '
                                         f'live JSON on localhost  ·  refresh 15 min')
        for s in self._sigs:
            url = f'http://127.0.0.1:{s["port"]}/signal'
            row = self._box(holder)
            row.pack(fill='x', pady=(0, 8))
            btns = self._box(row)
            btns.pack(side='right', anchor='n')
            self._btn(btns, 'Copy URL', lambda u=url: (self.root.clipboard_clear(),
                                                       self.root.clipboard_append(u)),
                      height=26, width=76).pack(side='left')
            log_btn = self._btn(btns, 'Log', lambda p=s.get('log'): self._open_folder(p),
                                height=26, width=44)
            log_btn.configure(state=('normal' if s.get('log') else 'disabled'))
            log_btn.pack(side='left', padx=(4, 0))
            self._btn(btns, '✕ Free port', lambda e=s: self._stop_signal_ui(e), kind='danger',
                      height=26, width=92).pack(side='left', padx=(4, 0))
            info = self._box(row)
            info.pack(side='left', fill='x', expand=True)
            head = (f'● {s["port"]}  ·  {s["label"]}  ·  {s["n_formulas"]} alpha(s)  ·  '
                    f'{s["n_tickers"]} pairs')
            if s.get('started'):
                head += f'  ·  since {s["started"]}'
            if s.get('adopted'):
                head += '  ·  from an earlier session'
            self._lbl(info, text=head, text_color=TXT, font=(self.UI, 13, 'bold'),
                         wraplength=620, justify='left', anchor='w').pack(anchor='w')
            self._lbl(info, text=url + (f'   ·   {s["log"]}' if s.get('log') else ''),
                         text_color=FAINT, font=(self.MONO, 11), wraplength=620,
                         justify='left', anchor='w').pack(anchor='w')
            lbl = self._lbl(info, text=self._sig_health.get(s['port'], 'starting…'), text_color=MUT,
                               font=(self.UI, 11), wraplength=620, justify='left', anchor='w')
            lbl.pack(anchor='w')
            self._sig_status_lbl[s['port']] = lbl

    def _sig_poll_worker(self, port):
        """Background: fetch /health for ONE service and stash the text. NO Tk here — Tk is not
        thread-safe; the main-thread tick renders from _sig_health."""
        try:
            import urllib.request
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as r:
                h = json.load(r)
            if h.get('ok'):
                age = h.get('age_secs')
                txt = (f'● serving · updated {h.get("updated_at", "")} ({age:.0f}s ago)'
                       if age is not None else '● serving')
            elif h.get('error'):
                txt = f'⚠ {h["error"]}'
            else:
                txt = 'computing the first signal…'
        except Exception:                                 # noqa: BLE001
            txt = 'starting…'
        self._sig_health[port] = txt

    # ---------- status polling ----------
    def _log_placeholder(self):
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        self.logbox.insert('end', 'live log — round starts, new champions and '
                                  'round summaries will appear here once the node runs.', 'i')
        self.logbox.configure(state='disabled')

    def _render_events(self, evs):
        """Rebuild the LIVE LOG feed (newest at the bottom, auto-scrolled, colored by kind)."""
        at_end = self.logbox.yview()[1] > 0.999          # don't yank the view if the user scrolled up
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        for e in evs[-80:]:
            self.logbox.insert('end', f"{e.get('ts', '')}  ", 'ts')
            self.logbox.insert('end', f"{e.get('t', '')}\n", e.get('k', 'i'))
        if at_end:
            self.logbox.see('end')
        self.logbox.configure(state='disabled')

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
            self._state_pill(f'● {"running" if state == "running" else state}', color)
            vol = st.get('target_vol')
            vol_s = f' · vol {vol:g}' if isinstance(vol, (int, float)) else ''
            self.lbl_res.configure(text=f'{st.get("cpu_percent","?")}% · {st.get("n_jobs","?")}/{st.get("cores","?")} cores '
                                     f'· {st.get("universe","")}{vol_s}')
            self.s_rounds.configure(text=str(st.get('rounds', 0)))
            self.s_trials.configure(text=f'{st.get("trials_total", 0):,}')
            self.s_found.configure(text=str(st.get('found', len(st.get('best', [])))))
            self.lbl_cur.configure(text=(st.get('current', '') + '   ' + st.get('gen', ''))[:120])
            evs = st.get('events') or []
            if evs and evs != self._events_last:
                self._events_last = evs
                self._render_events(evs)
            self._refresh_leaderboard(st.get('best', []))
            self._history = st.get('history', [])
            self._draw_chart()
        if not running and (not st or st.get('state') != 'running'):
            if not (self.proc and self.proc.poll() is None):
                self._state_pill('● stopped', MUT)
        try:
            while True:
                self.logq.get_nowait()
        except queue.Empty:
            pass
        self.root.after(1500, self._poll)

    def _on_chart_drag(self, e):
        h = e.y_root - self.chart.winfo_rooty()
        h = max(int(90 * self.SCALE), min(int(560 * self.SCALE), h))
        self.chart.configure(height=h)                   # <Configure> redraws the plot itself
        self.cfg['chart_h'] = round(h / self.SCALE)

    def _chart_reset(self, _e=None):
        self.cfg['chart_h'] = 0
        self.chart.configure(height=int(170 * self.SCALE))
        self._save()

    def _draw_chart(self):
        cv = getattr(self, 'chart', None)
        if cv is None:
            return
        hist = getattr(self, '_history', []) or []
        cv.configure(bg=CARD)
        cv.delete('all')
        w = max(cv.winfo_width(), 300)
        h = int(cv['height'])

        def _v(p):                                       # optimized fitness (old log — fallback to best_test)
            return p.get('best_base', p.get('best_test'))
        pts = [(p['round'], _v(p)) for p in hist if _v(p) is not None]
        last_test = next((p.get('best_test') for p in reversed(hist) if p.get('best_test') is not None), None)
        if len(pts) < 2:
            cv.create_text(w / 2, h / 2, text='chart will appear after a couple of rounds',
                           fill=MUT, font=self._font(self.UI, 12))
            return
        ys = [v for _, v in pts]
        lo, hi = min(ys), max(ys)
        if hi - lo < 0.3:                                  # keep the line from flattening out
            m = (hi + lo) / 2
            lo, hi = m - 0.15, m + 0.15
        S = self.SCALE
        padL, padR, padT, padB = (int(v * S) for v in (56, 18, 32, 24))
        n = len(pts)
        plotw, ploth = w - padL - padR, h - padT - padB
        base_y = padT + ploth

        def X(i):
            return padL + plotw * (i / (n - 1))

        def Y(v):
            return padT + ploth * (1 - (v - lo) / (hi - lo))

        k = max(2, min(4, int(ploth / (26 * S)) + 1))      # each Y label needs ~26px of air
        for frac in (i / (k - 1) for i in range(k)):       # dashed grid + Y labels
            val = lo + (hi - lo) * frac
            y = Y(val)
            cv.create_line(padL, y, w - padR, y, fill=GRID, dash=(2, 5))
            cv.create_text(padL - 9 * S, y, text=f'{val:+.2f}', anchor='e', fill=FAINT,
                           font=self._font(self.UI, 10))

        line = []
        for i, (_, v) in enumerate(pts):
            line += [X(i), Y(v)]
        # fake gradient (canvas has no alpha): a faint full fill, then a stronger ribbon that
        # hugs the line — reads as a glow fading toward the baseline
        cv.create_polygon(padL, base_y, *line, X(n - 1), base_y,
                          fill=_mix(ACC_SOFT, CARD, 0.45), outline='')
        ribbon = list(line)
        for i in range(n - 1, -1, -1):
            ribbon += [X(i), min(Y(pts[i][1]) + 14 * S, base_y)]
        cv.create_polygon(*ribbon, fill=ACC_SOFT, outline='')
        cv.create_line(*line, fill=ACC, width=2.5, capstyle='round', joinstyle='round')
        lx, ly = X(n - 1), Y(ys[-1])
        cv.create_oval(lx - 7 * S, ly - 7 * S, lx + 7 * S, ly + 7 * S,
                       fill=_mix(ACC, CARD, 0.72), outline='')
        cv.create_oval(lx - 4 * S, ly - 4 * S, lx + 4 * S, ly + 4 * S,
                       fill=ACC, outline=CARD, width=2)
        txt = f'fitness {ys[-1]:+.2f}'
        f12 = self._font(self.UI, 12, 'bold')
        bx1, by0 = w - padR, 3 * S
        bx0, by1 = bx1 - f12.measure(txt) - int(20 * S), by0 + int(23 * S)
        _rrect(cv, bx0, by0, bx1, by1, int(11 * S), fill=ACC_SOFT, outline='')
        cv.create_text((bx0 + bx1) / 2, (by0 + by1) / 2 + 1, text=txt, fill=ACC_HI, font=f12)
        cv.create_text(padL, h - 6 * S, text=f'round {pts[0][0]}', anchor='w', fill=FAINT,
                       font=self._font(self.UI, 10))
        cv.create_text(w - padR, h - 6 * S, text=f'round {pts[-1][0]}', anchor='e', fill=FAINT,
                       font=self._font(self.UI, 10))
        if last_test is not None:                        # honest held-out — bottom center, no collisions
            cv.create_text((padL + w - padR) / 2, h - 6 * S, text=f'champion TEST {last_test:+.2f} · held-out',
                           anchor='s', fill=FAINT, font=self._font(self.UI, 10))

    def _progress_interactive(self, _e=None):
        """The dashboard progress chart as a LIVE matplotlib window (wheel zoom / pan / reset),
        with the extras the compact card has no room for: the champion's held-out TEST curve
        over time and the library growth on a twin axis."""
        hist = getattr(self, '_history', []) or []

        def _v(p):
            return p.get('best_base', p.get('best_test'))
        base = [(p['round'], _v(p)) for p in hist if _v(p) is not None]
        if len(base) < 2:
            messagebox.showinfo('Progress', 'The chart appears after a couple of rounds.',
                                parent=self.root)
            return
        test = [(p['round'], p['best_test']) for p in hist if p.get('best_test') is not None]
        found = [(p['round'], p['found']) for p in hist if p.get('found') is not None]
        import matplotlib
        from matplotlib.figure import Figure
        win = self._dialog('Progress — fitness by round  (wheel: zoom · double-click: reset)',
                           f'{int(1040 / self.SCALE)}x{int(580 / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=14, pady=12)
        with self._plot_lock, matplotlib.rc_context(self._mpl_rc()):
            fig = Figure(figsize=(9.9, 4.5), dpi=100, facecolor=CARD)
            ax = fig.add_subplot(111)
            ax.plot([r for r, _ in base], [v for _, v in base], lw=2.0, color=ACC,
                    label='fitness min(train,val) — optimized')
            if test:
                ax.plot([r for r, _ in test], [v for _, v in test], lw=1.4, color='#f9a825',
                        label='champion TEST — held-out, never optimized')
            ax.set_xlabel('round', fontsize=9)
            ax.set_ylabel('Sharpe', fontsize=9)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)
            hnd, lab = ax.get_legend_handles_labels()
            if len(found) >= 2:
                ax2 = ax.twinx()
                ax2.plot([r for r, _ in found], [v for _, v in found], lw=1.0, ls='--',
                         color=MUT, label='library size')
                ax2.set_ylabel('champions in the library', fontsize=8, color=MUT)
                ax2.tick_params(labelsize=8, colors=MUT)
                ax2.grid(False)
                h2, l2 = ax2.get_legend_handles_labels()
                hnd, lab = hnd + h2, lab + l2
            ax.legend(hnd, lab, loc='upper left', fontsize=8)
            fig.tight_layout()
        self._embed_fig(body, fig)

    def _families(self, rows, target):
        """The best alpha per family: walk `rows` (already best-first) and keep one representative
        per formula shape (SequenceMatcher < 0.80), until `target` distinct families. The scan is
        capped so the O(N²) similarity can't freeze the GUI on a huge library."""
        kept = []
        for c in rows[:500]:
            f = c.get('formula', '')
            if all(difflib.SequenceMatcher(None, f, k.get('formula', '')).ratio() < 0.80 for k in kept):
                kept.append(c)
            if len(kept) >= target:
                break
        return kept

    _LB_TESTKEY = staticmethod(
        lambda c: (c.get('test') if isinstance(c.get('test'), dict) else {}).get('sharpe'))

    _SORTABLE = ('fit', 'test', 'dd', 'cagr', 'srt', 'ls', 'act', 'win', 'formula')

    @staticmethod
    def _finite(v):
        """A finite number or None — NaN must never reach the sort (it poisons comparisons)."""
        return v if (isinstance(v, (int, float)) and v == v
                     and v not in (float('inf'), float('-inf'))) else None

    def _sort_key(self, c, col):
        if col == 'fit':
            return c.get('base')
        if col == 'test':
            return self._LB_TESTKEY(c)
        if col == 'formula':
            return c.get('formula', '')
        m = self._metrics_cache.get(c.get('formula', ''))    # the rest — from the metrics cache
        m = m if isinstance(m, dict) else {}
        if col in ('dd', 'cagr'):                            # library row first, worker fallback
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            return self._finite(t.get(col)) if self._finite(t.get(col)) is not None \
                else self._finite(m.get(col))
        if col == 'srt':
            return self._finite(m.get('sortino'))
        if col == 'ls':
            return m.get('long', 0) + m.get('short', 0)
        if col == 'act':
            return m.get('act')
        if col == 'win':
            return m.get('win')
        return None

    def _sorted(self, rows):
        col, desc = self._sort_col, self._sort_desc
        if col == 'formula':
            return sorted(rows, key=lambda c: c.get('formula', ''), reverse=desc)

        def k(c):
            v = self._sort_key(c, col)
            return float('-inf') if v is None else v      # missing metrics sort to the bottom
        return sorted(rows, key=k, reverse=desc)

    def _update_headings(self):
        for c, txt in self._HEAD.items():
            arrow = ('  ▼' if self._sort_desc else '  ▲') if c == self._sort_col else ''
            self.tree.heading(c, text=txt.upper() + arrow)

    def _lb_head_text_for(self, select):
        scope = 'every alpha' if self._lb_mode == 'all' else 'best alpha per family'
        src = ('by TEST OOS — held-out, cherry-picked ⚠' if select == 'test'
               else 'by fitness min(train,val)')
        return (f'LEADERBOARD — {scope}, {src}  ·  '
                'click a column to sort  ·  double-click: equity  ·  right-click / Ctrl+C: copy')

    def _sort_by(self, col):
        """Click a column header. 'fitness' and 'TEST OOS' also RE-SELECT the population from the
        WHOLE library by that metric (top-by-fitness vs top-by-held-out-TEST); ls/act/win/formula
        just reorder the current population. Repeat click on the same column toggles direction."""
        if col not in self._SORTABLE:
            return
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        self._update_headings()
        self._treesig = None                             # force a redraw in the new order
        select = col if col in ('fit', 'test') else self._lb_select
        if select != self._lb_select:                    # population key changed -> re-query the library
            self._lb_select = select
            self._lb_head_text = self._lb_head_text_for(select)
            self.lbl_lb_head.configure(text=self._lb_head_text)
            self._start_lb_compute(force=True)
        else:
            self._render_lb(self._lb_rows())

    def _start_lb_compute(self, force=False):
        lib = self._lib_file()
        try:
            mt = os.path.getmtime(lib)
        except OSError:
            return
        cache = self._lib_cache
        if cache['computing']:
            return
        if not force and mt == cache['mtime']:
            return
        cache['computing'] = True
        cache['ts'] = time.time()
        threading.Thread(target=self._compute_lb, args=(lib, mt), daemon=True).start()

    def _lb_rows(self):
        """The set the table should show for the current toggle: every alpha, or best-per-family."""
        c = self._lib_cache
        return c['families'] if self._lb_mode == 'families' else c['all']

    def _render_lb(self, best):
        self._fill_tree(best)

    def _refresh_leaderboard(self, status_best):
        """Into the table — EVERY alpha in the library (or best-per-family when the toggle is on).
        Read + sorted in the background and cached by mtime/population key: the whole library can be
        thousands of rows, and the family dedup is O(N²), so neither runs on the Tk thread."""
        cache = self._lib_cache
        now = time.time()
        stale_select = cache.get('select') != self._lb_select    # a header click changed the population key
        if not cache['computing'] and (stale_select or now - cache['ts'] > 6):
            self._start_lb_compute(force=stale_select)   # restart on file change / mode switch / period
        if cache['dirty']:
            cache['dirty'] = False
            self._treesig = None                         # force a redraw after recompute
            self._render_lb(self._lb_rows())
        elif not cache.get('computed'):
            self._fill_tree(status_best)                 # until computed — the top from the node (as before)

    def _compute_lb(self, path, mtime):
        """Load the WHOLE library, ranked by the active population key: 'fit' = fitness min(train,val)
        (honest), or 'test' = held-out TEST OOS (cherry-pick, chosen by clicking the TEST OOS header).
        Produces both sets — every alpha ('all') and best-per-family ('families') — so the toggle is
        instant. Row order in the table is set client-side in _sorted."""
        select = self._lb_select
        rows = []
        try:
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:             # a half-written last line while the node appends
                    pass
        except OSError:
            self._lib_cache['computing'] = False
            return
        if select == 'test':
            rows = [c for c in rows if self._LB_TESTKEY(c) is not None]
            rows.sort(key=self._LB_TESTKEY, reverse=True)        # top by held-out TEST OOS (cherry-pick)
        else:
            rows = [c for c in rows if c.get('base') is not None]
            rows.sort(key=lambda c: c.get('base'), reverse=True)  # top by honest fitness min(train,val)
        families = self._families(rows, self._lb_target)
        self._lib_cache.update(all=rows, families=families, mtime=mtime, select=select,
                               computing=False, dirty=True, computed=True)

    def _fill_tree(self, best):
        best = self._sorted(best)                        # order by the clicked column (no dedup — see the toggle)
        sig = (self._lb_mode, self._sort_col, self._sort_desc, len(best),
               best[0]['formula'] if best else '')
        if getattr(self, '_treesig', None) == sig:
            return
        self._treesig = sig
        self._shown = best                               # for clicks: row -> champion
        top = self.tree.yview()[0] if self.tree.get_children() else 0.0   # keep the viewport across redraws
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._row_items = {}
        need = 0
        for i, c in enumerate(best):
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            ts = t.get('sharpe')                         # honest held-out OOS — colored by it
            base = c.get('base')
            sign = 'pos' if (ts is not None and ts >= 0) else ('neg' if ts is not None else 'even')
            stripe = 'odd' if i % 2 else 'even'
            formula = c.get('formula', '')
            f = formula                                  # full length — the column fits the widest row
            need = max(need, self._tree_font.measure(f))
            m = self._metrics_cache.get(formula)
            ls, act, win, srt = self._fmt_metrics(m)
            dd = self._lb_test_ratio(c, m, 'dd', pct=True)
            cagr = self._lb_test_ratio(c, m, 'cagr', pct=True)
            aid = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
            item = self.tree.insert('', 'end', values=(
                i + 1, f'{base:+.2f}' if base is not None else '—',
                f'{ts:+.2f}' if ts is not None else '—', dd, cagr, srt, ls, act, win, aid, f),
                tags=(sign, stripe))
            self._row_items[formula] = item
        self._lb_need_px = need + int(28 * self.SCALE)   # + cell padding / a breath of air
        self._fit_formula_col()
        if top:
            self.tree.yview_moveto(top)                  # a background recompute must not yank you to the top
        self.root.after_idle(self._pump_metrics)         # trade stats for what is actually on screen

    def _toggle_lb_mode(self):
        """All ↔ families. Both sets are already cached, so this is instant — just re-render."""
        self._lb_mode = 'families' if self.v_lbfam.get() else 'all'
        self.cfg['lb_mode'] = self._lb_mode
        self._save()
        self._lb_head_text = self._lb_head_text_for(self._lb_select)   # 'every alpha' vs 'best per family'
        self.lbl_lb_head.configure(text=self._lb_head_text)
        self._treesig = None                             # force a redraw with the other set
        self._render_lb(self._lb_rows())

    def _fit_formula_col(self):
        """Size the formula column to the widest row (full formulas, no ellipsis). Wider than
        the visible area -> the horizontal scrollbar takes over; narrower -> the column still
        stretches to fill the card. Re-run on every render and on tree resize."""
        try:
            fixed = sum(int(self.tree.column(c, 'width'))
                        for c in ('rank', 'fit', 'test', 'dd', 'cagr', 'srt',
                                  'ls', 'act', 'win', 'id'))
        except tk.TclError:                              # theme rebuild mid-flight
            return
        avail = self.tree.winfo_width() - fixed - int(4 * self.SCALE)
        w = max(self._lb_need_px, avail, int(260 * self.SCALE))
        self.tree.column('formula', width=w, stretch=False)

    def _on_tree_scroll(self, first, last):
        """Tk's yscrollcommand: keep the scrollbar in sync AND (debounced) load metrics for the rows
        that just scrolled into view. Trade stats are computed per viewport, not for the whole
        library — that is what keeps scrolling a 600-row table smooth."""
        self._vsb.set(first, last)
        if self._vis_after:
            try:
                self.root.after_cancel(self._vis_after)
            except (ValueError, tk.TclError):
                pass
        self._vis_after = self.root.after(140, lambda: self._start_metrics(self._visible_champs()))

    def _visible_champs(self):
        """The champions in (or just around) the current viewport — the batch whose trade stats we
        compute next. Bounded, so a not-yet-laid-out yview of (0,1) can't request the whole library."""
        n = len(self._shown)
        if not n:
            return []
        try:
            first, last = self.tree.yview()
        except tk.TclError:
            first, last = 0.0, 1.0
        lo = max(0, int(first * n) - 3)
        hi = min(n, int(round(last * n)) + 3)
        hi = min(hi, lo + 60)
        return self._shown[lo:hi]

    def _pump_metrics(self):
        """After a redraw: compute the visible rows' stats. Sorting BY a stat column is the one case
        that needs every value at once (else the order is wrong), so there we compute the full set."""
        if self._sort_col in ('ls', 'act', 'win', 'srt', 'dd', 'cagr'):
            self._start_metrics(self._shown)
        else:
            self._start_metrics(self._visible_champs())

    @staticmethod
    def _fmt_ratio(v, pct=False):
        """'—' unless v is a finite number; percents rounded to whole, ratios to 2 decimals."""
        if not isinstance(v, (int, float)) or v != v or v in (float('inf'), float('-inf')):
            return '—'
        return f'{v * 100:+.0f}%' if pct else f'{v:+.2f}'

    def _lb_test_ratio(self, c, m, key, pct):
        """dd/cagr cell: the library row's own TEST metrics first (stored at mining time),
        the metrics worker's number as a fallback for old rows; '…' while it may still come."""
        t = c.get('test') if isinstance(c.get('test'), dict) else {}
        v = t.get(key)
        if not isinstance(v, (int, float)) and isinstance(m, dict):
            v = m.get(key)
        if not isinstance(v, (int, float)) and m is None:
            return '…'
        return self._fmt_ratio(v, pct=pct)

    @staticmethod
    def _fmt_metrics(m):
        """('L/S', 'tr/yr·a', 'win%', 'sortino') strings from the cache: None=still computing,
        'err'=failed."""
        if m is None:
            return '…', '…', '…', '…'
        if m == 'err':
            return '—', '—', '—', '—'
        a = m.get('act', 0.0)
        astr = f'{a:.1f}' if a < 10 else f'{a:.0f}'
        return (f'{m["long"]:.0f}/{m["short"]:.0f}', astr, f'{m["win"] * 100:.0f}%',
                App._fmt_ratio(m.get('sortino')))

    def _start_metrics(self, champs):
        """Background computation of long/short/win (on TEST) for the shown alphas; cached by formula."""
        todo = [c for c in champs if c.get('formula') and c['formula'] not in self._metrics_cache]
        if not todo:
            return
        self._metrics_seq += 1
        seq = self._metrics_seq
        threading.Thread(target=self._compute_metrics, args=(todo, seq), daemon=True).start()

    def _compute_metrics(self, champs, seq):
        with self._metrics_lock:
            todo = [c['formula'] for c in champs
                    if not isinstance(self._metrics_cache.get(c.get('formula', '')), dict)]
            try:
                if todo and seq == self._metrics_seq:
                    got = self._run_metrics_worker(todo)
                    for f in todo:
                        self._metrics_cache[f] = got.get(f, 'err')
            except Exception:                            # noqa: BLE001  (no data/config — quietly)
                for c in champs:
                    self._metrics_cache.setdefault(c.get('formula', ''), 'err')
            finally:
                try:
                    self.root.after(0, lambda s=seq: self._apply_metrics(s))
                except (RuntimeError, tk.TclError):      # window already closed / no loop
                    pass

    def _run_metrics_worker(self, formulas):
        """Hand the batch to a child process and wait on its pipe.

        This is the whole point of the worker: the computation is GIL-bound Python, and running it
        in-process (thread or not) starves the Tk main loop — status polls went from ~14ms to
        ~800ms and the window stalled while the table filled in. Waiting on a pipe releases the GIL.
        Returns {formula: stats|'err'}; raises if the worker could not produce a batch at all."""
        c = self.cfg
        payload = {
            'formulas': list(formulas),
            'instruments': (None if c.get('universe_all', True) else
                            [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()]),
            'vol': float(c.get('target_vol', 0.25)), 'exec': float(c.get('exec_cost', 0.001)),
            'train_start': c.get('train_start'), 'test_start': c.get('test_start'),
            'test_end': c.get('test_end'),
        }
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini())
        proc = subprocess.Popen(_child_cmd('metrics'), env=env,
                                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self._metrics_proc = proc
        out, _ = proc.communicate(json.dumps(payload), timeout=600)
        doc = json.loads(out.strip().splitlines()[-1])   # the engine may print warnings first
        if not doc.get('ok'):
            raise RuntimeError(doc.get('error', 'metrics worker failed'))
        return doc.get('metrics') or {}

    def _apply_metrics(self, seq):
        """Set the computed metric cells into the already shown rows (main thread)."""
        if seq != self._metrics_seq:
            return
        for formula, item in list(self._row_items.items()):
            if not self.tree.exists(item):
                continue
            m = self._metrics_cache.get(formula)
            ls, act, win, srt = self._fmt_metrics(m)
            self.tree.set(item, 'ls', ls)
            self.tree.set(item, 'act', act)
            self.tree.set(item, 'win', win)
            self.tree.set(item, 'srt', srt)
            if isinstance(m, dict):                      # dd/cagr fallback for legacy library rows
                for col in ('dd', 'cagr'):
                    if self.tree.set(item, col) in ('…', '—'):
                        self.tree.set(item, col, self._fmt_ratio(m.get(col), pct=True))
        if self._sort_col in ('ls', 'act', 'win', 'srt', 'dd', 'cagr'):   # arrived -> reorder
            self._treesig = None
            self._render_lb(self._lb_rows() or self._shown)

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

    def _flash_lb(self, msg, ms=1300):
        """Say something in the leaderboard heading, then put the heading back."""
        self.lbl_lb_head.configure(text=msg)
        self.root.after(ms, lambda: self.lbl_lb_head.configure(text=self._lb_head_text))

    def _to_clipboard(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()                               # so the buffer is handed to the X server right away
        self._flash_lb(msg)

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

    # ---------- LEADERBOARD: download the table ----------
    @staticmethod
    def _seg(champ, seg, key):
        """One TRAIN/VAL/TEST number, or '' — an alpha may be missing a segment entirely."""
        v = (champ.get(seg) if isinstance(champ.get(seg), dict) else {}).get(key)
        return round(v, 4) if isinstance(v, (int, float)) else ''

    def _save_csv(self, path, header, rows, what):
        """csv.writer, not a join: every formula is full of commas (div(pmin(a,b),c)) and would
        otherwise split into columns. newline='' is what the csv module requires of its file."""
        try:
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(rows)
        except OSError as e:
            messagebox.showerror('Export', f'Could not write {path}:\n{e}', parent=self.root)
            return
        self._flash_lb(f'✓ {len(rows)} {what} saved to {os.path.basename(path)}', ms=2600)

    def _export_visible(self):
        """The table as shown: same rows, same order, same columns. The trade stats come from the
        metrics cache, which only ever holds the rows on screen — hence the two separate exports."""
        rows = list(self._shown)
        if not rows:
            messagebox.showinfo('Export table', 'The leaderboard is empty — nothing to export yet.',
                                parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Export leaderboard', defaultextension='.csv',
            initialfile=f'leaderboard_top{len(rows)}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        out = []
        for i, c in enumerate(rows):
            formula = c.get('formula', '')
            m = self._metrics_cache.get(formula)
            m = m if isinstance(m, dict) else {}          # None = still computing, 'err' = failed -> blanks
            base = c.get('base')
            out.append([i + 1,
                        round(base, 4) if isinstance(base, (int, float)) else '',
                        self._seg(c, 'train', 'sharpe'), self._seg(c, 'val', 'sharpe'),
                        self._seg(c, 'test', 'sharpe'), self._seg(c, 'test', 'dd'),
                        self._seg(c, 'test', 'cagr'),
                        round(m['sortino'], 3) if isinstance(m.get('sortino'), (int, float)) else '',
                        m.get('long', ''), m.get('short', ''),
                        round(m['act'], 2) if 'act' in m else '',
                        round(m['win'] * 100, 1) if 'win' in m else '',
                        'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6], formula])
        self._save_csv(path, ('rank', 'fitness', 'train_sharpe', 'val_sharpe', 'test_sharpe',
                              'test_dd', 'test_cagr', 'test_sortino', 'long', 'short', 'tr_yr_a',
                              'win_pct', 'id', 'formula'), out, 'rows')

    def _export_library(self):
        """Everything the node has ever mined — no dedup, no TEST filter. The table on screen is a
        diverse SLICE of this; here you get all of it, ordered by honest fitness min(train,val)."""
        rows = []
        try:
            with open(self._lib_file(), encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:          # a half-written last line while the node appends
                        pass
        except OSError:
            rows = []
        if not rows:
            messagebox.showinfo('Export library', 'The library is empty — the node has not mined '
                                                  'anything yet.', parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Export full library', defaultextension='.csv',
            initialfile=f'library_{len(rows)}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        rows.sort(key=lambda c: c.get('base') if isinstance(c.get('base'), (int, float)) else -1e9,
                  reverse=True)
        header = ['formula', 'size', 'fitness', 'round', 'found_at', 'origin']
        for seg in ('train', 'val', 'test'):
            header += [f'{seg}_{k}' for k in ('sharpe', 'dd', 'cagr', 'n')]
        out = []
        for c in rows:
            base = c.get('base')
            r = [c.get('formula', ''), c.get('size', ''),
                 round(base, 4) if isinstance(base, (int, float)) else '',
                 c.get('round', ''), c.get('ts', ''), c.get('origin', 'ga')]
            for seg in ('train', 'val', 'test'):
                r += [self._seg(c, seg, k) for k in ('sharpe', 'dd', 'cagr', 'n')]
            out.append(r)
        self._save_csv(path, header, out, 'alphas')

    # ---------- PORTFOLIO: combine top-N (1d: real engine; intraday: fastsim, same combiner) ----------
    def _build_portfolio(self):
        if self._pf_proc and self._pf_proc.poll() is None:
            return                                       # already building
        n = self._gi(self.v_pfn, 6)
        sel = 'base' if self.v_pfsel.get() == 'fitness' else 'test'
        eng = 'real engine, ~1–2 min' if self._tf() == '1d' else f'{self._tf()} fastsim, ~seconds'
        self.btn_pf.configure(state='disabled')
        self.lbl_pf_m.configure(text='', fg=MUT)
        self.lbl_pf.configure(text=f'building portfolio from top-{n} by '
                                   f'{"TEST" if sel == "test" else "fitness min(train,val)"} '
                                   f'({eng})…')
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini(),
                   # combine on the SAME universe the search optimizes — not all of data.pickle
                   ALPHANODE_UNIVERSE=('all' if self.cfg.get('universe_all', True)
                                       else self.cfg.get('universe_list', '')),
                   # the GUI's date fields, node.py-style — the ini only has daily defaults
                   ALPHANODE_TRAIN_START=self.cfg.get('train_start', ''),
                   ALPHANODE_VAL_START=self.cfg.get('val_start', ''),
                   ALPHANODE_TEST_START=self.cfg.get('test_start', ''),
                   ALPHANODE_TEST_END=self.cfg.get('test_end', ''))
        try:
            self._pf_proc = subprocess.Popen(
                _child_cmd('portfolio') + ['--top', str(n), '--select', sel,
                                           '--out', PORTFOLIO_JSON], env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.configure(text=f'could not start portfolio build: {e}')
            self.btn_pf.configure(state='normal')
            return
        threading.Thread(target=self._pf_reader, args=(self._pf_proc,), daemon=True).start()

    def _pf_reader(self, proc):
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if line:
                self.root.after(0, lambda s=line: self.lbl_pf.configure(text=s))
        proc.wait()
        self.root.after(0, self._portfolio_done)

    def _portfolio_done(self):
        self.btn_pf.configure(state='normal')
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.configure(text=f'portfolio build did not finish: {e}')
            return
        self._render_portfolio(doc)

    def _render_portfolio(self, doc):
        if not doc.get('ok'):
            self.lbl_pf.configure(text='portfolio build failed: ' + str(doc.get('error', ''))[:120])
            return
        self._pf_doc = doc                               # remember for re-render on resize
        self.btn_pf_csv.configure(state=('normal' if doc.get('weights') else 'disabled'))
        self.btn_pf_paper.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        self.btn_pf_sig.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        self.btn_pf_pdf.configure(state=('normal' if doc.get('weights') else 'disabled'))
        self.btn_pf_track.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        m = doc.get('metrics') or {}
        b = doc.get('basket') or {}
        if doc.get('sel') == 'base':                     # selection never saw TEST
            note = 'TEST held out of selection — the numbers below are honest OOS'
            picked = 'by fitness min(train,val)'
        else:                                            # 'test' or legacy docs without 'sel'
            note = ('⚠ members picked by TEST — its numbers are optimistic (cherry-pick); '
                    'validate with Paper')
            picked = 'by TEST OOS'
        eng = 'the real engine' if doc.get('tf', '1d') == '1d' else f'fastsim on {doc["tf"]} bars'
        span = (f'TRAIN+VAL+TEST {doc["span"]}' if doc.get('span')
                else f'TEST {doc.get("test", "")}')     # docs built before the full-span change
        self.lbl_pf.configure(text=f'top-{doc.get("n")} {picked} combined via {eng}  ·  '
                                f'{span}  ·  built in '
                                f'{doc.get("built_secs", "?")}s  ·  {note}')
        sh = m.get('sharpe')
        segs = doc.get('segments') or {}
        if segs:
            def _s(name):
                s = (segs.get(name) or {}).get('sharpe')
                return f'{s:+.2f}' if s is not None else '—'
            text = (f'Sharpe  TRAIN {_s("train")} · VAL {_s("val")} · TEST {_s("test")}   ·   '
                    f'TEST: CAGR {m.get("cagr", 0) * 100:+.0f}%  MaxDD {m.get("dd", 0) * 100:.0f}%'
                    f'      (vs buy&hold TEST {b.get("sharpe", 0):+.2f})')
        else:
            text = (f'Sharpe {sh:+.2f}   ·   CAGR {m.get("cagr", 0) * 100:+.0f}%   ·   '
                    f'MaxDD {m.get("dd", 0) * 100:.0f}%      (vs buy&hold Sharpe {b.get("sharpe", 0):+.2f})')
        self.lbl_pf_m.configure(text=text, fg=(POS if (sh is not None and sh >= 0) else NEG))
        threading.Thread(target=self._render_pf_equity, args=(doc, self._pf_width()),
                         daemon=True).start()

    def _pf_width(self):
        """Target equity-image width = current panel width (so it fills the space, expandable)."""
        w = self.pf_card.winfo_width()
        if w <= 1:                                       # not laid out yet
            w = self.tree.winfo_width() or 900
        return max(700, min(w - 34, 3400))

    def _pf_rerender(self, delay=250):
        """Debounced background re-render of the equity PNG at the current width/height."""
        if not self._pf_doc:
            return
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
        self._pf_resize_after = self.root.after(
            delay, lambda: threading.Thread(target=self._render_pf_equity,
                                            args=(self._pf_doc, self._pf_width()), daemon=True).start())

    def _on_pf_resize(self, event):
        if not self._pf_doc:
            return
        if abs(self._pf_width() - self._pf_last_w) < 40:   # ignore tiny/noise resizes
            return
        self._pf_rerender()                              # debounce: re-render after resize settles

    def _on_pf_grip(self, e):
        """The grip above the PORTFOLIO card: cursor-to-image-bottom = new equity-plot height."""
        base = (self.pf_img.winfo_rooty() + self.pf_img.winfo_height()
                if self._pf_img_ref is not None           # an equity PNG is actually shown
                else self.pf_card.winfo_rooty() + self.pf_card.winfo_height())
        self.cfg['pf_h'] = max(160, min(800, base - e.y_root))
        self._pf_rerender(delay=180)                     # height is picked up by the render itself

    def _pf_grip_reset(self, _e=None):
        self.cfg['pf_h'] = 0
        self._save()
        self._pf_rerender(delay=0)

    def _embed_fig(self, parent, fig):
        """A LIVE matplotlib canvas inside Tk: the stock pan/zoom toolbar, wheel = zoom around
        the cursor (log-aware on log axes), double-click = reset view. Drawing happens on the
        main thread — the very reason the figure is built pyplot-free (no global state)."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

        class _FlatDPICanvas(FigureCanvasTkAgg):
            """CTk sets Tk's global 'scaling' from the FONT dpi; matplotlib's TkAgg reads that
            as a device PIXEL ratio (<Map> binding) and renders a buffer ~1.75x larger than its
            photo slot — the visible chart becomes a magnified crop. Tk windows here are 1x
            device pixels, so the ratio update is simply wrong: neutralize it at CLASS level
            (the Tk binding captures the bound method, instance patches don't intercept)."""
            def _update_device_pixel_ratio(self, event=None):
                pass

        canvas = _FlatDPICanvas(fig, master=parent)
        tb = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        try:                                              # theme the stock toolbar
            tb.configure(background=CARD)
            for ch in tb.winfo_children():
                try:
                    ch.configure(background=CARD)
                except tk.TclError:
                    continue
                if isinstance(ch, (tk.Button, tk.Checkbutton)):
                    # icons were rendered against the DEFAULT (light) Tk palette at toolbar
                    # creation; recolor the button first, then let mpl re-derive the icon —
                    # on a dark background it swaps in the foreground-tinted variant
                    ch.configure(foreground=TXT, activebackground=BORDER,
                                 activeforeground=TXT, highlightthickness=0)
                    if isinstance(ch, tk.Checkbutton):
                        ch.configure(selectcolor=BORDER)  # Pan/Zoom pressed state
                    if getattr(ch, '_image_file', None):
                        tb._set_image_for_button(ch)
                elif isinstance(ch, tk.Label):
                    ch.configure(foreground=MUT)
            # Tk paints DISABLED image-buttons with a checkerboard stipple (ugly, and RGBA
            # icons flatten to a light block). Back/Forward are safe no-ops on an empty
            # history, so keep them enabled instead of letting mpl gray them out.
            tb.set_history_buttons = lambda: None
            for k in ('Back', 'Forward'):
                if k in tb._buttons:
                    tb._buttons[k]['state'] = 'normal'
        except (tk.TclError, AttributeError):
            pass
        tb.update()
        tb.pack(anchor='w', fill='x')
        w = canvas.get_tk_widget()
        w.configure(background=CARD, highlightthickness=0)
        w.pack(fill='both', expand=True)

        def _zoomed(lo, hi, c, f, log):
            if log and lo > 0 and c > 0:
                import math
                l0, l1, lc = math.log10(lo), math.log10(hi), math.log10(c)
                return 10 ** (lc - (lc - l0) * f), 10 ** (lc + (l1 - lc) * f)
            return c - (c - lo) * f, c + (hi - c) * f

        def on_scroll(ev):
            # every axes under the cursor, each in ITS OWN data coords — a twinx sibling
            # (e.g. library size on the progress chart) must zoom its own y scale too;
            # a shared x-axis is zoomed ONCE per group or twins would compound to f²
            f = 1 / 1.25 if ev.button == 'up' else 1.25
            hit, done_x = False, []
            for ax in canvas.figure.axes:
                if not ax.in_axes(ev):
                    continue
                xd, yd = ax.transData.inverted().transform((ev.x, ev.y))
                if not any(ax.get_shared_x_axes().joined(ax, o) for o in done_x):
                    ax.set_xlim(_zoomed(*ax.get_xlim(), xd, f, ax.get_xscale() == 'log'))
                    done_x.append(ax)
                ax.set_ylim(_zoomed(*ax.get_ylim(), yd, f, ax.get_yscale() == 'log'))
                hit = True
            if hit:
                canvas.draw_idle()

        def on_press(ev):
            if getattr(ev, 'dblclick', False):
                tb.home()
                canvas.draw_idle()

        canvas.mpl_connect('scroll_event', on_scroll)
        canvas.mpl_connect('button_press_event', on_press)
        canvas.draw()

        def _sync(_e=None):
            """CTk fixes the toplevel geometry a beat AFTER creation; the last <Configure> can
            race the canvas redraw and leave a stale, larger buffer on screen. Force the figure
            to the widget's FINAL pixel size and re-blit."""
            try:
                wpx, hpx = w.winfo_width(), w.winfo_height()
                if wpx > 50 and hpx > 50:
                    fig.set_size_inches(wpx / fig.dpi, hpx / fig.dpi, forward=False)
                    canvas.draw()
            except Exception:                             # noqa: BLE001 — cosmetic safety net
                pass
        w.after(350, _sync)
        return canvas

    @staticmethod
    def _mpl_rc():
        """rcParams matching the active theme — an equity PNG is an image, so it inherits nothing
        from the widgets around it and has to be told the colours."""
        return {'figure.facecolor': CARD, 'axes.facecolor': CARD, 'savefig.facecolor': CARD,
                'text.color': TXT, 'axes.labelcolor': MUT, 'axes.titlecolor': TXT,
                'axes.edgecolor': BORDER, 'xtick.color': MUT, 'ytick.color': MUT,
                'grid.color': GRID, 'legend.facecolor': CARD, 'legend.edgecolor': BORDER,
                'legend.labelcolor': TXT}

    def _pf_figure(self, doc, width, fig_h, dpi=100):
        """The portfolio chart as a plain Figure (no pyplot, no global state): shared by the
        card's PNG render (background thread) and the interactive zoom window (main thread)."""
        from matplotlib.figure import Figure
        import matplotlib.transforms as mtrans
        import numpy as np
        import pandas as pd
        eq = doc['equity']
        x = pd.to_datetime(eq['dates'])
        op = doc.get('open_pnl') or None
        if op is not None and len(op) != len(eq['dates']):
            op = None                                     # malformed doc — draw without the panel
        fig = Figure(figsize=(width / dpi, fig_h), dpi=dpi, facecolor=CARD)
        if op is not None:
            gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
            ax = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1], sharex=ax)
        else:
            ax = fig.add_subplot(111)
            ax2 = None
        ax.plot(x, eq['combined'], lw=2.0, color=ACC, label=f'Portfolio (top-{doc.get("n")})')
        ax.plot(x, eq['basket'], lw=1.2, color='#f9a825', ls=':', label='buy & hold (EW)')
        ax.set_yscale('log'); ax.grid(True, which='both', alpha=0.3)
        bounds = doc.get('bounds') or {}
        # full-span: segment names sit along the top edge -> legend goes bottom-right
        ax.legend(loc=('lower right' if bounds and doc.get('span') else 'upper left'), fontsize=8)
        if bounds and doc.get('span'):                    # full-span doc: mark the segments
            trans = mtrans.blended_transform_factory(ax.transData, ax.transAxes)
            lo0, hi0 = x[0], x[-1]
            edges = [lo0] + [min(max(pd.Timestamp(bounds[k], tz=lo0.tz), lo0), hi0)
                             for k in ('val_start', 'test_start')] + [hi0]
            for e in edges[1:-1]:
                if lo0 < e < hi0:                         # a late --sim-start can clip a segment
                    ax.axvline(e, color=MUT, lw=0.9, ls='--', alpha=0.55)
                    if ax2 is not None:
                        ax2.axvline(e, color=MUT, lw=0.9, ls='--', alpha=0.55)
            for name, lo, hi in zip(('TRAIN', 'VAL', 'TEST'), edges[:-1], edges[1:]):
                if hi > lo:
                    ax.text(lo + (hi - lo) / 2, 0.985, name, transform=trans,
                            ha='center', va='top', fontsize=7.5, color=MUT, alpha=0.9)
            title = f'combined equity — TRAIN / VAL / TEST ({doc["span"]})'
        else:                                             # docs built before the full-span change
            title = f'combined equity — TEST ({doc.get("test", "")})'
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=8)
        if ax2 is not None:
            opv = np.asarray(op, dtype=float)
            ax2.fill_between(x, opv, 0, where=opv >= 0, color=POS, alpha=0.30, lw=0)
            ax2.fill_between(x, opv, 0, where=opv < 0, color=NEG, alpha=0.30, lw=0)
            ax2.plot(x, opv, lw=0.9, color=MUT)
            ax2.axhline(0, color=MUT, lw=0.8)
            ax2.grid(True, alpha=0.3)
            ax2.tick_params(labelsize=8)
            ax2.set_ylabel('open PnL', fontsize=8)
            ax2.text(0.005, 0.93, 'open PnL — unrealized gain/loss of the held '
                                  'positions (share of book; a close/flip realizes it away)',
                     transform=ax2.transAxes, fontsize=7, color=MUT, va='top')
        import warnings as _w
        with _w.catch_warnings():                         # tight_layout grumbles about sharex axes
            _w.simplefilter('ignore')
            fig.tight_layout()
        return fig

    def _pf_interactive(self, _e=None):
        """Double-click on the portfolio PNG: the same chart as a LIVE canvas — wheel zoom,
        pan, toolbar. The card keeps the static PNG (it auto-fits the layout)."""
        doc = self._pf_doc
        if not doc or not (doc.get('equity') or {}).get('dates'):
            return
        import matplotlib
        # CTk multiplies the geometry string by window_scaling, while the figure is raw pixels —
        # divide back (same trick as _open_plot) so the canvas is not stretched vs the figure
        win = self._dialog('Portfolio equity — interactive (wheel: zoom · drag with ✥: pan · '
                           'double-click: reset)',
                           f'{int(1200 / self.SCALE)}x{int(660 / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=14, pady=12)
        with self._plot_lock, matplotlib.rc_context(self._mpl_rc()):
            fig = self._pf_figure(doc, width=1150, fig_h=5.3)
        self._embed_fig(body, fig)

    def _render_pf_equity(self, doc, width=900):
        eq = doc.get('equity') or {}
        if not eq.get('dates'):
            return
        try:
            self._pf_last_w = width
            with self._plot_lock:
                import matplotlib
                dpi = 100
                ph = self.cfg.get('pf_h') or 0            # user-dragged height (px), 0 = automatic
                fig_h = (ph / dpi) if ph else min(3.8, max(2.4, width / dpi / 4.5))
                if doc.get('open_pnl') and not ph:
                    fig_h = min(4.8, fig_h * 1.3)         # room for the lower panel
                with matplotlib.rc_context(self._mpl_rc()):
                    fig = self._pf_figure(doc, width, fig_h, dpi=dpi)
                    fig.savefig(PORTFOLIO_PNG, dpi=dpi, facecolor=CARD)
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
        # the GUI's timeframe selector wins over config.ini (load_config in THIS process only
        # sees the ini/env, not the widget), and the data snapshot follows the timeframe
        if cfg.get('tf', '1d') != self._tf():
            from timeframe import resolve as _rtf
            _t = _rtf(self._tf())
            cfg.update(tf=_t.name, ann=_t.periods_per_year, freq=_t.pandas_freq,
                       vol_window=_t.vol_window, ewma_lambda=_t.ewma_lambda,
                       binance_interval=_t.binance_interval)
        cfg['data'] = self._data_file()
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
               str(cfg['start']), str(cfg['end']), cfg.get('tf', '1d'))
        cached = self._panel_cache.get(key)
        if cached is None:
            tk_, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'],
                                          cfg.get('instruments'), freq=cfg.get('freq', 'D'))
            cached = (tk_, panel, make_market(panel, tk_, raw,
                                              vol_window=cfg.get('vol_window', 30)),
                      basket_returns(panel))
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
        holder = {'done': False, 'fig': None, 'err': None, 'dpi': dpi,
                  'figsize': (img_w / dpi, (img_h - 40) / dpi)}   # -40: room for the zoom toolbar
        # img_w/img_h are REAL pixels (the PNG is shown 1:1), but CTk multiplies a geometry string by
        # window_scaling — so divide it back out, or the window opens ~SCALE times wider than its chart.
        win = self._dialog('Equity — ' + champ.get('formula', '')[:60],
                           f'{int((img_w + 44) / self.SCALE)}x{int((img_h + 200) / self.SCALE)}')

        head = self._box(win)
        head.pack(fill='x', padx=16, pady=(14, 6))

        def seg(name, m, accent=False):
            m = m or {}
            sh, cg, dd = m.get('sharpe'), m.get('cagr'), m.get('dd')
            txt = f'{name}:  Sharpe {sh:+.2f}' if sh is not None else f'{name}:  —'
            if cg is not None:
                txt += f'   CAGR {cg*100:+.0f}%'
            if dd is not None:
                txt += f'   DD {dd*100:.0f}%'
            self._lbl(head, text=txt, text_color=(NEG if accent else TXT), anchor='w',
                         font=(self.UI, 11, 'bold' if accent else 'normal')).pack(anchor='w')

        seg('TRAIN', champ.get('train'))
        seg('VAL', champ.get('val'))
        seg('TEST (held-out)', champ.get('test'), accent=True)
        self._lbl(head, text=champ.get('formula', ''), text_color=MUT, justify='left', anchor='w',
                     wraplength=img_w - 30, font=(self.MONO, 12)).pack(anchor='w', pady=(6, 0))
        btnrow = self._box(head)
        btnrow.pack(anchor='w', pady=(10, 0))
        self._btn(btnrow, 'Paper Trade — build bundle', lambda: self._paper_trade(champ),
                  width=230).pack(side='left')
        self._btn(btnrow, 'Download signals (CSV)', lambda: self._download_signals(champ),
                  width=196).pack(side='left', padx=(8, 0))
        _f = champ.get('formula', '')
        self._btn(btnrow, 'Serve signal (API)',
                  lambda: self._serve_signal([_f], 'alpha_' + hashlib.md5(_f.encode()).hexdigest()[:6]),
                  width=176).pack(side='left', padx=(8, 0))
        pdf_btn = self._btn(btnrow, 'PDF report', lambda: self._pdf_report_alpha(champ), width=110)
        pdf_btn.pack(side='left', padx=(8, 0))
        self._tip(pdf_btn, 'Analytics dashboard as PDF: KPIs, equity and drawdown,\n'
                           'exposure and turnover, weight structure, monthly returns,\n'
                           'TRAIN/VAL/TEST breakdown and conclusions.')
        fwd_btn = self._btn(btnrow, 'Forward track ➕',
                            lambda: self._fwd_enroll(
                                [_f], 'alpha_' + hashlib.md5(_f.encode()).hexdigest()[:6], 'alpha'),
                            width=150)
        fwd_btn.pack(side='left', padx=(8, 0))
        self._tip(fwd_btn, 'Enroll this alpha into the FORWARD TRACK: freeze it (formula +\n'
                           'universe + vol/fee) and paper-step it once per closed daily bar\n'
                           'on live Binance data. Append-only — the honest forward test.')

        body = self._box(win)
        body.pack(fill='both', expand=True, padx=16, pady=(4, 14))
        status = self._lbl(body, text='building equity (TRAIN | VAL | TEST + basket B&H)…',
                              text_color=MUT, font=(self.UI, 15))
        status.pack(pady=40)

        threading.Thread(target=self._compute_equity, args=(champ, holder), daemon=True).start()
        self.root.after(200, lambda: self._check_plot(win, holder, status, body))

    # ---------- download portfolio signals (CSV) ----------
    def _alpha_weights_wide(self, formula, cfg, market, panel):
        """Target-weight table of one alpha over the whole panel — the same inverse-vol +
        chips normalization the engine trades with. Shared by the CSV export and the PDF report."""
        import numpy as np
        import pandas as pd
        from genome import parse
        from evaluator import eval_alpha_panel
        ap = eval_alpha_panel(parse(formula), panel)
        A = pd.DataFrame(ap[market['tk']].to_numpy(dtype=np.float64)).ffill().to_numpy()
        V = market['V']
        E = market['base_elig'] & np.isfinite(A)                # eligible & has a signal
        fc = np.where(E, A, 0.0) / V                            # inverse-vol (as in the engine)
        fc = np.where(E, fc, 0.0)
        chips = np.nansum(np.abs(fc), axis=1, keepdims=True)    # normalization by "chips"
        W = fc / np.where(chips == 0.0, 1.0, chips)             # target weight: + long / − short
        return pd.DataFrame(np.round(W, 6), index=market['index'], columns=market['tk'])

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
            cfg = self._build_plot_cfg()
            _tk, panel, market, _basket = self._get_market(cfg)
            wide = self._alpha_weights_wide(formula, cfg, market, panel)
            self._signals_from_wide(wide, cfg['splits'], path, panel=panel)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Error', f'Failed to build signals: {e}', parent=self.root)

    def _signals_from_wide(self, wide, splits, path, panel=None):
        """Wide target-weight table (index=date, cols=tickers) -> tidy CSV (row = one position,
        with the asset's OHLCV on that bar when `panel` is given) + the 'what to hold now'
        dialog. Shared by a single alpha and the combined portfolio."""
        import numpy as np
        wide = wide.copy()
        wide.index.name = 'date'
        wide = wide[wide.abs().sum(axis=1) > 0]                    # drop empty (pre-listing) days
        long = wide.reset_index().melt(id_vars='date', var_name='ticker', value_name='weight')
        long = long[long['weight'].abs() > 0.0005].copy()
        long['side'] = np.where(long['weight'] > 0, 'LONG', 'SHORT')
        long['weight_pct'] = long['weight'].map(lambda x: f'{x * 100:+.1f}%')
        d = long['date']
        long['segment'] = np.where(d < splits['val'][0], 'TRAIN',
                                   np.where(d < splits['test'][0], 'VAL', 'TEST'))
        long['_aw'] = long['weight'].abs()
        long = long.sort_values(['date', '_aw'], ascending=[True, False]).drop(columns='_aw')
        cols = ['date', 'segment', 'ticker', 'side', 'weight', 'weight_pct']
        if panel is not None:                                      # the asset's bar next to its weight
            ii = panel['close'].index.get_indexer(long['date'])
            jj = panel['close'].columns.get_indexer(long['ticker'])
            ok = (ii >= 0) & (jj >= 0)
            for c in ('open', 'high', 'low', 'close', 'volume'):
                arr = panel[c].to_numpy()
                long[c] = np.where(ok, arr[np.clip(ii, 0, None), np.clip(jj, 0, None)], np.nan)
            cols += ['open', 'high', 'low', 'close', 'volume']
        long = long[cols]
        long.to_csv(path, index=False)
        last = wide.iloc[-1]
        pos = sorted([(t, float(v)) for t, v in last.items() if abs(v) > 0.0005],
                     key=lambda kv: -abs(kv[1]))
        rng = f'{wide.index[0].date()}..{wide.index[-1].date()}'
        self._signals_dialog(path, wide.index[-1].date(), pos, len(wide), rng=rng)

    # ---------- forward track (append-only paper stepping inside the node) ----------
    def _fwd_lib(self):
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import forward_track
        return forward_track

    def _fwd_universe(self):
        """The universe to FREEZE into an enrollment — same resolution as the paper buttons."""
        c = self.cfg
        if c.get('universe_all', True):
            try:
                return list(pickle.load(open(apppaths.data_path(), 'rb'))[0])
            except Exception as e:                             # noqa: BLE001
                messagebox.showerror('Forward track', f'Cannot read the loaded data: {e}',
                                     parent=self.root)
                return None
        return [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()] or None

    def _fwd_enroll(self, formulas, name, kind):
        formulas = [f for f in (formulas or []) if f and f.strip()]
        if not formulas:
            return
        tickers = self._fwd_universe()
        if not tickers:
            messagebox.showwarning('Forward track', 'The pairs universe is empty.', parent=self.root)
            return
        tf = self._tf()                                   # frozen with the strategy: windows are in
        ft = self._fwd_lib()                              # bars of the tf the formula was mined on
        track = ft.load_track()
        dup = ft.find_duplicate(track, formulas, tickers, tf)
        if dup:
            messagebox.showinfo('Forward track',
                                f'Already enrolled: {dup["id"]} (since {dup["enrolled"]}).',
                                parent=self.root)
            return
        c = self.cfg
        start = str(c.get('train_start', '2019-09-05'))
        if tf != '1d':
            try:                                          # don't page YEARS of 1h/15m klines every
                from timeframe import resolve as _rtf     # step — the tf's recommended history is
                start = max(start, _rtf(tf).history)      # plenty of warm-up (ISO strings compare)
            except Exception:                             # noqa: BLE001
                pass
        entry = ft.new_entry(name, kind, formulas, tickers, c.get('target_vol', 0.25),
                             c.get('exec_cost', 0.001), start, tf=tf)
        track['entries'].append(entry)
        ft.save_track(track)
        self._fwd_refresh()
        intr = ('' if tf == '1d' else
                f'\n⚠ {tf} bars: steps only happen while the app is open — long gaps make the '
                'track sparse; fees/slippage weigh more intraday, read it conservatively.')
        if messagebox.askyesno(
                'Forward track',
                f'{entry["id"]} enrolled. The strategy is FROZEN as of today '
                f'({len(formulas)} formula{"s" if len(formulas) > 1 else ""}, {len(tickers)} assets, '
                f'{tf} bars, vol {c.get("target_vol", 0.25)}, fee {c.get("exec_cost", 0.001)}) and '
                f'will be paper-stepped once per closed {tf} bar while the app is open.\n'
                'History is append-only — forward numbers are never recomputed backwards.'
                f'{intr}\n\nRun the first step now (downloads live Binance data)?',
                parent=self.root):
            self._fwd_step()

    def _pf_fwd_enroll(self):
        doc = self._pf_doc
        formulas = (doc or {}).get('formulas_full')
        if not formulas:
            messagebox.showinfo('Forward track', 'Build the portfolio first.', parent=self.root)
            return
        self._fwd_enroll(list(formulas), f'portfolio_top{doc.get("n", len(formulas))}', 'portfolio')

    def _fwd_step(self, force=False):
        if self._fwd_proc and self._fwd_proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR)
        try:
            self._fwd_proc = subprocess.Popen(
                _child_cmd('forward') + ['step'] + (['--force'] if force else []), env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:                                 # noqa: BLE001
            self.lbl_fwd.configure(text=f'step failed to start: {e}')
            return
        self.btn_fwd_step.configure(state='disabled')
        self.lbl_fwd.configure(text='stepping — downloading live closed bars and running the engine…')
        threading.Thread(target=self._fwd_wait, args=(self._fwd_proc,), daemon=True).start()

    def _fwd_wait(self, proc):
        out = proc.stdout.read() if proc.stdout else ''
        proc.wait()
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        tail = lines[-1] if lines else ''
        self.root.after(0, lambda: self._fwd_step_done(tail))

    def _fwd_step_done(self, tail):
        try:
            self.btn_fwd_step.configure(state='normal')
            self._fwd_refresh(status=tail)
        except tk.TclError:                                    # window closed while stepping
            pass

    def _fwd_refresh(self, status=None):
        ft = self._fwd_lib()
        entries = [e for e in ft.load_track()['entries'] if not e.get('archived')]
        self._fwd_entries = entries
        for i in self.fwd_tree.get_children():
            self.fwd_tree.delete(i)
        for e in entries:
            m = ft.metrics(e)
            tf = e.get('tf', '1d')
            kind = e['kind'] if tf == '1d' else f'{e["kind"]}·{tf}'
            self.fwd_tree.insert('', 'end', iid=e['id'], values=(
                e['id'], kind, e['enrolled'], m['days'],
                f'${m["equity"]:,.0f}', f'{m["ret"] * 100:+.1f}%',
                (f'{m["sharpe"]:+.2f}' if m['sharpe'] is not None else '—'),
                (f'{m["dd"] * 100:.0f}%' if m['dd'] is not None else '—'),
                m['last'] or '—'))
        if entries:
            base = (f'{len(entries)} enrolled · steps run automatically once per closed daily bar '
                    '(UTC) while the app is open · history is append-only — forward numbers are '
                    'written by live steps only, never recomputed.')
        else:
            base = ('empty — enroll a champion (double-click it in the leaderboard → "Forward '
                    'track") or the built portfolio ("Track"). Enrollment freezes the strategy; '
                    'the node then paper-steps it daily on live data, append-only.')
        self.lbl_fwd.configure(text=(f'{status}   ·   {base}' if status else base))

    def _fwd_tick(self):
        """Step whenever any enrolled strategy has an unseen CLOSED bar of its own timeframe.
        Re-checks every 5 minutes — cheap (reads one small JSON); missed bars are harmless
        (mark-to-market covers the gap on the next step)."""
        try:
            ft = self._fwd_lib()
            due = [e for e in ft.load_track()['entries']
                   if not e.get('archived') and ft.is_due(e)]
            if due and not (self._fwd_proc and self._fwd_proc.poll() is None):
                self._fwd_step()
        except Exception:                                      # noqa: BLE001 — never kill the loop
            pass
        self.root.after(5 * 60 * 1000, self._fwd_tick)

    def _fwd_selected(self):
        sel = self.fwd_tree.selection()
        if not sel:
            messagebox.showinfo('Forward track', 'Select a strategy in the table first.',
                                parent=self.root)
            return None
        return next((e for e in self._fwd_entries if e['id'] == sel[0]), None)

    def _fwd_archive(self):
        e = self._fwd_selected()
        if not e:
            return
        if not messagebox.askyesno('Forward track',
                                   f'Archive {e["id"]}? It stops stepping; the history stays in '
                                   'state/forward.json (append-only), the row leaves the table.',
                                   parent=self.root):
            return
        ft = self._fwd_lib()
        track = ft.load_track()
        for x in track['entries']:
            if x['id'] == e['id']:
                x['archived'] = True
        ft.save_track(track)
        self._fwd_refresh()

    @staticmethod
    def _fwd_book_str(d):
        """{'BTCUSDT': -0.224, ...} -> 'BTC −22.4% · ETH +5.1%' (base asset, signed % of equity)."""
        if not d:
            return '—'
        items = sorted(d.items(), key=lambda kv: -abs(kv[1]))
        return ' · '.join(f'{t.replace("USDT", "")} {v * 100:+.1f}%' for t, v in items[:8]) \
            + (' …' if len(items) > 8 else '')

    def _fwd_signals(self):
        e = self._fwd_selected()
        if not e:
            return
        hist = e.get('history') or []
        if not hist:
            messagebox.showinfo('Forward signals', 'No steps yet — the log appears after the '
                                                   'first live step.', parent=self.root)
            return
        win = self._dialog(f'Forward signals — {e["id"]}', '1080x600')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=16, pady=14)
        st = e.get('state') or {}
        pos, prc = st.get('positions') or {}, st.get('prices') or {}
        eq = float(st.get('equity') or e['start_capital'])
        now_book = {t: u * float(prc.get(t, 0.0)) / eq for t, u in pos.items() if eq > 0}
        self._lbl(frm, text=f'Holding now:  {self._fwd_book_str(now_book)}',
                  text_color=TXT, font=(self.MONO, 13, 'bold')).pack(anchor='w')
        self._lbl(frm, text='book = signed % of equity per asset · trade = executed rebalance in $ '
                            '· rows before this feature show “—” (the log is append-only)',
                  text_color=FAINT, font=(self.UI, 11)).pack(anchor='w', pady=(2, 8))
        row = self._box(frm)                              # bottom bar FIRST — the table then takes
        row.pack(side='bottom', fill='x', pady=(10, 0))   # what's left and can't push it out of view
        self._btn(row, 'Export CSV (full log)', lambda: self._fwd_signals_csv(e),
                  width=190).pack(side='right')
        hist_note = self._lbl(row, text='', text_color=FAINT, font=(self.UI, 11))
        hist_note.pack(side='left')
        wrap = self._box(frm)
        wrap.pack(fill='both', expand=True)
        cols = ('date', 'book', 'trades', 'pnl', 'fees')
        tv = ttk.Treeview(wrap, columns=cols, show='headings', height=12)
        for c, txt, w, anc in (('date', 'BAR', 130, 'center'), ('book', 'BOOK HELD', 330, 'w'),
                               ('trades', 'REBALANCE ($)', 300, 'w'),
                               ('pnl', 'P&L $', 90, 'e'), ('fees', 'FEES $', 80, 'e')):
            tv.heading(c, text=txt)
            tv.column(c, width=int(w * self.SCALE), anchor=anc, stretch=(c in ('book', 'trades')))
        shown = hist[-400:]                               # a 1h track grows long — cap the widget
        for i, h in enumerate(reversed(shown)):           # latest first
            tr = h.get('trades')
            trs = ('—' if tr is None else
                   (' · '.join(f'{t.replace("USDT", "")} {v:+,.0f}'
                               for t, v in sorted(tr.items(), key=lambda kv: -abs(kv[1]))[:8])
                    or 'no trades'))
            tv.insert('', 'end', tags=('odd' if i % 2 else 'even',), values=(
                h['date'], self._fwd_book_str(h.get('pos')) if h.get('pos') is not None else '—',
                trs, f'{h.get("pnl", 0):+,.2f}', f'{h.get("fees", 0):,.2f}'))
        tv.tag_configure('odd', background=STRIPE)
        tv.tag_configure('even', background=CARD)
        vs = ctk.CTkScrollbar(wrap, orientation='vertical', command=tv.yview, fg_color=CARD,
                              button_color=BORDER, button_hover_color=FAINT, width=14)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        if len(hist) > len(shown):
            hist_note.configure(text=f'showing last {len(shown)} of {len(hist)} steps — '
                                     'the CSV has all of them')

    def _fwd_signals_csv(self, e):
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save forward signals log', defaultextension='.csv',
            initialfile=f'forward_{e["id"]}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        rows = []
        for h in e.get('history') or []:
            pos, tr = h.get('pos') or {}, h.get('trades') or {}
            for t in sorted(set(pos) | set(tr), key=lambda x: -abs(pos.get(x, 0.0))):
                rows.append([h['date'], t, pos.get(t, ''), tr.get(t, ''),
                             h.get('pnl', ''), h.get('fees', ''), h.get('equity', '')])
            if not pos and not tr:                        # pre-feature rows / flat bars stay visible
                rows.append([h['date'], '', '', '', h.get('pnl', ''), h.get('fees', ''),
                             h.get('equity', '')])
        self._save_csv(path, ('bar', 'ticker', 'book_frac', 'trade_usd', 'step_pnl',
                              'step_fees', 'equity'), rows, 'signal rows')

    def _fwd_chart(self):
        e = self._fwd_selected()
        if not e:
            return
        hist = e.get('history') or []
        if not hist:
            messagebox.showinfo('Forward track', 'No steps yet — the curve appears after the '
                                                 'first daily step.', parent=self.root)
            return
        import matplotlib
        from matplotlib.figure import Figure
        import pandas as pd
        win = self._dialog(f'Forward — {e["id"]}  (wheel: zoom · double-click: reset)',
                           f'{int(1040 / self.SCALE)}x{int(580 / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=14, pady=12)
        x = pd.to_datetime([h['date'] for h in hist])
        y = [h['equity'] for h in hist]
        with self._plot_lock, matplotlib.rc_context(self._mpl_rc()):
            fig = Figure(figsize=(9.9, 4.5), dpi=100, facecolor=CARD)
            ax = fig.add_subplot(111)
            ax.plot(x, y, lw=2.0, color=ACC, label=f'{e["id"]} · {len(hist)} live steps')
            ax.axhline(e['start_capital'], color=MUT, lw=0.9, ls='--')
            ax.set_title(f'forward equity — enrolled {e["enrolled"]} · append-only '
                         '(live steps, nothing recomputed)', fontsize=9)
            ax.legend(loc='upper left', fontsize=8)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)
            fig.tight_layout()
        self._embed_fig(body, fig)

    # ---------- PDF analytics report (single alpha and the portfolio) ----------
    def _worker_cfg(self):
        """The engine settings (universe, vol/fee, TRAIN/VAL/TEST dates) a worker process needs to
        reproduce what the leaderboard searched with. Shared by the PDF worker payloads."""
        c = self.cfg
        return {
            'instruments': (None if c.get('universe_all', True) else
                            [x.strip().upper() for x in c.get('universe_list', '').split(',')
                             if x.strip()] or None),
            'vol': float(c.get('target_vol', 0.25)), 'exec': float(c.get('exec_cost', 0.001)),
            'train_start': c.get('train_start'), 'val_start': c.get('val_start'),
            'test_start': c.get('test_start'), 'test_end': c.get('test_end'),
        }

    def _run_pdf_report(self, payload, out_path):
        """Build the report in a CHILD process (pdf_worker) and wait on its pipe from a background
        thread. matplotlib's FreeType text rasterization segfaults when it runs while Tk's main loop
        is live — both drive Xft/FreeType and the X error lands asynchronously as a SIGSEGV. A
        subprocess has its own address space (no shared FreeType) and its own GIL, so the GUI merely
        waits on a pipe. Same pattern as the leaderboard metrics worker.

        The thread only writes into `holder`; the MAIN thread polls it via after() and shows the
        dialog — Tk is not thread-safe, so we never touch a widget from the worker thread (this is
        exactly how the equity chart delivers its result, see _open_plot/_check_plot)."""
        payload = dict(payload, out=out_path)
        holder = {'done': False, 'info': None, 'err': None}

        def work():
            try:
                env = dict(os.environ)
                env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                           ALPHANODE_CONFIG_INI=apppaths.config_ini())
                proc = subprocess.Popen(_child_cmd('pdfreport'), env=env,
                                        cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
                try:
                    out, _ = proc.communicate(json.dumps(payload), timeout=600)
                except Exception:
                    proc.kill()                          # don't leave a runaway child behind
                    raise
                doc = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
                if not doc.get('ok'):
                    raise RuntimeError(doc.get('error', 'pdf worker failed'))
                holder['info'] = doc.get('info') or {}
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, lambda: self._check_pdf(holder, out_path))

    def _check_pdf(self, holder, out_path):
        """Main-thread poll of the PDF worker; show the result dialog when it finishes."""
        if not holder['done']:
            self.root.after(150, lambda: self._check_pdf(holder, out_path))
            return
        if holder['err']:
            messagebox.showerror('PDF report', f'Failed to build report: {holder["err"]}',
                                 parent=self.root)
        else:
            self._pdf_done_dialog(out_path, holder['info'])

    def _pdf_done_dialog(self, path, info):
        win = self._dialog('PDF report', '440x190')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(frm, text='Report saved', text_color=TXT,
                     font=(self.UI, 19, 'bold')).pack(anchor='w')
        self._lbl(frm, text=path, text_color=MUT, font=(self.MONO, 12), wraplength=390,
                     justify='left', anchor='w').pack(anchor='w', pady=(2, 6))
        self._lbl(frm, text=f'{(info or {}).get("pages", "?")} pages · '
                            f'{(info or {}).get("days", "?")} trading days',
                     text_color=FAINT, font=(self.UI, 12)).pack(anchor='w', pady=(0, 10))
        row = self._box(frm)
        row.pack(anchor='w')
        self._btn(row, 'Open folder', lambda: self._open_folder(path), width=120).pack(side='left')
        self._btn(row, 'Close', win.destroy, width=90).pack(side='left', padx=(8, 0))

    def _pdf_report_alpha(self, champ, status_lbl=None):
        formula = champ.get('formula', '')
        if not formula:
            return
        name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save PDF report', defaultextension='.pdf',
            initialfile=f'report_{name}.pdf', filetypes=[('PDF', '*.pdf')])
        if not path:
            return
        if status_lbl is not None:
            status_lbl.configure(text='building PDF report…')
        seg = {k: dict(champ[k]) for k in ('train', 'val', 'test')
               if isinstance(champ.get(k), dict)}
        payload = {'kind': 'alpha', 'formula': formula, 'seg_metrics': seg,
                   'title': 'AlphaNode — alpha report', 'subtitle': formula,
                   'stamp': f'AlphaNode · {datetime.now():%d.%m.%Y %H:%M} · {name}',
                   **self._worker_cfg()}
        self._run_pdf_report(payload, path)

    def _pf_pdf_report(self):
        doc = self._pf_doc
        if not doc or not doc.get('weights'):
            messagebox.showinfo('PDF report',
                                'Build the portfolio first (rebuild it if it was built by an '
                                'older version).', parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save PDF report', defaultextension='.pdf',
            initialfile=f'report_portfolio_top{doc.get("n", "")}.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not path:
            return
        slim = {k: doc.get(k) for k in ('weights', 'equity', 'test', 'metrics', 'n')}
        payload = {'kind': 'portfolio', 'doc': slim,
                   'title': f'AlphaNode — top-{doc.get("n", "?")} portfolio',
                   'subtitle': ' + '.join(doc.get('formulas', []))[:110],
                   'stamp': f'AlphaNode · {datetime.now():%d.%m.%Y %H:%M} · portfolio '
                            f'top-{doc.get("n", "?")} · TEST {doc.get("test", "")}',
                   **self._worker_cfg()}
        self._run_pdf_report(payload, path)

    # ---------- PORTFOLIO: CSV signals + paper-trade bundle (same as a single alpha) ----------
    def _pf_download_signals(self):
        doc = self._pf_doc
        if not doc or not doc.get('weights'):
            messagebox.showinfo('Portfolio signals',
                                'Build the portfolio first (rebuild it if it was built by an older version).',
                                parent=self.root)
            return
        if doc.get('tf', '1d') == '1d' and doc.get('weights_span') != 'full':
            if not messagebox.askyesno(
                    'Portfolio signals',
                    'This portfolio was built by an OLDER version — its stored weights cover '
                    'TEST only.\nPress "▶ Build portfolio" again and the CSV will span the whole '
                    'TRAIN/VAL/TEST history.\n\nExport the TEST-only CSV anyway?',
                    icon='warning', default='no', parent=self.root):
                return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save portfolio signals', defaultextension='.csv',
            initialfile=f'signals_portfolio_top{doc.get("n", "")}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        try:
            import numpy as np
            import pandas as pd
            w = doc['weights']
            idx = pd.to_datetime(w['dates']).tz_localize('UTC')   # match the tz-aware splits
            wide = pd.DataFrame(np.array(w['W'], dtype=float), index=idx, columns=w['tickers'])
            cfg = self._build_plot_cfg()
            try:                                                   # OHLCV columns are best-effort:
                _tk, panel, _market, _b = self._get_market(cfg)    # no data snapshot -> weights-only CSV
            except Exception:                                      # noqa: BLE001
                panel = None
            self._signals_from_wide(wide, cfg['splits'], path, panel=panel)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Error', f'Failed to build signals: {e}', parent=self.root)

    def _pf_paper_trade(self):
        if self._tf_gate('Paper trading'):
            return
        doc = self._pf_doc
        formulas = (doc or {}).get('formulas_full')
        if not formulas:
            messagebox.showinfo('Paper Trade',
                                'Build the portfolio first (rebuild it if it was built by an older version).',
                                parent=self.root)
            return
        try:
            sys.path.insert(0, HERE)
            import paper_export
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Paper Trade', f'The generator failed to load: {e}', parent=self.root)
            return
        c = self.cfg
        if c.get('universe_all', True):
            try:
                tickers = list(pickle.load(open(apppaths.data_path(), 'rb'))[0])
            except Exception as e:                                 # noqa: BLE001
                messagebox.showerror('Paper Trade', f'Cannot read the loaded data: {e}', parent=self.root)
                return
        else:
            tickers = [x.strip().upper() for x in c.get('universe_list', '').split(',') if x.strip()]
        if not tickers:
            messagebox.showwarning('Paper Trade', 'The pairs universe is empty.', parent=self.root)
            return
        name = f'portfolio_top{doc.get("n", len(formulas))}'
        meta = {'test': (doc.get('metrics') or {})}                # readme shows the combined TEST Sharpe
        try:
            path = paper_export.build_bundle(
                list(formulas), name, tickers, float(c.get('target_vol', 0.25)),
                float(c.get('exec_cost', 0.001)),
                str(doc.get('sim_start', c.get('train_start', '2019-09-05'))),
                apppaths.exports_dir(), meta=meta)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Paper Trade', f'Bundle build error: {e}', parent=self.root)
            return
        self._paper_dialog(path, len(tickers))

    def _signals_dialog(self, path, latest_date, positions, n_days, rng=None):
        win = self._dialog('Portfolio signals', '460x580')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(frm, text='Signals saved', text_color=TXT,
                     font=(self.UI, 19, 'bold')).pack(anchor='w')
        self._lbl(frm, text=path, text_color=MUT, font=(self.MONO, 12), wraplength=400,
                     justify='left', anchor='w').pack(anchor='w', pady=(2, 12))
        self._lbl(frm, text=f'What to hold on the last day ({latest_date}):', text_color=TXT,
                     font=(self.UI, 15, 'bold')).pack(anchor='w')
        self._lbl(frm, text='+ long · − short · %  = share of portfolio', text_color=FAINT,
                     font=(self.UI, 12)).pack(anchor='w', pady=(0, 8))
        tbl = self._box(frm)
        tbl.pack(fill='both', expand=True)
        for i, (t, w) in enumerate(positions[:16]):
            side, col = ('LONG', POS) if w > 0 else ('SHORT', NEG)
            rbg = STRIPE if i % 2 else CARD
            r = self._box(tbl, bg=rbg)
            r.pack(fill='x')
            self._lbl(r, text=side, text_color=col, font=(self.MONO, 13, 'bold'),
                         width=52, anchor='w').pack(side='left', padx=(6, 0))
            self._lbl(r, text=t, text_color=TXT, font=(self.MONO, 13), anchor='w').pack(side='left')
            self._lbl(r, text=f'{w * 100:+.1f}%', text_color=col, font=(self.MONO, 13, 'bold'),
                         anchor='e').pack(side='right', padx=(0, 8))
        covered = f'{n_days} bars' + (f', {rng}' if rng else '')
        self._lbl(frm, text=f'Full history ({covered}) — in the CSV: date, segment '
                            '(TRAIN/VAL/TEST), ticker, side, weight, weight_pct + the asset\'s '
                            'open/high/low/close/volume.',
                     text_color=MUT, font=(self.UI, 12), wraplength=400, justify='left',
                     anchor='w').pack(anchor='w', pady=(10, 0))
        self._btn(frm, 'Close', win.destroy, width=80).pack(anchor='e', pady=(10, 0))

    # ---------- paper trade: export the bundle + run ----------
    def _paper_trade(self, champ):
        if self._tf_gate('Paper trading'):
            return
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
        win = self._dialog('Paper-trading bundle ready', '660x300')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(frm, text='Bundle built', text_color=TXT,
                     font=(self.UI, 19, 'bold')).pack(anchor='w')
        self._lbl(frm, text=f'{n} pairs · engine + strategy.py + paper_trade.py + README.md',
                     text_color=MUT, font=(self.UI, 13)).pack(anchor='w', pady=(2, 10))
        self._lbl(frm, text=path, text_color=ACC, font=(self.MONO, 13), wraplength=600,
                     justify='left', anchor='w').pack(anchor='w')
        self._lbl(frm, text='Run paper trading FORWARD on new data — that is the honest check. '
                               'Live is not included in the bundle (see README).',
                     text_color=MUT, font=(self.UI, 13), wraplength=600, justify='left',
                     anchor='w').pack(anchor='w', pady=(10, 14))
        row = self._box(frm)
        row.pack(fill='x')
        self._btn(row, 'Open folder', lambda: self._open_folder(path), width=124).pack(side='left')
        self._btn(row, '▶ Run now', lambda: (win.destroy(), self._run_bundle(path)),
                  kind='accent', width=100).pack(side='left', padx=8)
        self._btn(row, 'Close', win.destroy, width=80).pack(side='right')

    def _open_folder(self, path):
        """Open a file or a folder in the system viewer."""
        if not path:
            return
        start = getattr(os, 'startfile', None)           # Windows
        if start is not None:
            try:
                start(path)
                return
            except Exception:                            # noqa: BLE001
                pass
        for opener in ('xdg-open', 'open'):
            try:
                subprocess.Popen([opener, path])
                return
            except Exception:                            # noqa: BLE001
                continue

    def _run_bundle(self, path):
        win = self._dialog('Paper-trade — step', '780x460')
        txt = self._console(win)

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
                import matplotlib
                from genome import parse
                from evaluator import simulate_returns
                import report
                cfg = self._build_plot_cfg()
                tk_, panel, market, basket = self._get_market(cfg)
                r = simulate_returns(parse(champ['formula']), tk_, panel, market, cfg['vol'],
                                     cfg['exec'], ann=cfg.get('ann', 365.0),
                                     ewma_lambda=cfg.get('ewma_lambda', 0.06))
                if r is None:
                    holder['err'] = 'the formula yields no valid returns on this data'
                else:
                    ts = (champ.get('test') or {}).get('sharpe')
                    label = 'strategy' + (f' · TEST Sharpe {ts:+.2f}' if ts is not None else '')
                    try:                                 # open-PnL panel: best-effort, chart survives without
                        from evaluator import open_pnl_series
                        wide = self._alpha_weights_wide(champ['formula'], cfg, market, panel)
                        op = open_pnl_series(wide, panel['ret'])
                    except Exception:                    # noqa: BLE001
                        op = None
                    with matplotlib.rc_context(self._mpl_rc()):
                        # a plain Figure (no pyplot): safe to build here in the worker thread and
                        # embed as a LIVE zoomable canvas on the main thread (_check_plot)
                        holder['fig'] = report.equity_figure(
                            {label: r}, basket, cfg['splits'],
                            'Growth of $1 (NET, log):  TRAIN | VAL | TEST   vs   EW basket (buy & hold)',
                            figsize=holder['figsize'], dpi=holder['dpi'],
                            facecolor=CARD, fg=MUT, axline=TXT, open_pnl=op)
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

    def _check_plot(self, win, holder, status, body):
        if not win.winfo_exists():
            return
        if not holder['done']:
            self.root.after(200, lambda: self._check_plot(win, holder, status, body))
            return
        if holder['err']:
            status.configure(text='Error: ' + holder['err'], fg=NEG)
            return
        try:
            status.destroy()
            self._embed_fig(body, holder['fig'])          # live canvas: wheel zoom / pan / reset
        except Exception as e:                            # noqa: BLE001
            status.configure(text=f'Failed to show the chart: {e}', fg=NEG)


def main():
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
