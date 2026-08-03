"""Charts: (1) fitness progress by generation; (2) champions' equity with
TRAIN | VAL | TEST zones (in the style of eval_oos.png)."""
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt   # noqa: E402


def plot_history(history, path):
    gens = [h['gen'] for h in history]
    best = [h['best_fit'] for h in history]
    hof = [h['hof_best_base'] for h in history]
    vf = [h['valid_frac'] for h in history]

    fig, ax1 = plt.subplots(figsize=(11, 6))
    ax1.plot(gens, best, '-o', color='#1565c0', lw=2, ms=4, label='best fitness in generation')
    ax1.plot(gens, hof, '-s', color='#c62828', lw=2, ms=4, label='HoF[0] base = min(train,val) Sharpe')
    ax1.set_xlabel('Generation')
    ax1.set_ylabel('Sharpe / fitness')
    ax1.grid(True, alpha=0.3)
    ax2 = ax1.twinx()
    ax2.plot(gens, vf, '--', color='#999', lw=1.2, label='fraction of valid genomes')
    ax2.set_ylabel('valid fraction', color='#999')
    ax2.set_ylim(0, 1)
    l1, la1 = ax1.get_legend_handles_labels()
    l2, la2 = ax2.get_legend_handles_labels()
    ax1.legend(l1 + l2, la1 + la2, loc='lower right', fontsize=9)
    plt.title('Evolution: champion quality improving over generations')
    plt.tight_layout()
    plt.savefig(path, dpi=150, facecolor='white')
    plt.close()


def plot_signal(signals, splits, path, title):
    """signals: dict ticker -> pd.Series of raw alpha. We plot DIRECTION (sign): the engine
    normalizes magnitude anyway, so long/short matters, not the absolute value."""
    plt.figure(figsize=(12, 5))
    ax = plt.gca()
    last = None
    for label, s in signals.items():
        ax.plot(s.index, np.sign(s.values), lw=1.1, label=label, alpha=0.8,
                drawstyle='steps-post')
        last = s.index[-1]
    ax.axhline(0, color='black', lw=0.6)
    va0, te0 = splits['val'][0], splits['test'][0]
    for x in (va0, te0):
        ax.axvline(x, color='black', ls='--', lw=1.0)
    if last is not None:
        ax.axvspan(te0, last, color='grey', alpha=0.08)
    ax.set_yticks([-1, 0, 1])
    ax.set_yticklabels(['SHORT', '—', 'LONG'])
    ax.set_ylim(-1.5, 1.5)
    plt.xlabel('Date')
    plt.title(title + '  (signal direction)')
    plt.legend(loc='upper right', fontsize=9)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=150, facecolor='white')
    plt.close()


def equity_figure(returns, basket, splits, title, figsize=(12, 7), dpi=150,
                  facecolor='white', fg='#555', axline='black', open_pnl=None):
    """Build the equity Figure WITHOUT pyplot and return it — pyplot keeps global state, while a
    plain Figure can be built on any thread and embedded live into Tk (FigureCanvasTkAgg) for
    pan/zoom. plot_equity() below wraps this and saves to a file for the classic callers.

    returns: dict label -> pd.Series of net returns (full period).
    facecolor/fg/axline exist so a caller with its own theme (the GUI in dark mode) can hand in
    colours that stay readable; the defaults are the light look every other caller expects.
    open_pnl (optional pd.Series) adds a lower panel: the unrealized PnL of the positions
    currently held (an episode's gain/loss since entry; a close/flip realizes it away)."""
    from matplotlib.figure import Figure
    fig = Figure(figsize=figsize, dpi=dpi, facecolor=facecolor)
    if open_pnl is None:
        ax = fig.add_subplot(111)
        ax2 = None
    else:
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.07)
        ax = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax)
    colors = ['#c62828', '#1565c0', '#2e7d32', '#6a1b9a', '#ef6c00', '#00838f']
    for i, (label, r) in enumerate(returns.items()):
        eq = (1 + r).cumprod()
        ax.plot(eq.index, eq.values, lw=1.8, color=colors[i % len(colors)], label=label)
    beq = (1 + basket).cumprod()
    ax.plot(beq.index, beq.values, lw=1.6, color='#f9a825', ls=':', label='Basket (EW)')

    tr0, tr1 = splits['train']
    va0, va1 = splits['val']
    te0, te1 = splits['test']
    for x in (va0, te0):
        ax.axvline(x, color=axline, ls='--', lw=1.2)
    ax.axvspan(te0, beq.index[-1], color='grey', alpha=0.10)
    tr = ax.get_xaxis_transform()
    ax.text(tr0 + (va0 - tr0) / 2, 0.02, 'TRAIN\n(evolution)', transform=tr,
            ha='center', va='bottom', fontsize=10, color=fg)
    ax.text(va0 + (te0 - va0) / 2, 0.02, 'VAL\n(robustness)', transform=tr,
            ha='center', va='bottom', fontsize=10, color=fg)
    ax.text(te0 + (beq.index[-1] - te0) / 2, 0.02, 'TEST\n(held-out)', transform=tr,
            ha='center', va='bottom', fontsize=10, color='#c62828', fontweight='bold')

    ax.set_yscale('log')
    ax.set_ylabel('Growth of $1 (log, NET)')
    ax.set_title(title)
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, which='both', alpha=0.3)

    if ax2 is not None:
        op = open_pnl
        ax2.fill_between(op.index, op.values, 0, where=op.values >= 0,
                         color='#2e7d32', alpha=0.30, lw=0)
        ax2.fill_between(op.index, op.values, 0, where=op.values < 0,
                         color='#c62828', alpha=0.30, lw=0)
        ax2.plot(op.index, op.values, lw=0.9, color=fg)
        ax2.axhline(0, color=axline, lw=0.8)
        for x in (va0, te0):
            ax2.axvline(x, color=axline, ls='--', lw=1.0)
        ax2.set_ylabel('open PnL', fontsize=9)
        ax2.grid(True, alpha=0.3)
        ax2.text(0.005, 0.94, 'open PnL — unrealized gain/loss of the positions currently held '
                              '(share of book; a close/flip realizes it away)',
                 transform=ax2.transAxes, fontsize=7.5, color=fg, va='top')
    (ax2 if ax2 is not None else ax).set_xlabel('Date')
    fig.tight_layout()
    return fig


def plot_equity(returns, basket, splits, path, title, figsize=(12, 7), dpi=150,
                facecolor='white', fg='#555', axline='black', open_pnl=None):
    """Classic file-saving wrapper around equity_figure() — same signature as always."""
    fig = equity_figure(returns, basket, splits, title, figsize=figsize, dpi=dpi,
                        facecolor=facecolor, fg=fg, axline=axline, open_pnl=open_pnl)
    fig.savefig(path, dpi=dpi, facecolor=facecolor)
