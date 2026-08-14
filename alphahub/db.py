"""SQLite data layer for AlphaHub: accounts, subscriptions, the device roster, a reveal audit
log. Pure data access — no crypto, no HTTP. Times are ISO-8601 UTC strings; comparisons parse
them back to aware datetimes (never lexicographic, which would break across offsets)."""
import secrets
import sqlite3
from datetime import datetime, timezone

# Plans: node_limit is the seat count (distinct registered machines). demo is the free tier,
# tied to an account so it can't be farmed anonymously. price_usd is informational here — the
# real amount lives in the payment provider; the webhook only tells us WHICH plan was bought.
PLANS = {
    'demo':  {'node_limit': 3,  'price_usd': 0},
    'pro':   {'node_limit': 5,  'price_usd': 100},
    'scale': {'node_limit': 50, 'price_usd': 500},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,
    token      TEXT UNIQUE NOT NULL,           -- the account credential the node carries
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plan       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active', -- active | canceled
    expires_at TEXT,                           -- ISO UTC; NULL = never (free/demo)
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id  TEXT NOT NULL,
    label      TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE(user_id, device_id)
);
CREATE TABLE IF NOT EXISTS reveals (          -- audit: which account/device opened which formula
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    device_id  TEXT NOT NULL,
    formula_id TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_requests (  -- early-access waitlist from the site's form
    id         INTEGER PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,          -- one row per address: a re-submit updates it
    note       TEXT,
    status     TEXT NOT NULL DEFAULT 'new',   -- new | invited
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_devices_user ON devices(user_id);
"""


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _parse(ts):
    """ISO string -> aware UTC datetime (naive input is assumed UTC). Accepts a trailing 'Z'
    (RFC3339), which datetime.fromisoformat rejects before Python 3.11."""
    t = ts.strip()
    if t.endswith(('Z', 'z')):
        t = t[:-1] + '+00:00'
    dt = datetime.fromisoformat(t)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_expiry(s):
    """Validate + canonicalize an expiry at the WRITE boundary: None/'' -> None (never expires),
    otherwise a parsed-and-reserialized ISO UTC string. Raises ValueError on garbage, so a bad
    webhook date is rejected up front (400) instead of poisoning the row and 500-ing every later
    gate for that account."""
    if s is None or not str(s).strip():
        return None
    return _parse(str(s)).isoformat(timespec='seconds')


def connect(path):
    # check_same_thread=False: FastAPI runs sync deps and endpoints across threadpool threads, so
    # a per-request connection is legitimately touched from more than one thread (never two at
    # once). row_factory for dict-ish rows; foreign keys on for the ON DELETE CASCADE.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def new_token():
    return secrets.token_urlsafe(24)


# ---- accounts / subscriptions ----
def get_user_by_token(conn, token):
    if not token:
        return None
    return conn.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()


def get_user_by_email(conn, email):
    return conn.execute('SELECT * FROM users WHERE email = ?', (email.lower(),)).fetchone()


def apply_payment(conn, email, plan, expires_at=None, status='active'):
    """Idempotent account+subscription upsert — the ONE mutation the payment webhook and the
    admin CLI share. Creates the account (minting a token) on first sight, then sets the plan.
    Validates plan / status / expiry up front (a bad webhook can't grant infinite seats or poison
    the row). A downgrade prunes seats past the new limit (oldest kept). Returns the token."""
    if plan not in PLANS:
        raise ValueError(f'unknown plan {plan!r}')
    if status not in ('active', 'canceled'):
        raise ValueError(f'invalid status {status!r}')
    expires_at = normalize_expiry(expires_at)            # raises ValueError on a bad date
    email = email.lower().strip()
    if not email:
        raise ValueError('email required')
    now = iso_now()
    row = get_user_by_email(conn, email)
    if row is None:
        token = new_token()
        cur = conn.execute('INSERT INTO users(email, token, created_at) VALUES (?,?,?)',
                           (email, token, now))
        user_id = cur.lastrowid
    else:
        user_id, token = row['id'], row['token']
    conn.execute(
        'INSERT INTO subscriptions(user_id, plan, status, expires_at, updated_at) '
        'VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
        'plan=excluded.plan, status=excluded.status, expires_at=excluded.expires_at, '
        'updated_at=excluded.updated_at',
        (user_id, plan, status, expires_at, now))
    _prune_seats(conn, user_id, PLANS[plan]['node_limit'])   # downgrade shrinks the roster
    conn.commit()
    return token


def _prune_seats(conn, user_id, node_limit):
    """Keep the oldest `node_limit` devices, drop the rest — so a downgrade actually reduces the
    usable seats instead of grandfathering every old machine past the new cap."""
    rows = conn.execute('SELECT id FROM devices WHERE user_id = ? ORDER BY first_seen, id',
                        (user_id,)).fetchall()
    for r in rows[node_limit:]:
        conn.execute('DELETE FROM devices WHERE id = ?', (r['id'],))


def rotate_token(conn, user_id):
    """Issue a fresh account token (revokes the old one everywhere at once)."""
    token = new_token()
    conn.execute('UPDATE users SET token = ? WHERE id = ?', (token, user_id))
    conn.commit()
    return token


def subscription_state(conn, user_id):
    """Everything the gates need in one shot: plan, node_limit, seats used, and whether the
    subscription is live right now (status active AND not past expiry)."""
    sub = conn.execute('SELECT * FROM subscriptions WHERE user_id = ?', (user_id,)).fetchone()
    used = device_count(conn, user_id)
    if sub is None:
        return {'plan': None, 'status': 'none', 'node_limit': 0, 'expires_at': None,
                'active': False, 'used': used}
    plan = sub['plan']
    limit = PLANS.get(plan, {}).get('node_limit', 0)
    try:                                                 # a malformed stored date must not 500 the
        expired = (sub['expires_at'] is not None            # gate — treat it as expired instead
                   and _parse(sub['expires_at']) <= datetime.now(timezone.utc))
    except ValueError:
        expired = True
    active = sub['status'] == 'active' and not expired
    return {'plan': plan, 'status': sub['status'], 'node_limit': limit,
            'expires_at': sub['expires_at'], 'active': active, 'used': used}


# ---- devices (seat enforcement) ----
def device_count(conn, user_id):
    return conn.execute('SELECT COUNT(*) AS n FROM devices WHERE user_id = ?',
                        (user_id,)).fetchone()['n']


def get_device(conn, user_id, device_id):
    return conn.execute('SELECT * FROM devices WHERE user_id = ? AND device_id = ?',
                        (user_id, device_id)).fetchone()


def register_device(conn, user_id, device_id, node_limit, label=None):
    """Seat gate. An already-registered device is always allowed (just bumps last_seen). A new
    device is allowed only while count < node_limit. Returns (ok, reason). The count check and the
    insert run in one IMMEDIATE transaction so two nodes racing for the last seat can't both win."""
    if not device_id:
        return False, 'device_id required'
    now = iso_now()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT id FROM devices WHERE user_id = ? AND device_id = ?',
                           (user_id, device_id)).fetchone()
        if row is not None:
            conn.execute('UPDATE devices SET last_seen = ?, label = COALESCE(?, label) '
                         'WHERE id = ?', (now, label, row['id']))
            conn.commit()
            return True, 'known device'
        n = conn.execute('SELECT COUNT(*) AS n FROM devices WHERE user_id = ?',
                         (user_id,)).fetchone()['n']
        if n >= node_limit:
            conn.rollback()
            return False, f'node limit reached ({n}/{node_limit})'
        conn.execute('INSERT INTO devices(user_id, device_id, label, first_seen, last_seen) '
                     'VALUES (?,?,?,?,?)', (user_id, device_id, label, now, now))
        conn.commit()
        return True, 'registered'
    except Exception:                                    # noqa: BLE001 — never leave a txn open
        conn.rollback()
        raise


def remove_device(conn, user_id, device_id):
    cur = conn.execute('DELETE FROM devices WHERE user_id = ? AND device_id = ?',
                       (user_id, device_id))
    conn.commit()
    return cur.rowcount > 0


def list_devices(conn, user_id):
    return conn.execute('SELECT device_id, label, first_seen, last_seen FROM devices '
                        'WHERE user_id = ? ORDER BY first_seen', (user_id,)).fetchall()


def log_reveal(conn, user_id, device_id, formula_id):
    conn.execute('INSERT INTO reveals(user_id, device_id, formula_id, at) VALUES (?,?,?,?)',
                 (user_id, device_id, formula_id, iso_now()))
    conn.commit()


# ---- early-access waitlist (the site's request form) ----
def add_access_request(conn, email, note=None):
    """Record an early-access request. One row per address — a re-submit refreshes the note and
    the timestamp instead of piling up duplicates, so the list stays a list of PEOPLE. Returns
    True when this is a first-time request (worth a notification), False for a repeat."""
    email = (email or '').strip()
    if len(email) < 3 or '@' not in email or len(email) > 320:
        raise ValueError('invalid email')
    note = (note or '').strip()[:2000] or None
    now = iso_now()
    # asked BEFORE the write: an upsert reports rowcount 1 either way, so it cannot tell a new
    # person from someone submitting twice — and that distinction is what gates the notification
    first_time = conn.execute('SELECT 1 FROM access_requests WHERE email = ?',
                              (email,)).fetchone() is None
    conn.execute(
        'INSERT INTO access_requests(email, note, created_at, updated_at) VALUES (?,?,?,?) '
        'ON CONFLICT(email) DO UPDATE SET note = COALESCE(excluded.note, access_requests.note), '
        '                                 updated_at = excluded.updated_at',
        (email, note, now, now))
    conn.commit()
    return first_time


def list_access_requests(conn, status=None):
    if status:
        return conn.execute('SELECT * FROM access_requests WHERE status = ? '
                            'ORDER BY created_at', (status,)).fetchall()
    return conn.execute('SELECT * FROM access_requests ORDER BY created_at').fetchall()


def mark_access_request(conn, email, status):
    if status not in ('new', 'invited'):
        raise ValueError("status must be 'new' or 'invited'")
    cur = conn.execute('UPDATE access_requests SET status = ?, updated_at = ? WHERE email = ?',
                       (status, iso_now(), (email or '').strip()))
    conn.commit()
    return cur.rowcount > 0
