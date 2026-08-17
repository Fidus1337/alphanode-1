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
    last_build TEXT,                          -- build_id last seen activating (leak provenance)
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE(user_id, device_id)
);
CREATE TABLE IF NOT EXISTS device_claims (    -- permanent ownership ledger: which account FIRST
    device_id  TEXT PRIMARY KEY,              -- activated a node. v2 sealed boxes name their
    user_id    INTEGER NOT NULL,              -- minting node, and /reveal refuses every other
    claimed_at TEXT NOT NULL                  -- account. Survives seat pruning and downgrades on
);                                            -- purpose: ownership is not a billing artifact.
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
    name       TEXT,
    phone      TEXT,                          -- optional on the form; a way to reach fast buyers
    note       TEXT,
    ip         TEXT,                          -- submitting client, for abuse forensics
    status     TEXT NOT NULL DEFAULT 'new',   -- new | invited
    notified_at TEXT,                         -- NULL = the operator was never told about this one
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
    _migrate(conn)
    conn.commit()


def _migrate(conn):
    """Additive column adds for a database created by an earlier version. CREATE TABLE IF NOT
    EXISTS is a no-op once the table exists, so a column added to SCHEMA never reaches a live
    hub without this — and the live hub is the one holding the waitlist."""
    have = {r['name'] for r in conn.execute('PRAGMA table_info(access_requests)')}
    for col in ('name', 'phone', 'ip', 'notified_at'):
        if col not in have:
            conn.execute(f'ALTER TABLE access_requests ADD COLUMN {col} TEXT')
    dev_cols = {r['name'] for r in conn.execute('PRAGMA table_info(devices)')}
    if 'last_build' not in dev_cols:
        conn.execute('ALTER TABLE devices ADD COLUMN last_build TEXT')
    # ownership backfill: every seat that existed before the claims ledger belongs to the
    # account that held it (earliest first_seen wins if one device_id somehow sits under two
    # accounts). OR IGNORE makes the backfill idempotent across restarts.
    conn.execute('INSERT OR IGNORE INTO device_claims(device_id, user_id, claimed_at) '
                 'SELECT device_id, user_id, first_seen FROM devices ORDER BY first_seen, id')


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


def register_device(conn, user_id, device_id, node_limit, label=None, build=None):
    """Seat gate. An already-registered device is always allowed (just bumps last_seen). A new
    device is allowed only while count < node_limit. Returns (ok, reason). The count check and the
    insert run in one IMMEDIATE transaction so two nodes racing for the last seat can't both win.
    `build` is the reporting node's build_id, recorded for leak provenance."""
    if not device_id:
        return False, 'device_id required'
    now = iso_now()
    try:
        conn.execute('BEGIN IMMEDIATE')
        # ownership first: a device_id already claimed by ANOTHER account can never be
        # re-registered — otherwise a stolen state dir (library + device_id ride together)
        # could be re-homed and its sealed boxes revealed under the thief's subscription.
        claim = conn.execute('SELECT user_id FROM device_claims WHERE device_id = ?',
                             (device_id,)).fetchone()
        if claim is not None and claim['user_id'] != user_id:
            conn.rollback()
            return False, 'node is registered to another account (contact support)'
        row = conn.execute('SELECT id FROM devices WHERE user_id = ? AND device_id = ?',
                           (user_id, device_id)).fetchone()
        if row is not None:
            conn.execute('UPDATE devices SET last_seen = ?, label = COALESCE(?, label), '
                         'last_build = COALESCE(?, last_build) WHERE id = ?',
                         (now, label, build, row['id']))
            conn.execute('INSERT OR IGNORE INTO device_claims(device_id, user_id, claimed_at) '
                         'VALUES (?,?,?)', (device_id, user_id, now))
            conn.commit()
            return True, 'known device'
        n = conn.execute('SELECT COUNT(*) AS n FROM devices WHERE user_id = ?',
                         (user_id,)).fetchone()['n']
        if n >= node_limit:
            conn.rollback()
            return False, f'node limit reached ({n}/{node_limit})'
        conn.execute('INSERT INTO devices(user_id, device_id, label, last_build, '
                     'first_seen, last_seen) VALUES (?,?,?,?,?,?)',
                     (user_id, device_id, label, build, now, now))
        conn.execute('INSERT OR IGNORE INTO device_claims(device_id, user_id, claimed_at) '
                     'VALUES (?,?,?)', (device_id, user_id, now))
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
    return conn.execute('SELECT device_id, label, last_build, first_seen, last_seen FROM devices '
                        'WHERE user_id = ? ORDER BY first_seen', (user_id,)).fetchall()


# ---- ownership ledger (formula <-> account binding) ----
def get_claim(conn, device_id):
    return conn.execute('SELECT * FROM device_claims WHERE device_id = ?',
                        (device_id,)).fetchone()


def release_claim(conn, device_id):
    """Support path only (admin release-node): frees a device_id for re-claim — e.g. a customer
    who legitimately moved their node to a new account. Boxes minted by that node become
    revealable by whichever account claims it NEXT, so verify the story before releasing."""
    cur = conn.execute('DELETE FROM device_claims WHERE device_id = ?', (device_id,))
    conn.commit()
    return cur.rowcount > 0


def list_claims(conn):
    return conn.execute(
        'SELECT c.device_id, c.claimed_at, u.email FROM device_claims c '
        'LEFT JOIN users u ON u.id = c.user_id ORDER BY c.claimed_at, c.device_id').fetchall()


def log_reveal(conn, user_id, device_id, formula_id):
    conn.execute('INSERT INTO reveals(user_id, device_id, formula_id, at) VALUES (?,?,?,?)',
                 (user_id, device_id, formula_id, iso_now()))
    conn.commit()


# ---- early-access waitlist (the site's request form) ----
def add_access_request(conn, email, name=None, phone=None, note=None, ip=None):
    """Record an early-access request. One row per address — a re-submit refreshes the details and
    the timestamp instead of piling up duplicates, so the list stays a list of PEOPLE. Returns
    True when this is a first-time request (worth a notification), False for a repeat.

    Only the email is validated: name and phone are free text people type in a hurry, and losing a
    prospective buyer over a phone format we did not anticipate costs more than a messy string.
    The email is different — it becomes a Reply-To header, an `admin invite` argument and
    eventually an account identity, so it must be ONE address: no whitespace, and none of the
    separators that would smuggle a second recipient into a header ("bob@x.io, evil@attacker.tld"
    passed the old contains-@ check and made every reply a CC to the attacker)."""
    # lowercased like every other email in this module: the UNIQUE index is case-sensitive, so
    # without it "Yurii@Gmail.com" is a second person, and `invite` (which grants against the
    # lowercased address) would never find the row to mark.
    email = (email or '').strip().lower()
    if (len(email) < 3 or '@' not in email[1:] or len(email) > 320
            or any(c in ',<>;' or c.isspace() for c in email)):
        raise ValueError('invalid email')
    name = (name or '').strip()[:200] or None
    phone = (phone or '').strip()[:64] or None
    note = (note or '').strip()[:2000] or None
    ip = (ip or '').strip()[:64] or None
    now = iso_now()
    # asked BEFORE the write: an upsert reports rowcount 1 either way, so it cannot tell a new
    # person from someone submitting twice — and that distinction is what gates the notification
    first_time = conn.execute('SELECT 1 FROM access_requests WHERE email = ?',
                              (email,)).fetchone() is None
    # COALESCE per field: a second submit that leaves the phone blank must not erase the number
    # given the first time round.
    conn.execute(
        'INSERT INTO access_requests(email, name, phone, note, ip, created_at, updated_at) '
        'VALUES (?,?,?,?,?,?,?) '
        'ON CONFLICT(email) DO UPDATE SET name  = COALESCE(excluded.name,  access_requests.name), '
        '                                 phone = COALESCE(excluded.phone, access_requests.phone), '
        '                                 note  = COALESCE(excluded.note,  access_requests.note), '
        '                                 ip    = COALESCE(excluded.ip,    access_requests.ip), '
        '                                 updated_at = excluded.updated_at',
        (email, name, phone, note, ip, now, now))
    conn.commit()
    return first_time


def get_access_request(conn, email):
    return conn.execute('SELECT * FROM access_requests WHERE email = ?',
                        ((email or '').strip().lower(),)).fetchone()


def list_unnotified(conn):
    """Requests the operator has never been told about — because SMTP was not configured yet when
    they arrived, or because the send failed. This is the backlog `admin catchup` clears."""
    return conn.execute('SELECT * FROM access_requests WHERE notified_at IS NULL '
                        'ORDER BY created_at').fetchall()


def mark_notified(conn, emails):
    """Stamp requests as announced. Called only after a send actually succeeded, so a dead SMTP
    server leaves the backlog intact instead of quietly swallowing it."""
    if isinstance(emails, str):
        emails = [emails]
    now = iso_now()
    for e in emails:
        conn.execute('UPDATE access_requests SET notified_at = ? WHERE email = ?',
                     (now, (e or '').strip().lower()))
    conn.commit()


def delete_access_request(conn, email):
    """The removal path the site's privacy note promises. An actual DELETE — a row that merely
    changes status is still someone's name and phone number sitting in the database."""
    cur = conn.execute('DELETE FROM access_requests WHERE email = ?',
                       ((email or '').strip().lower(),))
    conn.commit()
    return cur.rowcount > 0


def list_access_requests(conn, status=None):
    if status:
        return conn.execute('SELECT * FROM access_requests WHERE status = ? '
                            'ORDER BY created_at', (status,)).fetchall()
    return conn.execute('SELECT * FROM access_requests ORDER BY created_at').fetchall()


def mark_access_request(conn, email, status):
    if status not in ('new', 'invited'):
        raise ValueError("status must be 'new' or 'invited'")
    cur = conn.execute('UPDATE access_requests SET status = ?, updated_at = ? WHERE email = ?',
                       (status, iso_now(), (email or '').strip().lower()))
    conn.commit()
    return cur.rowcount > 0
