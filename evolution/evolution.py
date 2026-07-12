"""Evolutionary driver: a population of trees -> selection -> crossover/mutation -> champions.

Against overfitting (the main risk of searching through millions of formulas):
  * fitness = min(train_Sharpe, val_Sharpe) — a strategy is "good" only as much as
    its WORST training segment is (rewarding robustness, not curve-fitting);
  * a complexity penalty (parsimony) — simple beats complex;
  * a penalty/dedup for correlation with already-found champions (a diverse Hall of Fame);
  * the TEST segment is HELD-OUT: it plays no part in the fitness, computed once at the very end.
"""
import multiprocessing as mp
import random

import numpy as np

from genome import Node, random_tree, crossover, mutate, parse   # noqa: F401
from evaluator import build_panel, make_market, evaluate


# ---------------- parallel evaluation ----------------
_G = {}


def _winit(data, start, end, splits, vol, exec_rate, instruments):
    tk, raw, panel = build_panel(data, start, end, instruments)
    _G.update(tk=tk, panel=panel, market=make_market(panel, tk, raw),
              splits=splits, vol=vol, exec=exec_rate)


def _weval(node):
    return evaluate(node, _G['tk'], _G['panel'], _G['market'], _G['splits'], _G['vol'], _G['exec'])


class Runner:
    """Unified evaluation interface: a parallel pool or a sequential fallback."""

    def __init__(self, cfg):
        self.cfg = cfg
        if cfg['n_jobs'] == 1:
            self.tk, _raw, self.panel = build_panel(cfg['data'], cfg['start'], cfg['end'],
                                                    cfg.get('instruments'))
            self.market = make_market(self.panel, self.tk, _raw)
            self.pool = None
        else:
            self.pool = mp.Pool(
                cfg['n_jobs'], initializer=_winit,
                initargs=(cfg['data'], cfg['start'], cfg['end'],
                          cfg['splits'], cfg['vol'], cfg['exec'], cfg.get('instruments')))

    def map(self, nodes):
        if not nodes:
            return []
        if self.pool is None:
            return [evaluate(n, self.tk, self.panel, self.market,
                             self.cfg['splits'], self.cfg['vol'], self.cfg['exec']) for n in nodes]
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


def _next_pop(scored, rng, cfg):
    valid = [s for s in scored if s[1] is not None]
    sel = valid if valid else scored
    new = [e[0].copy() for e in sorted(sel, key=lambda s: -s[2])[:cfg['elitism']]]  # elite
    for _ in range(cfg['random_inject']):                                          # novelty injection
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

            if gen < cfg['gens'] - 1:
                pop = _next_pop(scored, rng, cfg)
    finally:
        runner.close()
    return hof, history, cache
