"""Generator of a self-contained paper-trading bundle from an alpha formula.

build_bundle(...) creates a folder you can carry anywhere and run:
  - copies the engine (5 evolution/ modules + quantpylib/) — with no dependency on the repo;
  - strategy.py       — the formula as a strategy class (via the real engine's run_simulation);
  - paper_trade.py    — a daily paper trader on LIVE Binance data (urllib, no keys);
  - config.json       — formula, universe, target_vol, fee, starting capital;
  - README.md         — how to install/run/schedule via cron + an honest disclaimer;
  - requirements.txt  — numpy, pandas.

Used by the "📄 Paper Trade" button in the node's GUI (alphanode_gui.py).
"""
import os
import json
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
try:                                                     # engine sources: from the bundle or from the repo
    import apppaths
    EVO = apppaths.engine_dir()
    QUANT = apppaths.quant_dir()
except Exception:                                        # noqa: BLE001  (dev fallback)
    EVO = os.path.join(PROJ, 'evolution')
    QUANT = os.path.join(PROJ, 'quantpylib')

ENGINE_MODULES = ['primitives.py', 'genome.py', 'evaluator.py', 'fastsim.py', 'evolved_strategy.py']

REQUIREMENTS = "numpy>=1.24\npandas>=2.0\n"

STRATEGY_PY = '''\
"""An evolutionary alpha as a strategy (via the real quantpylib engine).

    from strategy import Strategy
    a = Strategy(insts=tickers, dfs=dfs, start=..., end=..., portfolio_vol=0.25, execrates=0.001)
    portfolio_df = a.run_simulation()

The formula yields a number per instrument per day: >0 -> long, <0 -> short, |.| -> size.
The engine normalizes, targets volatility, and computes positions on its own. See README.md.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from evolved_strategy import make_evolved

FORMULA = {formula!r}
NAME = {name!r}

Strategy = make_evolved(FORMULA, NAME)
'''

RUNNER_PY = '''\
"""Paper trading of a SINGLE evolutionary alpha on live Binance data (USD-M perpetual).

Each run = one trading step: pull fresh CLOSED daily candles -> run the same simulation
up to today -> take target positions -> mark-to-market -> rebalance -> fees -> log.
The account is stored in paper_state.json, trades in paper_trades.csv. No keys needed (public klines).

    python paper_trade.py            # one step (run once a day after 00:00 UTC)
    python paper_trade.py force      # apply forcibly (without the "bar already processed" check)

This is PAPER trading (execution simulation). Disclaimer and switching to live — in README.md.
"""
import os
import sys
import json
import time
import warnings
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
np.seterr(divide='ignore', invalid='ignore')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from evolved_strategy import make_evolved
from quantpylib.simulator.alpha import Portfolio

CFG = json.load(open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8'))
FORMULA = CFG['formula']
NAME = CFG.get('name', 'PaperAlpha')
TICKERS0 = CFG['tickers']
START = datetime.fromisoformat(CFG.get('start', '2019-09-05'))
VOL = float(CFG.get('portfolio_vol', 0.25))
EXEC = float(CFG.get('exec', 0.001))
START_CAPITAL = float(CFG.get('start_capital', 10000.0))

STATE_FILE = os.path.join(BASE_DIR, 'paper_state.json')
TRADES_LOG = os.path.join(BASE_DIR, 'paper_trades.csv')
DUST = 1.0
KLINES = 'https://fapi.binance.com/fapi/v1/klines'


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fetch_json(url, retries=4):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_klines(symbol, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        url = f'{KLINES}?symbol={symbol}&interval=1d&startTime={cur}&endTime={end_ms}&limit=1500'
        data = fetch_json(url)
        if not data:
            break
        out.extend(data)
        if len(data) < 1500:
            break
        cur = data[-1][0] + 1
        time.sleep(0.1)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=['openTime', 'open', 'high', 'low', 'close', 'volume',
                                    'closeTime', 'qav', 'trades', 'tbb', 'tbq', 'ig'])
    df = df[df['closeTime'] <= now_ms()]                       # CLOSED candles only
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = df[c].astype(float)
    df['datetime'] = pd.to_datetime(df['openTime'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
    return df[~df.index.duplicated()]


def fresh(dfs):
    return {t: df.copy() for t, df in dfs.items()}


def compute_targets(tickers, dfs, end):
    """Run the alpha up to end via the real engine; target weights + leverage from the last row."""
    Alpha = make_evolved(FORMULA, NAME)
    a = Alpha(insts=tickers, dfs=fresh(dfs), start=START, end=end,
              portfolio_vol=VOL, execrates=EXEC)
    stratdf = a.run_simulation()
    pf = Portfolio(insts=tickers, dfs=fresh(dfs), start=START, end=end,
                   stratdfs=[stratdf], portfolio_vol=VOL, execrates=EXEC)
    last = pf.run_simulation().iloc[-1]
    lev = float(last.get('leverage', 0.0))
    weights = {t: float(last.get(f'{t} w', 0.0)) for t in tickers}
    return weights, lev


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        dd = {h['date']: h for h in state.get('history', [])}
        state['history'] = [dd[d] for d in sorted(dd)]
        return state
    return {'equity': START_CAPITAL, 'positions': {}, 'prices': {}, 'last_run': None, 'history': []}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def log_trades(date, trades, prices):
    new = not os.path.exists(TRADES_LOG)
    with open(TRADES_LOG, 'a', encoding='utf-8') as f:
        if new:
            f.write('date,ticker,side,units,notional_usd\\n')
        for t, d in trades.items():
            f.write(f'{date},{t},{"BUY" if d > 0 else "SELL"},{d:.6f},{abs(d*prices[t]):.2f}\\n')


def main():
    force = 'force' in sys.argv
    state = load_state()
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    if not force and state.get('last_run') == expected:
        print(f'New daily bar has not closed yet ({expected} already processed; next — after 00:00 UTC).')
        print(f'  Account: equity ${state["equity"]:,.2f} | positions {len(state["positions"])}')
        return

    print(f'[{NAME}] pulling fresh daily data for {len(TICKERS0)} tickers...')
    dfs, ok = {}, []
    for t in TICKERS0:
        try:
            df = fetch_klines(t, int(pd.Timestamp(START, tz='UTC').timestamp() * 1000), now_ms())
            if len(df) > 60:
                dfs[t] = df
                ok.append(t)
        except Exception as e:                              # noqa: BLE001
            print(f'  {t}: skipped ({type(e).__name__})')
    if not ok:
        print('Could not download any ticker — step skipped.')
        return
    tickers = ok
    last_date = max(df.index[-1] for df in dfs.values())
    prices = {t: float(dfs[t]['close'].iloc[-1]) for t in tickers}
    end = datetime(last_date.year, last_date.month, last_date.day)
    print(f'Last closed day: {end:%Y-%m-%d} | assets: {len(tickers)}')

    if not force and state.get('last_run') == f'{end:%Y-%m-%d}':
        print(f'Bar {end:%Y-%m-%d} already processed. Step skipped.')
        return

    print('Computing target positions (running the alpha up to today)...')
    weights, lev = compute_targets(tickers, dfs, end)

    equity = state['equity']
    positions = state['positions']
    prev_prices = state['prices']

    pnl = sum(positions.get(t, 0.0) * (prices[t] - prev_prices.get(t, prices[t]))
              for t in tickers if t in prices)
    equity += pnl
    target = {t: (weights.get(t, 0.0) * lev * equity / prices[t]) if prices[t] > 0 else 0.0
              for t in tickers}

    trades, turnover = {}, 0.0
    for t in tickers:
        d = target[t] - positions.get(t, 0.0)
        if abs(d * prices[t]) > DUST:
            trades[t] = d
            turnover += abs(d * prices[t])
    cost = turnover * EXEC
    equity -= cost

    positions = {t: target[t] for t in tickers if abs(target[t] * prices[t]) > DUST}
    state.update({'equity': equity, 'positions': positions,
                  'prices': {t: prices[t] for t in tickers}, 'last_run': f'{end:%Y-%m-%d}'})
    entry = {'date': f'{end:%Y-%m-%d}', 'equity': round(equity, 2),
             'pnl': round(pnl, 2), 'leverage': round(lev, 3)}
    if state['history'] and state['history'][-1]['date'] == entry['date']:
        state['history'][-1] = entry
    else:
        state['history'].append(entry)
    save_state(state)
    if trades:
        log_trades(f'{end:%Y-%m-%d}', trades, prices)

    longs = {t: v for t, v in positions.items() if v > 0}
    shorts = {t: v for t, v in positions.items() if v < 0}
    gross = sum(abs(v * prices[t]) for t, v in positions.items())
    ret_tot = equity / START_CAPITAL - 1
    print('\\n' + '=' * 60)
    print(f'  PAPER ACCOUNT [{NAME}] — {end:%Y-%m-%d}')
    print('=' * 60)
    print(f'  Equity            : ${equity:,.2f}   (start ${START_CAPITAL:,.0f}, {ret_tot*100:+.1f}%)')
    print(f'  P&L since last run: ${pnl:+,.2f}')
    print(f'  Step fees         : ${cost:,.2f}   (turnover ${turnover:,.0f})')
    print(f'  Leverage (target) : {lev:.2f}   gross exposure ${gross:,.0f}')
    print(f'  Positions         : {len(longs)} long / {len(shorts)} short')
    for t, u in sorted(positions.items(), key=lambda kv: -abs(kv[1] * prices[kv[0]]))[:6]:
        print(f'      {t:12s} {"LONG " if u > 0 else "SHORT"} ${abs(u*prices[t]):>9,.0f}')
    print('=' * 60)
    print(f'State: {STATE_FILE}\\nTrade log: {TRADES_LOG}')


if __name__ == '__main__':
    main()
'''


def _readme(formula, name, tickers, vol, exec_rate, meta, start_capital):
    def sh(seg):
        v = (meta or {}).get(seg, {}).get('sharpe') if meta else None
        return f'{v:+.2f}' if v is not None else '—'
    folder = f'paper_{name}'
    return f'''# Paper-trading bundle — `{name}`

Self-contained paper trading of one evolutionary alpha on **live Binance data**
(USD-M perpetual, public klines — API keys are NOT needed).

## Formula
```
{formula}
```
Gives a number per instrument per day: **>0 → long, <0 → short**, absolute value → size. Then the
(`quantpylib`) engine normalizes, targets volatility ({vol:g}), and computes positions on its own.

Metrics from the search (a hypothetical backtest): TRAIN Sharpe {sh('train')} · VAL {sh('val')} · **TEST {sh('test')}**.

## ⚠️ Honest disclaimer
- This is a **hypothetical backtest**, not investment advice. The past ≠ the future.
- The alpha was selected from a large search (overfitting risk) on surviving coins (survivorship).
- **Paper-forward first.** Run this bundle for weeks/months on NEW data the search has not
  seen. Only if the forward holds up — consider real money, and even then in small size.
- Live execution is NOT included here (keys, position limits, a kill-switch are needed — do it deliberately).

## Installation
```bash
pip install -r requirements.txt        # numpy, pandas
```

## Running (once a day)
```bash
python paper_trade.py                   # one step; after 00:00 UTC (daily candle has closed)
python paper_trade.py force             # apply forcibly
```
The first run opens the account (starting capital ${start_capital:,.0f}), then each step:
mark-to-market → rebalance to targets → fees ({exec_rate:g}) → log.

### Automatically via cron (Linux/mac)
```cron
5 0 * * *  cd /path/to/{folder} && /usr/bin/python3 paper_trade.py >> paper.log 2>&1
```
(at 00:05 UTC every day). On Windows — Task Scheduler with the same command.

## Output
- `paper_state.json` — the account: equity, positions, per-day history (for the chart).
- `paper_trades.csv` — trade journal (date, ticker, side, units, notional).

## Bundle contents
- `strategy.py` — the formula as a strategy class (for import/analysis).
- `paper_trade.py` — the daily trader (the file you run).
- `config.json` — formula, universe ({len(tickers)} pairs), target_vol, fee, starting capital.
- `quantpylib/`, `primitives.py`, `genome.py`, `evaluator.py`, `fastsim.py`, `evolved_strategy.py` — the engine.
'''


def build_bundle(formula, name, tickers, vol, exec_rate, start, out_root,
                 start_capital=10000.0, meta=None):
    """Build a self-contained bundle in out_root/paper_<name>/. Return the folder path."""
    dest = os.path.join(out_root, f'paper_{name}')
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    for mod in ENGINE_MODULES:                            # formula engine
        shutil.copy2(os.path.join(EVO, mod), os.path.join(dest, mod))
    shutil.copytree(QUANT, os.path.join(dest, 'quantpylib'))

    cfg = {'formula': formula, 'name': name, 'tickers': list(tickers),
           'start': start, 'portfolio_vol': float(vol), 'exec': float(exec_rate),
           'start_capital': float(start_capital)}
    with open(os.path.join(dest, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    with open(os.path.join(dest, 'strategy.py'), 'w', encoding='utf-8') as f:
        f.write(STRATEGY_PY.format(formula=formula, name=name))
    with open(os.path.join(dest, 'paper_trade.py'), 'w', encoding='utf-8') as f:
        f.write(RUNNER_PY)
    with open(os.path.join(dest, 'requirements.txt'), 'w', encoding='utf-8') as f:
        f.write(REQUIREMENTS)
    with open(os.path.join(dest, 'README.md'), 'w', encoding='utf-8') as f:
        f.write(_readme(formula, name, tickers, vol, exec_rate, meta, start_capital))
    return dest
