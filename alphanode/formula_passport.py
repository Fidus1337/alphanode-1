"""Formula passport — explain ANY mined formula in two layers, offline and deterministic.

Layer 1, anatomy: the expression tree drawn with a human label per node + the same tree
unrolled into numbered plain-English reading steps (every operator has a translation
template — no LLM, no network).

Layer 2, behavior: what the formula DOES, measured on the data —
  * its position on one asset's price (long / cash / short band under the chart);
  * which inputs feed it (ablation: neutralize each feature terminal, see how much the
    signal changes);
  * its character (holding period, long/short balance, momentum-vs-reversal score);
  * which archetype its RETURNS resemble (correlation with canonical strategies).

The window this feeds carries a warning on purpose: an explanation is not evidence.
A readable story makes a formula more convincing, not more profitable — validity is
still judged only by held-out TEST and the forward track.
"""
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Layer 1: translation dictionary
# ---------------------------------------------------------------------------
FEATURE_LABEL = {
    'close': 'close price', 'open': 'open price', 'high': 'bar high', 'low': 'bar low',
    'volume': 'traded volume', 'ret': '1-bar return', 'logret': '1-bar log return',
    'vwap': 'typical price (H+L+C)/3', 'range': 'bar range — intrabar volatility',
    'body': 'candle body (close−open)/open', 'dvol': 'dollar volume',
    'funding': 'funding rate (perp carry)',
}
# (node label for the tree, sentence template for the reading steps)
OP_LABEL = {
    'ts_mean':  ('avg {w}', 'average it over the last {w} bars'),
    'ts_sum':   ('sum {w}', 'sum it over the last {w} bars'),
    'ts_std':   ('vol {w}', 'take its volatility over the last {w} bars'),
    'ts_zscore': ('z {w}', 'ask how unusual it is vs its own last {w} bars (z-score)'),
    'ts_min':   ('min {w}', 'take its lowest value of the last {w} bars'),
    'ts_max':   ('max {w}', 'take its highest value of the last {w} bars'),
    'ts_delta': ('Δ {w}', 'take its change over {w} bars'),
    'ts_delay': ('lag {w}', 'take its value {w} bars ago'),
    'ts_roc':   ('%Δ {w}', 'take its % change over {w} bars'),
    'ema':      ('ema {w}', 'smooth it over ~{w} bars (EMA)'),
    'ts_rank':  ('rank {w}', 'rank it within its own last {w} bars'),
    'ts_corr':  ('corr {w}', 'correlate the two over the last {w} bars'),
    'ts_cov':   ('cov {w}', 'covariance of the two over the last {w} bars'),
    'ts_median': ('med {w}', 'take its median over the last {w} bars'),
    'ts_argmax': ('t@max {w}', 'bars since its {w}-bar high'),
    'ts_argmin': ('t@min {w}', 'bars since its {w}-bar low'),
    'ts_skew':  ('skew {w}', 'take its skewness over the last {w} bars'),
    'ts_kurt':  ('kurt {w}', 'take its kurtosis over the last {w} bars'),
    'decay_linear': ('decay {w}', 'weight it toward recent bars (linear decay over {w})'),
    'cs_rank':  ('rank⟂', 'rank it across assets on each bar (0..1)'),
    'cs_zscore': ('z⟂', 'z-score it across assets on each bar'),
    'cs_demean': ('−mean⟂', 'subtract the cross-asset average (market-neutralize)'),
    'cs_scale': ('scale⟂', 'rescale so asset weights sum to 1 in absolute value'),
    'sign':     ('sign', 'keep only its direction (+1 / 0 / −1)'),
    'tanh':     ('tanh', 'squash it into −1..+1 (tame outliers)'),
    'slog':     ('slog', 'compress extremes with a signed log'),
    'neg':      ('neg', 'flip its sign (bet the opposite)'),
    'abs':      ('abs', 'keep only its magnitude'),
    'add': ('+', 'add'), 'sub': ('−', 'subtract'), 'mul': ('×', 'multiply'),
    'div': ('÷', 'divide'), 'pmax': ('max(a,b)', 'take the larger of'),
    'pmin': ('min(a,b)', 'take the smaller of'),
}
_BINARY_WORD = {'add': 'add (step {a}) and (step {b})',
                'sub': 'subtract (step {b}) from (step {a})',
                'mul': 'multiply (step {a}) by (step {b})',
                'div': 'divide (step {a}) by (step {b})',
                'pmax': 'take the larger of (step {a}) and (step {b})',
                'pmin': 'take the smaller of (step {a}) and (step {b})',
                'ts_corr': 'correlate (step {a}) with (step {b}) over {w} bars',
                'ts_cov': 'covariance of (step {a}) and (step {b}) over {w} bars'}


def node_label(node):
    """Short label for a tree box."""
    if node.is_terminal:
        return node.op
    lbl = OP_LABEL.get(node.op, (node.op, node.op))[0]
    return lbl.format(w=node.window) if '{w}' in lbl else lbl


def reading_steps(root, max_steps=14):
    """The tree unrolled into numbered bottom-up steps a human can read aloud."""
    steps, memo = [], {}

    def walk(n):
        key = id(n)
        if key in memo:
            return memo[key]
        if n.is_terminal:
            steps.append(f'take the {FEATURE_LABEL.get(n.op, n.op)}')
        else:
            kids = [walk(c) for c in n.children]
            if len(kids) == 2:
                tpl = _BINARY_WORD.get(n.op, f'{n.op} (step {{a}}) and (step {{b}})')
                steps.append(tpl.format(a=kids[0], b=kids[1], w=n.window))
            else:
                sent = OP_LABEL.get(n.op, (n.op, n.op))[1]
                sent = sent.format(w=n.window) if '{w}' in sent else sent
                ref = '' if kids[0] == len(steps) else f' (step {kids[0]})'
                steps.append(sent + ref)
        memo[key] = len(steps)
        return memo[key]

    walk(root)
    steps.append('positive result → long the asset, negative → short '
                 '(then inverse-vol sizing and cross-asset normalization)')
    if len(steps) > max_steps:                    # deep trees: keep head and tail readable
        steps = steps[:max_steps - 4] + ['…'] + steps[-3:]
    return steps


# ---------------------------------------------------------------------------
# Layer 1: tree drawing (matplotlib only — no graphviz dependency)
# ---------------------------------------------------------------------------
_KIND_COLOR = {'feat': '#2e7d32', 'ts': '#1565c0', 'cs': '#6a1b9a', 'math': '#8a93a2'}


def _kind(node):
    if node.is_terminal:
        return 'feat'
    if node.op.startswith('ts_') or node.op in ('ema', 'decay_linear'):
        return 'ts'
    if node.op.startswith('cs_'):
        return 'cs'
    return 'math'


def draw_tree(ax, root, ink='#d7dce3'):
    """Layout: leaves get consecutive x slots, parents sit centered above children."""
    pos, next_x = {}, [0.0]

    def place(n, depth):
        if not n.children:
            pos[id(n)] = (next_x[0], -depth)
            next_x[0] += 1.0
        else:
            for c in n.children:
                place(c, depth + 1)
            xs = [pos[id(c)][0] for c in n.children]
            pos[id(n)] = (sum(xs) / len(xs), -depth)

    place(root, 0)

    def edges(n):
        for c in n.children:
            x0, y0 = pos[id(n)]
            x1, y1 = pos[id(c)]
            ax.plot([x0, x1], [y0, y1], color=ink, lw=0.7, alpha=0.45, zorder=1)
            edges(c)
    edges(root)

    for n in _all_nodes(root):
        x, y = pos[id(n)]
        c = _KIND_COLOR[_kind(n)]
        ax.annotate(node_label(n), (x, y), ha='center', va='center', fontsize=8.2,
                    color='#ffffff' if _kind(n) != 'math' else ink, zorder=3,
                    bbox=dict(boxstyle='round,pad=0.32', fc=c if _kind(n) != 'math' else 'none',
                              ec=c, lw=1.0, alpha=0.95))
    ax.set_xlim(min(x for x, _ in pos.values()) - 0.7, max(x for x, _ in pos.values()) + 0.7)
    ax.set_ylim(min(y for _, y in pos.values()) - 0.7, 0.7)
    ax.axis('off')


def _all_nodes(n):
    out = [n]
    for c in n.children:
        out.extend(_all_nodes(c))
    return out


# ---------------------------------------------------------------------------
# Layer 2: behavior
# ---------------------------------------------------------------------------
def _spearman_sample(a, b, step=7):
    """Pooled Spearman over every step-th bar of two [T,N] arrays (speed over ceremony)."""
    a, b = a[::step].ravel(), b[::step].ravel()
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 200:
        return np.nan
    ar = pd.Series(a[m]).rank().to_numpy()
    br = pd.Series(b[m]).rank().to_numpy()
    ar, br = ar - ar.mean(), br - br.mean()
    d = np.sqrt((ar * ar).sum() * (br * br).sum())
    return float((ar * br).sum() / d) if d > 0 else np.nan


def ablation_importance(node, panel, tk, A_orig):
    """Neutralize each feature terminal (per-asset median) and measure how much the
    signal reorders. importance = 1 − spearman(original, ablated); NaN-safe."""
    from evaluator import eval_alpha_panel
    feats = sorted({n.op for n in _all_nodes(node) if n.is_terminal})
    out = {}
    for f in feats:
        try:
            saved = panel[f]
            med = saved.median(axis=0)
            p2 = dict(panel)                               # shallow copy: never mutate the
            p2[f] = pd.DataFrame(                          # GUI's shared, cached panel dict
                np.tile(med.to_numpy(), (len(saved), 1)),
                index=saved.index, columns=saved.columns)
            A2 = eval_alpha_panel(node, p2)[tk].to_numpy(dtype=np.float64)
            rho = _spearman_sample(A_orig, A2)
            out[f] = 1.0 if not np.isfinite(rho) else float(max(0.0, 1.0 - rho))
        except Exception:                                  # noqa: BLE001
            out[f] = np.nan
    return out


def character_stats(A, wl, ret_1d):
    """Holding period, balance and momentum-vs-reversal score from the position path."""
    sgn = np.sign(np.where(np.isfinite(wl), wl, 0.0))
    active = sgn != 0
    long_sh = float((sgn > 0).sum() / max(active.sum(), 1))
    # mean run length of a held side, pooled over assets
    runs = []
    for j in range(sgn.shape[1]):
        s = sgn[:, j]
        n = 0
        for i in range(1, len(s)):
            if s[i] != 0 and s[i] == s[i - 1]:
                n += 1
            elif n:
                runs.append(n + 1); n = 0
        if n:
            runs.append(n + 1)
    hold_bars = float(np.mean(runs)) if runs else 0.0
    mom = _spearman_sample(A, ret_1d)                      # signal vs the PAST day's return
    return {'hold_bars': hold_bars, 'long_share': long_sh,
            'active_share': float(active.mean()), 'mom_score': mom}


def archetype_correlations(r_own, panel, tk, market, vol, exec_rate, ann, ewma, ppd):
    """Correlate the formula's simulated returns with canonical strategy archetypes
    built straight from the panel (windows scaled by bars-per-day)."""
    from fastsim import fast_sim
    close, lr, fund = panel['close'][tk], panel['logret'][tk], panel['funding'][tk]
    d = max(1, int(round(ppd)))
    arch = {
        'momentum (30d)': close.pct_change(30 * d),
        'reversal (1d)': -close.pct_change(d),
        'carry (funding)': -fund.rolling(7 * d, min_periods=d).mean(),
        'low-vol tilt': -lr.rolling(7 * d, min_periods=d).std(),
        'trend z (20d)': ((close - close.rolling(20 * d, min_periods=5 * d).mean())
                          / close.rolling(20 * d, min_periods=5 * d).std()),
    }
    out = {}
    ro = r_own.to_numpy() if hasattr(r_own, 'to_numpy') else np.asarray(r_own)
    for name, sig in arch.items():
        try:
            r = fast_sim(sig.to_numpy(dtype=np.float64), market, vol, exec_rate,
                         ann=ann, ewma_lambda=ewma).to_numpy()
            m = np.isfinite(ro) & np.isfinite(r) & ((ro != 0) | (r != 0))
            out[name] = float(np.corrcoef(ro[m], r[m])[0, 1]) if m.sum() > 200 else np.nan
        except Exception:                                  # noqa: BLE001
            out[name] = np.nan
    return out


# ---------------------------------------------------------------------------
# The figure
# ---------------------------------------------------------------------------
def build_figure(formula, panel, market, tk, vol, exec_rate, ann, ewma, ppd, tf_name,
                 figsize=(13.2, 10.6), dpi=110, facecolor='#171a21', fg='#8a93a2',
                 ink='#d7dce3', accent='#7d8cff'):
    """The whole passport as one Figure (built worker-thread-safe, no pyplot)."""
    from matplotlib.figure import Figure
    from genome import parse
    from evaluator import eval_alpha_panel
    from fastsim import fast_sim_paths

    node = parse(formula)
    A = eval_alpha_panel(node, panel)[tk].to_numpy(dtype=np.float64)
    r_own, wl = fast_sim_paths(A, market, vol, exec_rate, ann=ann, ewma_lambda=ewma)
    idx = market['index']
    ret_1d = panel['close'][tk].pct_change(max(1, int(round(ppd)))).to_numpy(dtype=np.float64)

    imp = ablation_importance(node, panel, tk, A)
    ch = character_stats(A, wl, ret_1d)
    arch = archetype_correlations(r_own, panel, tk, market, vol, exec_rate, ann, ewma, ppd)

    fig = Figure(figsize=figsize, dpi=dpi, facecolor=facecolor)
    gs = fig.add_gridspec(3, 2, height_ratios=[1.35, 1.0, 0.9], width_ratios=[1.12, 0.88],
                          hspace=0.42, wspace=0.34,
                          left=0.055, right=0.975, top=0.94, bottom=0.075)

    # ---- tree ----
    ax_tree = fig.add_subplot(gs[0, 0])
    draw_tree(ax_tree, node, ink=ink)
    ax_tree.set_title('anatomy — the formula as a tree', fontsize=10, color=fg, loc='left')

    # ---- reading steps + character ----
    import textwrap
    ax_txt = fig.add_subplot(gs[0, 1]); ax_txt.axis('off')
    lines = []
    for i, s in enumerate(reading_steps(node)):
        lines += textwrap.wrap(f'{i + 1}. {s}', width=54, subsequent_indent='   ') or ['']
    mom = ch['mom_score']
    nature = ('momentum-leaning' if (np.isfinite(mom) and mom > 0.05) else
              'reversal-leaning' if (np.isfinite(mom) and mom < -0.05) else 'neither (mixed)')
    hold = ch['hold_bars']
    unit = {'1d': 'days', '4h': 'bars (×4h)', '1h': 'hours', '15m': 'bars (×15m)'}.get(tf_name, 'bars')
    lines += ['', 'character —',
              f'  typical hold: ~{hold:.0f} {unit}',
              f'  long / short bars: {ch["long_share"]:.0%} / {1 - ch["long_share"]:.0%}',
              f'  vs yesterday\'s return: {nature}'
              + (f' (ρ {mom:+.2f})' if np.isfinite(mom) else '')]
    ax_txt.text(0.0, 1.0, '\n'.join(lines), transform=ax_txt.transAxes, va='top',
                fontsize=8.0, color=ink, family='monospace', linespacing=1.3)
    ax_txt.set_title('how to read it', fontsize=10, color=fg, loc='left')

    # ---- signal on one asset's price ----
    j = next((k for k, t in enumerate(tk) if 'BTC' in t), 0)
    axp = fig.add_subplot(gs[1, :])
    price = market['C'][:, j]
    axp.plot(idx, price, lw=1.0, color=ink)
    axp.set_yscale('log')
    axp.set_title(f'behavior — its position on {tk[j]} (green long · red short · grey flat)',
                  fontsize=10, color=fg, loc='left')
    axp.grid(alpha=0.25)
    s = np.sign(np.where(np.isfinite(wl[:, j]), wl[:, j], 0.0))
    lo = np.nanmin(price[np.isfinite(price) & (price > 0)])
    band0, band1 = lo * 0.82, lo * 0.94
    axp.fill_between(idx, band0, band1, where=s > 0, color='#2e7d32', alpha=0.85, lw=0)
    axp.fill_between(idx, band0, band1, where=s < 0, color='#c62828', alpha=0.85, lw=0)
    axp.fill_between(idx, band0, band1, where=s == 0, color=fg, alpha=0.25, lw=0)

    # ---- ablation ----
    ax_ab = fig.add_subplot(gs[2, 0])
    items = sorted(imp.items(), key=lambda kv: -(kv[1] if np.isfinite(kv[1]) else -1))
    names = [k for k, _ in items]
    vals = [v if np.isfinite(v) else 0.0 for _, v in items]
    ax_ab.barh(range(len(names))[::-1], vals, color=accent, alpha=0.9)
    ax_ab.set_yticks(range(len(names))[::-1]); ax_ab.set_yticklabels(names, fontsize=8.5)
    ax_ab.set_xlim(0, 1.0); ax_ab.grid(alpha=0.25, axis='x')
    ax_ab.set_title('what feeds it — signal loss when an input is neutralized',
                    fontsize=10, color=fg, loc='left')

    # ---- archetypes ----
    ax_ar = fig.add_subplot(gs[2, 1])
    names = list(arch.keys())
    vals = [arch[n] if np.isfinite(arch[n]) else 0.0 for n in names]
    colors = ['#2e7d32' if v >= 0 else '#c62828' for v in vals]
    ax_ar.barh(range(len(names))[::-1], vals, color=colors, alpha=0.85)
    ax_ar.set_yticks(range(len(names))[::-1]); ax_ar.set_yticklabels(names, fontsize=8.5)
    ax_ar.set_xlim(-1, 1); ax_ar.axvline(0, color=fg, lw=0.8); ax_ar.grid(alpha=0.25, axis='x')
    ax_ar.set_title('return correlation with strategy archetypes', fontsize=10, color=fg,
                    loc='left')

    fig.suptitle(f'FORMULA PASSPORT · {tf_name} · {formula[:96]}', fontsize=10.5,
                 color=ink, x=0.055, ha='left')
    fig.text(0.055, 0.012,
             'An explanation is not evidence: a readable story makes a formula more convincing, '
             'not more profitable. Validity is judged only by held-out TEST and the forward track.',
             fontsize=8.3, color='#f99c00')
    return fig
