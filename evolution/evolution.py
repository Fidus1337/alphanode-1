"""Evolutionary driver: a population of trees -> selection -> crossover/mutation -> champions.

Against overfitting (the main risk of searching through millions of formulas):
  * fitness = min(train_Sharpe, val_Sharpe) — a strategy is "good" only as much as
    its WORST training segment is (rewarding robustness, not curve-fitting);
  * a complexity penalty (parsimony) — simple beats complex;
  * a penalty/dedup for correlation with already-found champions (a diverse Hall of Fame);
  * the TEST segment is HELD-OUT: it plays no part in fitness, selection or seeding — its
    metrics are carried along for reporting only and are first LOOKED AT after the run.
"""
import multiprocessing as mp
import random

import numpy as np

from genome import Node, random_tree, crossover, mutate, parse   # noqa: F401
from evaluator import build_panel, make_market, evaluate


# ---------------- parallel evaluation ----------------
_G = {}


def _winit(data, start, end, splits, vol, exec_rate, instruments, freq, vol_window, ann, ewma_lambda):
    tk, raw, panel = build_panel(data, start, end, instruments, freq=freq)
    _G.update(tk=tk, panel=panel, market=make_market(panel, tk, raw, vol_window=vol_window),
              splits=splits, vol=vol, exec=exec_rate, ann=ann, ewma_lambda=ewma_lambda)


def _weval(node):
    return evaluate(node, _G['tk'], _G['panel'], _G['market'], _G['splits'], _G['vol'], _G['exec'],
                    ann=_G['ann'], ewma_lambda=_G['ewma_lambda'])


class Runner:
    """Unified evaluation interface: a parallel pool or a sequential fallback."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ann = cfg.get('ann', 365.0)                 # timeframe params (daily defaults)
        self.ewma_lambda = cfg.get('ewma_lambda', 0.06)
        freq = cfg.get('freq', 'D')
        vol_window = cfg.get('vol_window', 30)
        if cfg['n_jobs'] == 1:
            self.tk, _raw, self.panel = build_panel(cfg['data'], cfg['start'], cfg['end'],
                                                    cfg.get('instruments'), freq=freq)
            self.market = make_market(self.panel, self.tk, _raw, vol_window=vol_window)
            self.pool = None
        else:
            self.pool = mp.Pool(
                cfg['n_jobs'], initializer=_winit,
                initargs=(cfg['data'], cfg['start'], cfg['end'],
                          cfg['splits'], cfg['vol'], cfg['exec'], cfg.get('instruments'),
                          freq, vol_window, self.ann, self.ewma_lambda))

    def map(self, nodes):
        if not nodes:
            return []
        if self.pool is None:
            return [evaluate(n, self.tk, self.panel, self.market, self.cfg['splits'],
                             self.cfg['vol'], self.cfg['exec'],
                             ann=self.ann, ewma_lambda=self.ewma_lambda) for n in nodes]
        return self.pool.map(_weval, nodes, chunksize=1)

    def close(self):
        if self.pool:
            self.pool.close()
            self.pool.join()


# ---------------- correlation / novelty ----------------
def corr(a, b):
    if a is None or b is None or len(a) != len(b):
        return 0.0
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        return 0.0
    aa, bb = a[m], b[m]
    if aa.std() == 0 or bb.std() == 0:
        return 0.0
    return float(abs(np.corrcoef(aa, bb)[0, 1]))


# ---------------- fitness ----------------
def fitness(res, hof, cfg):
    if res is None:
        return -1e9
    base = min(res['train_sharpe'], res['val_sharpe'])
    if not np.isfinite(base):
        return -1e9
    fit = base - cfg['parsimony'] * res['size']
    if hof:
        mc = max(corr(res['rv'], h['rv']) for h in hof)
        if mc > cfg['corr_thresh']:
            fit -= cfg['corr_penalty'] * (mc - cfg['corr_thresh'])
    return fit


# ---------------- Hall of Fame (diverse) ----------------
def hof_update(hof, res, cfg):
    base = min(res['train_sharpe'], res['val_sharpe'])
    if not np.isfinite(base):
        return hof
    if any(h['canon'] == res['canon'] for h in hof):
        return hof
    cand = {**res, 'base': base}
    # the most similar current champion
    inc, inc_c = None, 0.0
    for h in hof:
        c = corr(cand['rv'], h['rv'])
        if c > inc_c:
            inc, inc_c = h, c
    if inc is not None and inc_c > cfg['corr_thresh']:
        if base <= inc['base']:
            return hof                      # worse than its similar peer — skip it
        hof.remove(inc)                     # better — evict the similar one
    hof.append(cand)
    hof.sort(key=lambda h: -h['base'])
    return hof[:cfg['hof_cap']]


# ---------------- selection / new generation ----------------
def _rand_sized(rng, cfg):
    """A random tree within max_size (a limited number of attempts, otherwise as-is)."""
    t = random_tree(rng, cfg['max_depth'], term_prob=0.3)
    for _ in range(8):
        if t.size() <= cfg['max_size']:
            break
        t = random_tree(rng, cfg['max_depth'], term_prob=0.45)
    return t


def _tournament(scored, rng, k):
    best = None
    for _ in range(k):
        s = scored[rng.randrange(len(scored))]
        if best is None or s[2] > best[2]:
            best = s
    return best[0]


def _next_pop(scored, rng, cfg, extra=None):
    """`extra`: pre-built nodes injected into the new generation (the LLM advisor's proposals).
    They take the random-injection slots first — informed guesses instead of blind ones."""
    valid = [s for s in scored if s[1] is not None]
    sel = valid if valid else scored
    new = [e[0].copy() for e in sorted(sel, key=lambda s: -s[2])[:cfg['elitism']]]  # elite
    for node in (extra or []):
        new.append(node)
    for _ in range(max(0, cfg['random_inject'] - len(extra or []))):               # novelty injection
        new.append(random_tree(rng, cfg['max_depth'], term_prob=0.3))
    guard = 0
    while len(new) < cfg['pop'] and guard < cfg['pop'] * 50:
        guard += 1
        if rng.random() < cfg['cx_prob']:
            child = crossover(_tournament(sel, rng, cfg['tourn']),
                              _tournament(sel, rng, cfg['tourn']), rng, cfg['max_depth'])
        else:
            child = mutate(_tournament(sel, rng, cfg['tourn']), rng, cfg['max_depth'])
        if 1 < child.size() <= cfg['max_size']:
            new.append(child)
    while len(new) < cfg['pop']:
        new.append(_rand_sized(rng, cfg))
    return new


def _init_pop(rng, cfg):
    pop, seen, guard = [], set(), 0
    for node in (cfg.get('seed_formulas') or []):     # warm-start: champions from the library
        if len(pop) >= cfg['pop']:
            break
        try:
            n = node if isinstance(node, Node) else parse(node)
        except Exception:
            continue
        if n.size() > cfg['max_size']:
            continue
        c = n.canon()
        if c in seen:
            continue
        seen.add(c)
        pop.append(n)
    while len(pop) < cfg['pop'] and guard < cfg['pop'] * 80:
        guard += 1
        t = random_tree(rng, cfg['max_depth'], term_prob=0.25)
        if t.size() > cfg['max_size']:
            continue
        c = t.canon()
        if c in seen:
            continue
        seen.add(c)
        pop.append(t)
    while len(pop) < cfg['pop']:
        pop.append(_rand_sized(rng, cfg))
    return pop


# ---------------- main loop ----------------
def evolve(cfg, log=print):
    rng = random.Random(cfg['seed'])
    runner = Runner(cfg)
    cache = {}          # canon -> res (already-evaluated formulas aren't recomputed)
    hof, history = [], []
    n_eval = 0

    # --- neuro-symbolic advisor (optional; see advisor.py). The LLM proposes, the sim judges.
    adv = None
    origins = {}        # canon -> 'llm' (provenance: is the advisor actually pulling weight?)
    best_fit_ever, stall = -1e18, 0
    if cfg.get('advisor'):
        from advisor import Advisor
        adv = Advisor(model=cfg.get('advisor_model'), n_proposals=cfg.get('advisor_n', 10), log=log)
        if not adv.available():
            adv = None                              # no SDK/creds -> behave exactly as before

    try:
        pop = _init_pop(rng, cfg)
        for gen in range(cfg['gens']):
            unseen = []
            seen_this = set()
            for n in pop:
                c = n.canon()
                if c not in cache and c not in seen_this:
                    seen_this.add(c)
                    unseen.append(n)
            for n, r in zip(unseen, runner.map(unseen)):
                cache[n.canon()] = r
                n_eval += 1

            scored = [(n, cache[n.canon()], 0.0) for n in pop]
            scored = [(n, r, fitness(r, hof, cfg)) for (n, r, _) in scored]
            for _, r, _f in scored:
                if r is not None:
                    hof = hof_update(hof, r, cfg)

            valid = [s for s in scored if s[1] is not None]
            n_valid = len(valid)
            best = max(valid, key=lambda s: s[2]) if valid else None
            hb = hof[0]['base'] if hof else float('nan')
            history.append({
                'gen': gen, 'evaluated': n_eval, 'unique': len(cache),
                'valid_frac': n_valid / len(pop),
                'best_fit': best[2] if best else float('nan'),
                'hof_best_base': hb, 'hof_size': len(hof),
            })
            log(f'gen {gen:2d} | evaluated {n_eval:5d} (uniq {len(cache):5d}) '
                f'| valid {n_valid:3d}/{len(pop)} '
                f'| best fit {best[2] if best else float("nan"):+.2f} '
                f'| HoF[0] base {hb:+.2f} size {len(hof)}')

            # --- plateau detector for the advisor: consult only when the blind search stalls
            proposals = []
            if best is not None and best[2] > best_fit_ever + 1e-9:
                best_fit_ever, stall = best[2], 0
            else:
                stall += 1
            if (adv is not None and gen < cfg['gens'] - 1
                    and stall >= cfg.get('advisor_patience', 4)
                    and adv.stats['calls'] < cfg.get('advisor_max_calls', 8)):
                top = [{'canon': r['canon'], 'fit': f, 'train': r['train_sharpe'],
                        'val': r['val_sharpe'], 'size': r['size']}
                       for (_n, r, f) in sorted(valid, key=lambda s: -s[2])[:10]]
                for node, hypo in adv.propose(top, [h['canon'] for h in hof], stall, cfg):
                    c = node.canon()
                    if c not in cache:              # already tried -> not worth a slot
                        origins[c] = 'llm'
                        proposals.append(node)
                        log(f'  advisor -> {c}  [{hypo}]')
                stall = 0                           # cooldown: don't consult every generation

            if gen < cfg['gens'] - 1:
                pop = _next_pop(scored, rng, cfg, extra=proposals)
    finally:
        runner.close()

    if adv is not None:
        for h in hof:
            h['origin'] = origins.get(h['canon'], 'ga')
        n_llm = sum(1 for h in hof if h['origin'] == 'llm')
        s = adv.stats
        log(f'advisor: {s["calls"]} calls, {s["proposed"]} proposed, {s["valid"]} valid '
            f'-> {n_llm}/{len(hof)} of the Hall of Fame is LLM-born')
    return hof, history, cache
