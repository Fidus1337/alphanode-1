"""Alpha / portfolio analytics dashboard rendered to a multi-page A4 PDF.

Pure computation + drawing module: no Tk, no pyplot. Uses matplotlib's object API
(`Figure` + `PdfPages`) so it is safe to call from a background thread while the
GUI keeps running — pyplot's global state machine is never touched.

The layout mirrors the HTML diagnostics dashboard the strategy signals were
audited with (KPI tiles, exposure regime, long/short balance, turnover,
concentration, weight structure, per-segment table, conclusions) and adds the
profitability blocks (equity, drawdown, monthly returns, attribution) that a
weights-only file cannot provide.

Inputs are plain pandas objects, so the module serves both callers:
  - a single alpha: target weights over the whole history + NET returns from
    the fast engine (TRAIN | VAL | TEST);
  - the combined portfolio: weights/equity over TEST from portfolio.json.
"""
import textwrap

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch

ANN = 365                     # bars/year; crypto trades 24/7 (evaluator.ANN). build_report(ann=...)
                              # overrides it for intraday timeframes (bars, not days).

# palette of the reference dashboard (light: a PDF is a print artefact)
INK = '#14181F'
MUT = '#6B7280'
ACC = '#2D3A6B'
LONG = '#1F8A5B'
SHORT = '#C43D3D'
AMBER = '#B6802A'
HAIR = '#E3E6EB'
SEG_COLORS = {'train': '#3E6E9E', 'val': '#8A5FA0', 'test': '#3E8E5E'}
PAGE = (8.27, 11.69)          # A4 portrait, inches


# ---------------------------------------------------------------- metrics ----
def weight_stats(wide):
    """Per-day series + scalar stats of a target-weight table (index=date, cols=tickers)."""
    w = wide.fillna(0.0)
    w = w[w.abs().sum(axis=1) > 0]                       # pre-listing / empty days carry no info
    aw = w.abs()
    gross = aw.sum(axis=1)
    net = w.sum(axis=1)
    long_g = w.clip(lower=0).sum(axis=1)
    short_g = (-w.clip(upper=0)).sum(axis=1)
    n_active = (aw > 1e-6).sum(axis=1)
    # effective number of positions: 1 / HHI of gross-normalized weights
    frac = aw.div(gross.replace(0, np.nan), axis=0)
    eff_n = 1.0 / (frac ** 2).sum(axis=1).replace(0, np.nan)
    turn = 0.5 * (w - w.shift(1)).abs().sum(axis=1)      # one-sided turnover
    turn.iloc[0] = np.nan                                # no previous day
    flat = w.to_numpy().ravel()
    prev = w.shift(1).to_numpy().ravel()
    m = np.isfinite(flat) & np.isfinite(prev)
    autocorr = np.nan
    if m.sum() > 10 and np.std(flat[m]) > 0 and np.std(prev[m]) > 0:
        autocorr = float(np.corrcoef(flat[m], prev[m])[0, 1])
    return {
        'wide': w, 'gross': gross, 'net': net, 'long': long_g, 'short': short_g,
        'n_active': n_active, 'eff_n': eff_n, 'turnover': turn, 'autocorr': autocorr,
        'days': len(w),
        'net_mean': float(net.mean()), 'net_std': float(net.std()),
        'net_long_share': float((net > 0).mean()),
        'gross_mean': float(gross.mean()), 'gross_std': float(gross.std()),
        'turn_mean': float(turn.mean()) if turn.notna().any() else np.nan,
        'eff_n_mean': float(eff_n.mean()),
        'n_active_mean': float(n_active.mean()), 'n_active_max': int(n_active.max()),
    }


def return_stats(r):
    """Sharpe / CAGR / MaxDD / win% of a daily-returns series, engine conventions (ANN=365)."""
    r = r.dropna()
    if len(r) < 5:
        return None
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    sd = float(r.std())
    return {
        'sharpe': float(r.mean() / sd * np.sqrt(ANN)) if sd > 0 else np.nan,
        'cagr': float(eq.iloc[-1] ** (ANN / len(r)) - 1.0),
        'dd': float(dd.min()),
        'win': float((r > 0).mean()),
        'n': len(r),
        'equity': eq, 'ddown': dd,
    }


def _segments(index, splits):
    """[(key, label, mask)] for the segments that actually overlap the index."""
    out = []
    if not splits:
        return out
    for key, label in (('train', 'TRAIN'), ('val', 'VAL'), ('test', 'TEST')):
        if key not in splits:
            continue
        a, b = splits[key]
        a = pd.Timestamp(a).tz_localize(None)
        b = pd.Timestamp(b).tz_localize(None)
        idx = index.tz_localize(None) if index.tz is not None else index
        mask = pd.Series((idx >= a) & (idx < b), index=index)
        if mask.any():
            out.append((key, label, mask))
    return out


# ---------------------------------------------------------------- drawing ----
def _shade_segments(ax, index, splits, label_y=0.02):
    for key, label, mask in _segments(index, splits):
        sub = index[mask.to_numpy()]
        if len(sub) == 0:
            continue
        ax.axvspan(sub[0], sub[-1], color=SEG_COLORS[key], alpha=0.05, zorder=0)
        ax.annotate(label, xy=(sub[0] + (sub[-1] - sub[0]) / 2, label_y),
                    xycoords=('data', 'axes fraction'), ha='center', va='bottom',
                    fontsize=6.5, color=SEG_COLORS[key], alpha=0.9)


def _style(ax, title=None):
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    for s in ('left', 'bottom'):
        ax.spines[s].set_color(HAIR)
    ax.tick_params(colors=MUT, labelsize=6.5)
    ax.grid(True, color=HAIR, lw=0.5, alpha=0.7)
    if title:
        ax.set_title(title, fontsize=8.5, color=INK, loc='left', pad=6, fontweight='bold')


def _tile(ax, label, value, caption, color=ACC):
    ax.set_axis_off()
    ax.add_patch(FancyBboxPatch(
        (0.01, 0.04), 0.98, 0.92, boxstyle='round,pad=0.008,rounding_size=0.03',
        transform=ax.transAxes, facecolor='white', edgecolor=HAIR, lw=0.8))
    ax.text(0.09, 0.74, label.upper(), fontsize=5.6, color=MUT, transform=ax.transAxes,
            fontfamily='DejaVu Sans')
    ax.text(0.09, 0.36, value, fontsize=11.5, color=color, transform=ax.transAxes,
            fontweight='bold')
    ax.text(0.09, 0.12, caption, fontsize=5.4, color=MUT, transform=ax.transAxes)


def _header(fig, title, subtitle, meta_line):
    fig.text(0.06, 0.965, title, fontsize=15, color=INK, fontweight='bold')
    if subtitle:
        fig.text(0.06, 0.945, subtitle[:110], fontsize=7.5, color=MUT, fontfamily='DejaVu Sans Mono')
    fig.text(0.06, 0.929, meta_line, fontsize=7, color=ACC)
    fig.add_artist(Line2D(
        [0.06, 0.94], [0.922, 0.922], color=HAIR, lw=0.8, transform=fig.transFigure))


def _footer(fig, page, npages, stamp):
    fig.text(0.06, 0.022, stamp, fontsize=6, color=MUT)
    fig.text(0.94, 0.022, f'{page} / {npages}', fontsize=6, color=MUT, ha='right')


def _fmt_pct(x, digits=1, sign=False):
    if x is None or not np.isfinite(x):
        return '—'
    s = '+' if sign else ''
    return f'{x * 100:{s}.{digits}f}%'


# ------------------------------------------------------------------ pages ----
def _page_returns(pdf, title, subtitle, meta_line, stamp, npages, ws, rs, basket_rs, splits,
                  seg_metrics):
    fig = Figure(figsize=PAGE, dpi=150)
    _header(fig, title, subtitle, meta_line)

    idx = ws['wide'].index
    years = (idx[-1] - idx[0]).days / 365.25 if len(idx) > 1 else 0
    row1 = [
        ('Sharpe (TEST)', f"{(seg_metrics.get('test') or {}).get('sharpe', np.nan):+.2f}"
         if np.isfinite((seg_metrics.get('test') or {}).get('sharpe', np.nan)) else '—',
         'held-out', ACC),
        ('CAGR (TEST)', _fmt_pct((seg_metrics.get('test') or {}).get('cagr', np.nan), 0, True),
         'annualized, NET', LONG if (seg_metrics.get('test') or {}).get('cagr', 0) >= 0 else SHORT),
        ('MaxDD (TEST)', _fmt_pct((seg_metrics.get('test') or {}).get('dd', np.nan), 0),
         'drawdown', SHORT),
        ('Win% (TEST)', _fmt_pct((seg_metrics.get('test') or {}).get('win', np.nan), 0),
         'share of winning days', ACC),
    ]
    hold = (ws['gross_mean'] / ws['turn_mean']) if ws['turn_mean'] and np.isfinite(ws['turn_mean']) \
        and ws['turn_mean'] > 0 else np.nan
    row2 = [
        ('Horizon', f'{years:.1f} yr', f"{ws['days']} trading days", ACC),
        ('Net exposure', _fmt_pct(ws['net_mean'], 1, True),
         f"σ {_fmt_pct(ws['net_std'], 1)}, long {_fmt_pct(ws['net_long_share'], 0)} of days",
         LONG if ws['net_mean'] >= 0 else SHORT),
        ('Turnover / yr', f"{(ws['turn_mean'] or 0) * ANN:.0f}×",
         f"{_fmt_pct(ws['turn_mean'], 1)}/day · hold ≈{hold:.1f} d"
         if np.isfinite(hold) else 'one-sided', AMBER),
        ('Eff. N', f"{ws['eff_n_mean']:.1f}",
         f"1/HHI · universe ⌀{ws['n_active_mean']:.0f} (peak {ws['n_active_max']})", ACC),
    ]
    for i, args in enumerate(row1):
        _tile(fig.add_axes([0.055 + i * 0.2225, 0.845, 0.215, 0.062]), *args)
    for i, args in enumerate(row2):
        _tile(fig.add_axes([0.055 + i * 0.2225, 0.775, 0.215, 0.062]), *args)

    # equity, log scale, vs basket
    ax = fig.add_axes([0.08, 0.44, 0.86, 0.30])
    _style(ax, 'Growth of $1 (NET, log) — strategy vs buy & hold (EW basket)')
    if rs is not None:
        ax.plot(rs['equity'].index, rs['equity'].values, color=SHORT, lw=1.1, label='strategy')
    if basket_rs is not None:
        ax.plot(basket_rs['equity'].index, basket_rs['equity'].values, color=AMBER, lw=0.8,
                ls=':', label='basket (EW)')
    ax.set_yscale('log')
    if rs is not None or basket_rs is not None:          # else: no labeled artists -> mpl warns
        ax.legend(fontsize=6.5, loc='upper left', frameon=False)
    if rs is not None:
        _shade_segments(ax, rs['equity'].index, splits)

    # drawdown
    ax2 = fig.add_axes([0.08, 0.255, 0.86, 0.145])
    _style(ax2, 'Strategy drawdown')
    if rs is not None:
        ax2.fill_between(rs['ddown'].index, rs['ddown'].values * 100, 0,
                         color=SHORT, alpha=0.35, lw=0)
        ax2.plot(rs['ddown'].index, rs['ddown'].values * 100, color=SHORT, lw=0.7)
        _shade_segments(ax2, rs['ddown'].index, splits)
    ax2.set_ylabel('%', fontsize=6.5, color=MUT)

    # per-segment metric lines
    y = 0.205
    fig.text(0.08, y, f'Per-segment metrics (NET, engine, ANN={ANN:g}):', fontsize=8,
             color=INK, fontweight='bold')
    y -= 0.022
    for key, label in (('train', 'TRAIN'), ('val', 'VAL'), ('test', 'TEST (held-out)')):
        m = seg_metrics.get(key)
        if not m:
            continue
        txt = (f"{label:<16}  Sharpe {m.get('sharpe', np.nan):+.2f}   "
               f"CAGR {_fmt_pct(m.get('cagr'), 0, True):>6}   "
               f"MaxDD {_fmt_pct(m.get('dd'), 0):>5}   "
               f"win {_fmt_pct(m.get('win'), 0):>4}   days {m.get('n', '—')}")
        fig.text(0.08, y, txt, fontsize=7.5, fontfamily='DejaVu Sans Mono',
                 color=SEG_COLORS.get(key, INK))
        y -= 0.019
    _footer(fig, 1, npages, stamp)
    pdf.savefig(fig)


def _page_exposure(pdf, title, stamp, npages, ws, splits):
    fig = Figure(figsize=PAGE, dpi=150)
    _header(fig, title, 'Exposure, long/short balance, turnover', '')
    idx = ws['wide'].index
    sm = max(1, min(20, ws['days'] // 10))

    ax = fig.add_axes([0.08, 0.70, 0.86, 0.19])
    _style(ax, f'Net exposure ({sm}-day smoothing) — share of gross capital')
    net_s = ws['net'].rolling(sm, min_periods=1).mean() * 100
    ax.fill_between(idx, net_s.clip(lower=0), 0, color=LONG, alpha=0.45, lw=0)
    ax.fill_between(idx, net_s.clip(upper=0), 0, color=SHORT, alpha=0.45, lw=0)
    ax.plot(idx, net_s, color=INK, lw=0.6)
    ax.axhline(0, color=MUT, lw=0.6)
    _shade_segments(ax, idx, splits)
    ax.set_ylabel('%', fontsize=6.5, color=MUT)

    ax2 = fig.add_axes([0.08, 0.475, 0.86, 0.17])
    _style(ax2, 'Capital decomposition: long (up) and short (down)')
    lg = ws['long'].rolling(sm, min_periods=1).mean() * 100
    sh = ws['short'].rolling(sm, min_periods=1).mean() * 100
    ax2.fill_between(idx, lg, 0, color=LONG, alpha=0.5, lw=0, label='long book')
    ax2.fill_between(idx, -sh, 0, color=SHORT, alpha=0.5, lw=0, label='short book')
    ax2.axhline(0, color=MUT, lw=0.6)
    ax2.legend(fontsize=6.5, loc='upper left', frameon=False)
    _shade_segments(ax2, idx, splits)
    ax2.set_ylabel('% gross', fontsize=6.5, color=MUT)

    ax3 = fig.add_axes([0.08, 0.25, 0.86, 0.17])
    _style(ax3, f'Turnover (one-sided, % of capital per day; dashed — mean '
                f'{_fmt_pct(ws["turn_mean"], 1)})')
    ax3.plot(idx, ws['turnover'] * 100, color=AMBER, lw=0.4, alpha=0.5)
    ax3.plot(idx, ws['turnover'].rolling(sm, min_periods=1).mean() * 100, color=AMBER, lw=1.0)
    if np.isfinite(ws['turn_mean']):
        ax3.axhline(ws['turn_mean'] * 100, color=MUT, lw=0.6, ls='--')
    _shade_segments(ax3, idx, splits)
    ax3.set_ylabel('%', fontsize=6.5, color=MUT)

    ax4 = fig.add_axes([0.08, 0.065, 0.86, 0.13])
    _style(ax4, 'Universe size (active positions) and effective N (1/HHI)')
    ax4.plot(idx, ws['n_active'], color=ACC, lw=0.8, label='active tickers')
    ax4.plot(idx, ws['eff_n'], color=LONG, lw=0.8, label='eff. N')
    ax4.legend(fontsize=6.5, loc='upper left', frameon=False)
    _shade_segments(ax4, idx, splits)
    _footer(fig, 2, npages, stamp)
    pdf.savefig(fig)


def _page_weights(pdf, title, stamp, npages, ws, rets_monthly):
    fig = Figure(figsize=PAGE, dpi=150)
    _header(fig, title, 'Weight structure and return calendar', '')
    w = ws['wide']

    ax = fig.add_axes([0.08, 0.70, 0.40, 0.19])
    vals = w.to_numpy().ravel()
    vals = vals[np.abs(vals) > 1e-6] * 100
    p99 = np.percentile(np.abs(vals), 99) if len(vals) else np.nan
    # p99 goes in the title, not an x-label — an x-label here collides with the ax3 title below
    _style(ax, 'Weight distribution, % (log y)'
           + (f'  ·  p99 |w| {p99:.0f}%' if np.isfinite(p99) else ''))
    if len(vals):
        ax.hist(vals, bins=80, color=ACC, alpha=0.8)
        ax.set_yscale('log')
        ax.axvline(p99, color=AMBER, lw=0.8, ls='--')
        ax.axvline(-p99, color=AMBER, lw=0.8, ls='--')

    ax2 = fig.add_axes([0.56, 0.575, 0.38, 0.315])
    _style(ax2, 'Directional tilt by asset (⌀ weight)')
    mean_w = w.mean().sort_values()
    show = pd.concat([mean_w.head(10), mean_w.tail(10)]) * 100
    colors = [SHORT if v < 0 else LONG for v in show.values]
    ax2.barh(range(len(show)), show.values, color=colors, alpha=0.8)
    ax2.set_yticks(range(len(show)))
    ax2.set_yticklabels([t.replace('USDT', '') for t in show.index], fontsize=5.5)
    ax2.axvline(0, color=MUT, lw=0.6)
    ax2.set_xlabel('⌀ weight, %', fontsize=6.5, color=MUT)

    ax3 = fig.add_axes([0.08, 0.575, 0.40, 0.09])
    _style(ax3, 'Gross stability (Σ|w|)')
    ax3.plot(w.index, ws['gross'], color=ACC, lw=0.7)
    ax3.set_ylim(0, max(1.5, float(ws['gross'].max()) * 1.1))

    # monthly compound returns heatmap
    ax4 = fig.add_axes([0.08, 0.10, 0.86, 0.40])
    if rets_monthly is not None and len(rets_monthly):
        piv = rets_monthly * 100
        vmax = max(1e-6, np.nanmax(np.abs(piv.values)))
        norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
        ax4.imshow(piv.values, cmap='RdYlGn', norm=norm, aspect='auto')
        ax4.set_xticks(range(piv.shape[1]))
        ax4.set_xticklabels(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][:piv.shape[1]],
                            fontsize=6)
        ax4.set_yticks(range(piv.shape[0]))
        ax4.set_yticklabels(piv.index, fontsize=6.5)
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                v = piv.values[i, j]
                if np.isfinite(v):
                    ax4.text(j, i, f'{v:+.0f}', ha='center', va='center', fontsize=5.5,
                             color=INK if abs(v) < vmax * 0.7 else 'white')
        ax4.set_title('Strategy monthly returns, % (NET)', fontsize=8.5, color=INK,
                      loc='left', pad=6, fontweight='bold')
        ax4.tick_params(colors=MUT, length=0)
        for s in ax4.spines.values():
            s.set_visible(False)
    else:
        ax4.set_axis_off()
        ax4.text(0.5, 0.5, 'no return data', ha='center', color=MUT, fontsize=9)
    _footer(fig, 3, npages, stamp)
    pdf.savefig(fig)


def _page_segments(pdf, title, stamp, npages, ws, rs_full, splits, seg_metrics, contrib,
                   exec_cost):
    fig = Figure(figsize=PAGE, dpi=150)
    _header(fig, title, 'Segments, attribution, conclusions', '')
    w = ws['wide']
    segs = _segments(w.index, splits)

    # per-segment construction table
    ax = fig.add_axes([0.06, 0.66, 0.88, 0.23])
    ax.set_axis_off()
    cols = ['Segment', 'Days', '⌀ active', '⌀ net', 'net-long days', '⌀ turnover',
            'turnover/yr', '⌀ eff.N', 'Sharpe', 'CAGR', 'MaxDD']
    rows = []
    row_colors = []
    for key, label, mask in (segs or [('all', 'ALL', pd.Series(True, index=w.index))]):
        sub = w[mask.to_numpy()]
        if not len(sub):
            continue
        s = weight_stats(sub)
        m = seg_metrics.get(key) or {}
        rows.append([label, f"{s['days']}", f"{s['n_active_mean']:.0f}",
                     _fmt_pct(s['net_mean'], 1, True), _fmt_pct(s['net_long_share'], 0),
                     _fmt_pct(s['turn_mean'], 1), f"{(s['turn_mean'] or 0) * ANN:.0f}×",
                     f"{s['eff_n_mean']:.1f}",
                     f"{m['sharpe']:+.2f}" if np.isfinite(m.get('sharpe', np.nan)) else '—',
                     _fmt_pct(m.get('cagr'), 0, True), _fmt_pct(m.get('dd'), 0)])
        row_colors.append(SEG_COLORS.get(key, INK))
    if rows:
        table = ax.table(cellText=rows, colLabels=cols, loc='upper center', cellLoc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(6.3)
        table.scale(1, 1.55)
        for (r, c), cell in table.get_celld().items():
            cell.set_edgecolor(HAIR)
            if r == 0:
                cell.set_text_props(color=MUT, fontsize=5.4)
                cell.set_facecolor('#F4F5F7')
            elif c == 0:
                cell.set_text_props(color=row_colors[r - 1], fontweight='bold')
        ax.set_title('Per-segment breakdown', fontsize=8.5, color=INK, loc='left',
                     pad=4, fontweight='bold')

    # approximate per-asset PnL attribution
    ax2 = fig.add_axes([0.10, 0.365, 0.84, 0.26])
    if contrib is not None and len(contrib):
        _style(ax2, 'PnL attribution by asset (target weights, BEFORE costs — approximation)')
        show = pd.concat([contrib.head(8), contrib.tail(8)]) * 100
        show = show[~show.index.duplicated()]
        colors = [SHORT if v < 0 else LONG for v in show.values]
        ax2.barh(range(len(show)), show.values, color=colors, alpha=0.85)
        ax2.set_yticks(range(len(show)))
        ax2.set_yticklabels([t.replace('USDT', '') for t in show.index], fontsize=5.8)
        ax2.axvline(0, color=MUT, lw=0.6)
        ax2.set_xlabel('total contribution, pp', fontsize=6.5, color=MUT)
    else:
        ax2.set_axis_off()

    # auto conclusions — the thresholds mirror the reference dashboard's "Conclusions"
    bullets = []
    if ws['gross_std'] < 0.02 and abs(ws['gross_mean'] - 1.0) < 0.05:
        bullets.append('Proper gross-normalized long/short: Σ|w|=1 holds steadily '
                       f"(σ={ws['gross_std']:.4f}) — the portfolio carries no leverage leaks.")
    else:
        bullets.append(f"Gross is not constant: ⌀{ws['gross_mean']:.2f}, σ={ws['gross_std']:.2f} — "
                       'the leverage contribution should be separated from the cross-sectional edge.')
    tilt = ws['net_mean']
    if abs(tilt) > 0.1:
        bullets.append(f"Structural {'long' if tilt > 0 else 'short'} tilt: ⌀ net "
                       f"{_fmt_pct(tilt, 1, True)}, net-long on {_fmt_pct(ws['net_long_share'], 0)} "
                       'of days. Not a dollar-neutral strategy — the tilt contribution should be '
                       'benchmarked against BTC-only.')
    else:
        bullets.append(f"Close to market-neutral: ⌀ net {_fmt_pct(tilt, 1, True)}.")
    if np.isfinite(ws['turn_mean']):
        drag = ws['turn_mean'] * ANN * 2 * exec_cost
        bullets.append(f"Turnover {_fmt_pct(ws['turn_mean'], 1)}/day (~{ws['turn_mean'] * ANN:.0f}× "
                       f"per year): at {exec_cost * 1e4:.0f} bp fees the cost drag is ≈ "
                       f"{_fmt_pct(drag, 1)} annualized — already included in the engine's NET returns.")
    conc_days = int((ws['eff_n'] < 1.5).sum())
    if conc_days > 0:
        bullets.append(f"On {conc_days} days the book is effectively a single asset (eff. N < 1.5) — "
                       'averaged metrics over that period are biased.')
    if np.isfinite(ws['autocorr']):
        hold = ws['gross_mean'] / ws['turn_mean'] if ws['turn_mean'] else np.nan
        bullets.append(f"Weight persistence: autocorrelation {ws['autocorr']:.2f}, "
                       f"holding ≈ {hold:.1f} d — the signal is not jittery." if np.isfinite(hold)
                       else f"Weight autocorrelation {ws['autocorr']:.2f}.")
    tr, te = seg_metrics.get('train') or {}, seg_metrics.get('test') or {}
    if np.isfinite(tr.get('sharpe', np.nan)) and np.isfinite(te.get('sharpe', np.nan)):
        if te['sharpe'] < 0.5 * tr['sharpe']:
            bullets.append(f"Sharpe degrades out-of-sample: {tr['sharpe']:+.2f} (TRAIN) → "
                           f"{te['sharpe']:+.2f} (TEST) — a sign of overfitting; a forward test "
                           'is mandatory.')
        else:
            bullets.append(f"Sharpe holds out-of-sample: {tr['sharpe']:+.2f} (TRAIN) → "
                           f"{te['sharpe']:+.2f} (TEST).")
    bullets.append('All numbers are a hypothetical backtest; TEST is a one-shot estimate, '
                   'not a guarantee. The final check is a forward/paper run on new data.')

    y = 0.315
    fig.text(0.06, y, 'Conclusions', fontsize=10, color=INK, fontweight='bold')
    y -= 0.02
    for b in bullets:
        for li, line in enumerate(textwrap.wrap(b, 118)):
            fig.text(0.075 if li else 0.065, y, ('' if li else '• ') + line,
                     fontsize=6.8, color=INK)
            y -= 0.0135
        y -= 0.006
    _footer(fig, 4, npages, stamp)
    pdf.savefig(fig)


# ------------------------------------------------------------------ entry ----
def build_report(out_pdf, *, title, subtitle, wide, rets=None, basket=None, splits=None,
                 seg_metrics=None, asset_rets=None, exec_cost=0.001, stamp='', ann=None):
    """Render the 4-page dashboard.

    wide       : DataFrame of target weights (index=dates, cols=tickers)
    rets       : Series of per-bar NET strategy returns (optional but expected)
    basket     : Series of per-bar buy&hold basket returns (optional)
    splits     : {'train': (a, b), 'val': ..., 'test': ...} — omit for TEST-only (portfolio)
    seg_metrics: {'train': {'sharpe','cagr','dd',['win','n']}, ...} — champion numbers if known;
                 anything missing is computed from `rets`
    asset_rets : DataFrame of per-asset per-bar returns for the attribution page (optional)
    ann        : bars per year (365 daily; intraday timeframes pass theirs)
    Returns a small summary dict for the caller's dialog.
    """
    global ANN
    if ann:                       # the worker is a fresh process per report — module state is fine
        ANN = float(ann)
    ws = weight_stats(wide)
    idx = ws['wide'].index

    def _naive(s):
        if s is None:
            return None
        s = s.copy()
        if getattr(s.index, 'tz', None) is not None:
            s.index = s.index.tz_localize(None)
        return s

    if getattr(idx, 'tz', None) is not None:
        ws = {**ws}
        for k in ('wide', 'gross', 'net', 'long', 'short', 'n_active', 'eff_n', 'turnover'):
            ws[k] = _naive(ws[k])
        idx = ws['wide'].index
    rets, basket = _naive(rets), _naive(basket)
    asset_rets = _naive(asset_rets)

    rs = return_stats(rets) if rets is not None else None
    basket_rs = return_stats(basket) if basket is not None else None

    # fill per-segment metrics from returns where the champion record has none
    seg_metrics = dict(seg_metrics or {})
    for key, label, mask in _segments(idx, splits) or []:
        have = seg_metrics.get(key) or {}
        if rets is not None:
            seg_r = rets[rets.index.isin(idx[mask.to_numpy()])]
            calc = return_stats(seg_r)
            if calc:
                merged = {k: calc[k] for k in ('sharpe', 'cagr', 'dd', 'win', 'n')}
                merged.update({k: v for k, v in have.items() if v is not None})
                seg_metrics[key] = merged
    if not seg_metrics and rs is not None:                # portfolio: everything is TEST
        seg_metrics = {'test': {k: rs[k] for k in ('sharpe', 'cagr', 'dd', 'win', 'n')}}

    monthly = None
    if rets is not None and len(rets.dropna()) > 20:
        r = rets.dropna()
        comp = (1 + r).groupby([r.index.year, r.index.month]).prod() - 1
        monthly = comp.unstack(fill_value=np.nan).reindex(columns=range(1, 13))
        monthly.index = [str(y) for y in monthly.index]

    contrib = None
    if asset_rets is not None:
        ar = asset_rets.reindex(index=ws['wide'].index, columns=ws['wide'].columns)
        contrib = (ws['wide'].shift(1) * ar).sum(axis=0, skipna=True).sort_values()
        contrib = contrib[contrib.abs() > 1e-9]

    meta_line = (f"{idx[0].date()} — {idx[-1].date()}  ·  {ws['days']} days  ·  "
                 f"universe ⌀{ws['n_active_mean']:.0f} tickers  ·  gross ⌀{ws['gross_mean']:.2f}×")
    npages = 4
    with PdfPages(out_pdf) as pdf:
        _page_returns(pdf, title, subtitle, meta_line, stamp, npages, ws, rs, basket_rs,
                      splits, seg_metrics)
        _page_exposure(pdf, title, stamp, npages, ws, splits)
        _page_weights(pdf, title, stamp, npages, ws, monthly)
        _page_segments(pdf, title, stamp, npages, ws, rs, splits, seg_metrics, contrib,
                       exec_cost)
        d = pdf.infodict()
        d['Title'] = title
        d['Creator'] = 'AlphaNode'
    return {'pages': npages, 'days': ws['days'], 'path': out_pdf}
