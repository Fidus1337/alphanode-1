"""Starred alphas — the user's own picks, kept outside the LIBRARY but inside the SESSION.

'Clear all history' wipes the library and leaves the stars standing: each favorite is the
full champion doc frozen at star time (formula + metrics), so it stays readable after the
library row that produced it is gone. A session load is the other case and goes the other
way — favorites.json is session-owned (sessions._STATE_PATTERNS), so a workspace arrives
with its own stars instead of inheriting the last one's. Stars used to outlive the library
they pointed into: load a different session and the ★ list still held rows mined on another
basket, another cut, sometimes another timeframe, with nothing on screen saying so."""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

FILE = 'favorites.json'


def alpha_id(formula):
    """The same 6-char id the leaderboard shows — favorites and the table read as one list."""
    return hashlib.md5(formula.encode()).hexdigest()[:6]


def _path(state_dir):
    return os.path.join(state_dir, FILE)


def load(state_dir):
    """The starred docs, oldest first. Missing or corrupt file reads as 'no favorites'."""
    try:
        with open(_path(state_dir), encoding='utf-8') as fh:
            doc = json.load(fh)
        favs = doc.get('favorites', [])
        return [f for f in favs if isinstance(f, dict) and f.get('formula')]
    except Exception:                                    # noqa: BLE001 — absent/corrupt = empty
        return []


def save(state_dir, favs):
    """Atomic write — a crash mid-save must never eat the star list."""
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix='.favorites-', suffix='.partial')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'favorites': list(favs)}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _path(state_dir))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ids(state_dir):
    """The starred ids as a set — what the leaderboard paints its ★ column from."""
    return {alpha_id(f['formula']) for f in load(state_dir)}


def toggle(state_dir, champ, tf):
    """Star or unstar a champion doc. Returns (favs, added). Docs without formula text
    (sealed vault rows) can't be starred — there is nothing to keep."""
    formula = champ.get('formula') or ''
    if not formula:
        raise ValueError('no formula text — locked docs cannot be starred')
    aid = alpha_id(formula)
    favs = load(state_dir)
    kept = [f for f in favs if alpha_id(f['formula']) != aid]
    added = len(kept) == len(favs)
    if added:
        doc = dict(champ)                                # frozen at star time
        doc['added'] = datetime.now(timezone.utc).date().isoformat()
        doc['tf'] = tf
        kept.append(doc)
    save(state_dir, kept)
    return kept, added


def remove(state_dir, aid):
    """Drop one favorite by its 6-char id. Returns the remaining list."""
    favs = [f for f in load(state_dir) if alpha_id(f['formula']) != aid]
    save(state_dir, favs)
    return favs
