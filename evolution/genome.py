"""Genome = an expression tree that produces an alpha signal.

A single data type (a wide table), so crossover/mutation don't break types:
any subtree can be put in place of any other.

Node.op — either a feature name (FEATURES, then it's a leaf), or a primitive name.
Node.window — the window for time-series primitives (otherwise None).
canon() — the canonical string: both a human-readable form and the cache/dedup key.
"""
import math

import primitives as P


def mutate_window_value(w, rng):
    """Smart window step: a log-normal jitter around the CURRENT horizon (30 -> 34, 30 -> 24 …,
    any integer in [W_MIN, W_MAX]) so evolution can TUNE a horizon, not just hop the coarse
    grid; ~20% of the time a global jump onto the grid prior keeps exploration alive."""
    if w is None or rng.random() < 0.2:
        return rng.choice(P.WINDOWS)
    for _ in range(4):
        nw = max(P.W_MIN, min(P.W_MAX, round(w * math.exp(rng.gauss(0.0, 0.35)))))
        if nw != w:
            return nw
    return max(P.W_MIN, min(P.W_MAX, w + (1 if rng.random() < 0.5 else -1)))


class Node:
    __slots__ = ('op', 'children', 'window', '_canon', '_size')

    def __init__(self, op, children=None, window=None):
        self.op = op
        self.children = children if children is not None else []
        self.window = window
        self._canon = None
        self._size = None

    # ---------- structure ----------
    @property
    def is_terminal(self):
        return self.op in P.FEATURES

    def canon(self):
        if self._canon is not None:
            return self._canon
        if self.is_terminal:
            s = self.op
        else:
            w = f':{self.window}' if self.window is not None else ''
            s = f'{self.op}{w}(' + ','.join(c.canon() for c in self.children) + ')'
        self._canon = s
        return s

    def size(self):
        if self._size is not None:                # memoized like canon(); reset on any mutation
            return self._size
        self._size = 1 + sum(c.size() for c in self.children)
        return self._size

    def depth(self):
        return 1 + max((c.depth() for c in self.children), default=0)

    def copy(self):
        return Node(self.op,
                    [c.copy() for c in self.children],
                    self.window)

    def all_nodes(self):
        """List of all nodes in the subtree (for choosing a crossover/mutation point)."""
        out = [self]
        for c in self.children:
            out.extend(c.all_nodes())
        return out

    def __repr__(self):
        return self.canon()


# ================= random generation =================
def random_terminal(rng):
    return Node(rng.choice(P.FEATURES))


def random_op_node(rng, children):
    op = rng.choice(P.ALL_PRIMS)
    # arity is fixed by the registry; children are already prepared for it by the caller
    window = rng.choice(P.WINDOWS) if P.NEEDS_WINDOW[op] else None
    return Node(op, children, window)


def random_tree(rng, max_depth, term_prob=0.25, depth=0):
    """Grow method: as depth increases so does the leaf chance; the root is always an operator."""
    force_op = (depth == 0)
    if not force_op and (depth >= max_depth or rng.random() < term_prob):
        return random_terminal(rng)
    op = rng.choice(P.ALL_PRIMS)
    arity = P.ARITY[op]
    window = rng.choice(P.WINDOWS) if P.NEEDS_WINDOW[op] else None
    children = [random_tree(rng, max_depth, term_prob, depth + 1) for _ in range(arity)]
    return Node(op, children, window)


# ================= depth pruning =================
def prune(node, max_depth, rng, depth=0):
    """If a subtree is deeper than the limit — replace it with a leaf (preserving validity)."""
    if depth >= max_depth:
        return random_terminal(rng) if not node.is_terminal else node
    node.children = [prune(c, max_depth, rng, depth + 1) for c in node.children]
    node._canon = None
    node._size = None
    return node


# ================= crossover =================
def _pick_node(tree, rng, internal_bias=0.9):
    """Pick a node; with probability internal_bias prefer internal ones (operators)."""
    nodes = tree.all_nodes()
    internal = [n for n in nodes if not n.is_terminal]
    if internal and rng.random() < internal_bias:
        return rng.choice(internal)
    return rng.choice(nodes)


def crossover(p1, p2, rng, max_depth):
    """Subtree exchange: a copy of p1, into which a subtree from p2 is grafted."""
    child = p1.copy()
    nodes = child.all_nodes()
    # don't use the root (nodes[0]) as an insertion point — otherwise a donor leaf would make the
    # root a bare feature (violating the invariant "the root is always an operator"). The root is an
    # operator with children, so nodes[1:] is non-empty.
    cand = nodes[1:] if len(nodes) > 1 else nodes
    target = _pick_node_from_list(cand, rng)
    donor = _pick_node(p2, rng).copy()
    # graft: overwrite the contents of target with the contents of donor
    target.op, target.children, target.window = donor.op, donor.children, donor.window
    _invalidate(child)
    return prune(child, max_depth, rng)


def _pick_node_from_list(nodes, rng, internal_bias=0.9):
    internal = [n for n in nodes if not n.is_terminal]
    if internal and rng.random() < internal_bias:
        return rng.choice(internal)
    return rng.choice(nodes)


def _invalidate(node):
    node._canon = None
    node._size = None
    for c in node.children:
        _invalidate(c)


# ================= mutations =================
def mutate(node, rng, max_depth):
    """One of three kinds of mutation, with equal probability."""
    child = node.copy()
    kind = rng.choice(('subtree', 'point', 'window'))
    if kind == 'subtree':
        _mutate_subtree(child, rng, max_depth)
    elif kind == 'point':
        _mutate_point(child, rng)
    else:
        _mutate_window(child, rng)
    _invalidate(child)
    return prune(child, max_depth, rng)


def _mutate_subtree(tree, rng, max_depth):
    target = _pick_node(tree, rng, internal_bias=0.7)
    fresh = random_tree(rng, max_depth=max(2, max_depth - 1), term_prob=0.4)
    target.op, target.children, target.window = fresh.op, fresh.children, fresh.window


def _mutate_point(tree, rng):
    """Replace an operator with another from the same group (same arity/type)."""
    ops = [n for n in tree.all_nodes() if not n.is_terminal]
    if not ops:
        # a single-leaf tree — replace the feature
        leaf = tree.all_nodes()[0]
        leaf.op = rng.choice(P.FEATURES)
        return
    n = rng.choice(ops)
    for grp in P.COMPAT_GROUPS:
        if n.op in grp:
            n.op = rng.choice(grp)
            # swapping the statistic KEEPS the horizon (ts_mean:30 -> ema:30) — the idea
            # "a monthly signal" survives the operator swap instead of being re-rolled
            n.window = ((n.window or rng.choice(P.WINDOWS))
                        if P.NEEDS_WINDOW[n.op] else None)
            break


def _mutate_window(tree, rng):
    windowed = [n for n in tree.all_nodes() if n.window is not None]
    if not windowed:
        return _mutate_point(tree, rng)      # nothing to tune — fall back to point mutation
    n = rng.choice(windowed)
    n.window = mutate_window_value(n.window, rng)


# ================= parse a canonical string back into a tree =================
def parse(s):
    """canon() -> Node. The exact inverse of Node.canon() for all operators."""
    pos = 0

    def node():
        nonlocal pos
        j = pos
        while j < len(s) and s[j] not in '(),:':
            j += 1
        name = s[pos:j]
        pos = j
        window = None
        if pos < len(s) and s[pos] == ':':
            pos += 1
            k = pos
            while k < len(s) and s[k].isdigit():
                k += 1
            window = int(s[pos:k])
            pos = k
        if name in P.FEATURES:
            return Node(name)
        assert s[pos] == '(', f'expected "(" at position {pos}: ...{s[pos:pos + 12]}'
        pos += 1
        children = [node()]
        while s[pos] == ',':
            pos += 1
            children.append(node())
        assert s[pos] == ')', f'expected ")" at position {pos}'
        pos += 1
        return Node(name, children, window)

    return node()
