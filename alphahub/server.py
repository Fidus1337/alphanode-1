"""AlphaHub HTTP API (FastAPI). Two gates, as designed:

  * POST /activate  — the NODE-COUNT gate: an account may register up to plan.node_limit distinct
                      machines (seats). Hit on node start / periodically.
  * POST /reveal    — the SUBSCRIPTION gate: unseal a formula only for a live subscription on an
                      already-activated device. This is where the vendor's private key is used.

Plus /signup (free demo account), /webhook/payment (provider-agnostic; Paddle/crypto call it),
/me (account status for the app + web dashboard), /pub (the key the node seals to), /health.

Run:  uvicorn alphahub.server:app --host 127.0.0.1 --port 8790
Config (env): ALPHAHUB_DB, ALPHAHUB_VAULT_KEY, ALPHAHUB_WEBHOOK_SECRET, ALPHAHUB_SITE_ORIGIN,
and the optional ALPHAHUB_SMTP_* / ALPHAHUB_NOTIFY_TO block that mails early-access requests
to the operator (off unless both HOST and NOTIFY_TO are set; `admin testmail` proves it works).
NOTE: terminate TLS in front of this (a reverse proxy) — /reveal returns plaintext formulas.
Cap request bodies at that proxy too (e.g. Caddy `request_body max_size 2MB`): FastAPI parses
the whole JSON body before any auth check runs, so the proxy is the real pre-auth size gate.
"""
import os
import re
import sys

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'alphanode'), PROJ):       # reuse the node's sealed-box crypto
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vault                                              # noqa: E402
from alphahub import db as hubdb                          # noqa: E402


# ---- request bodies ----
class SignupIn(BaseModel):
    email: str


class MeIn(BaseModel):
    token: str


class PaymentIn(BaseModel):
    secret: str
    email: str
    plan: str
    expires_at: str | None = None
    status: str = 'active'


class ActivateIn(BaseModel):
    token: str
    device_id: str
    label: str | None = None


class RevealIn(BaseModel):
    token: str
    device_id: str
    formula_enc: str


class RevealBatchIn(BaseModel):
    token: str
    device_id: str
    formulas: list[str]


class AccessRequestIn(BaseModel):
    email: str
    name: str | None = None
    phone: str | None = None                             # optional on the form
    note: str | None = None
    website: str | None = None                           # honeypot: humans leave this empty


def header_safe(value, limit=320):
    """Fold CR/LF out of anything that becomes a mail header and cap its length. The name and
    address here come straight off a public form, and a newline in a header value is exactly how
    a stranger gets to write their own headers into our mail."""
    return re.sub(r'[\r\n\t]+', ' ', str(value or '')).strip()[:limit]


def mail_config():
    """The SMTP settings, or None when notifications are off. One function decides what
    'configured' means, so the startup banner, `admin testmail` and the live path can never
    disagree about whether mail is going to work.

    ALPHAHUB_SMTP_TLS picks the transport: starttls (default, port 587), ssl (implicit TLS, the
    default when the port is 465), or none for a relay on the same host."""
    host = (os.environ.get('ALPHAHUB_SMTP_HOST') or '').strip()
    to = (os.environ.get('ALPHAHUB_NOTIFY_TO') or '').strip()
    if not host or not to:
        return None
    try:
        port = int((os.environ.get('ALPHAHUB_SMTP_PORT') or '587').strip())
    except ValueError:
        port = 587
    mode = (os.environ.get('ALPHAHUB_SMTP_TLS') or '').strip().lower()
    if mode not in ('starttls', 'ssl', 'none'):
        mode = 'ssl' if port == 465 else 'starttls'
    return {'host': host, 'port': port, 'tls': mode, 'to': to,
            'user': (os.environ.get('ALPHAHUB_SMTP_USER') or '').strip(),
            'password': os.environ.get('ALPHAHUB_SMTP_PASS') or '',
            # most providers reject a From they have not verified, so it defaults to the
            # recipient (you mailing yourself) rather than to the visitor's address
            'sender': (os.environ.get('ALPHAHUB_SMTP_FROM') or '').strip() or to}


def send_mail(subject, body, reply_to=None, cfg=None):
    """Best-effort email to the operator. Returns (ok, detail) and never raises: the request is
    already committed to the database by the time this runs, and a dead SMTP server must not turn
    a visitor's form into an error page. Failures are logged with the reason — silence would leave
    you unable to tell 'nobody applied' from 'the mail path is broken'."""
    cfg = cfg or mail_config()
    if cfg is None:
        return False, 'not configured'
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg['From'] = header_safe(cfg['sender'])
    msg['To'] = header_safe(cfg['to'])
    msg['Subject'] = header_safe(subject, 200)
    if reply_to:
        msg['Reply-To'] = header_safe(reply_to)          # hit Reply, write to the person who asked
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        smtp = (smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=20, context=ctx)
                if cfg['tls'] == 'ssl' else
                smtplib.SMTP(cfg['host'], cfg['port'], timeout=20))
        with smtp:
            smtp.ehlo()
            if cfg['tls'] == 'starttls':
                smtp.starttls(context=ctx)
                smtp.ehlo()                              # capabilities change after the upgrade
            if cfg['user']:
                smtp.login(cfg['user'], cfg['password'])
            smtp.send_message(msg)
        print(f'[notify] sent to {cfg["to"]}: {msg["Subject"]}', flush=True)
        return True, f'sent to {cfg["to"]}'
    except Exception as e:                               # noqa: BLE001 — log and move on
        detail = f'{type(e).__name__}: {e}'
        print(f'[notify] FAILED via {cfg["host"]}:{cfg["port"]} ({cfg["tls"]}) '
              f'-> {cfg["to"]}: {detail}', flush=True)
        return False, detail


def create_app(db_path, key_path, webhook_secret, site_origins=()):
    """Build an app bound to explicit config (tests pass scratch paths; the module-level `app`
    below builds this from env). The vault keypair is created on first run if absent.
    site_origins: exact origins the marketing site is served from (https://example.com) — the
    browser needs CORS to POST /signup and /me from there."""
    if not os.path.exists(key_path):
        try:
            vault.generate_keys(key_path)                # O_EXCL inside: refuses to clobber a key
        except FileExistsError:
            pass                                         # a parallel worker won the create race
    priv = vault.load_priv(key_path)
    pub_hex = _pub_hex(priv)                              # always derive from the private key

    conn0 = hubdb.connect(db_path)                        # one-time schema create
    hubdb.init_db(conn0)
    conn0.close()

    app = FastAPI(title='AlphaHub', docs_url=None, redoc_url=None)
    if site_origins:
        from fastapi.middleware.cors import CORSMiddleware
        # exact origins only, never '*': /me answers with account details, and a wildcard would
        # let any page on the internet read them from a visitor's browser.
        app.add_middleware(CORSMiddleware, allow_origins=list(site_origins),
                           allow_methods=['POST'], allow_headers=['Content-Type'])

    def get_db():
        conn = hubdb.connect(db_path)                     # a fresh connection per request (SQLite
        try:                                              # + threadpool: never share across threads)
            yield conn
        finally:
            conn.close()

    def announce(email, name, phone, note):
        """Mail one early-access request to the operator and, only if that worked, stamp it as
        announced. Runs after the response has gone out, on a connection of its own — the
        request-scoped one is already closed by then."""
        body = (f'name:  {(name or "").strip() or "(not given)"}\n'
                f'email: {email}\n'
                f'phone: {(phone or "").strip() or "(not given)"}\n\n'
                f'{(note or "").strip() or "(no note)"}\n\n'
                f'-- invite them with:  admin invite {email} demo\n')
        ok, _ = send_mail(f'AlphaNode early access: {(name or "").strip() or email}',
                          body, reply_to=email)
        if not ok:
            return                                        # leave it in the backlog for `catchup`
        c = hubdb.connect(db_path)
        try:
            hubdb.mark_notified(c, email)
        finally:
            c.close()

    def _account(conn, token):
        user = hubdb.get_user_by_token(conn, token)
        if user is None:
            raise HTTPException(status_code=403, detail='invalid account token')
        return user

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.get('/pub')
    def pub():
        return {'pub': pub_hex}

    @app.get('/pub.txt', response_class=PlainTextResponse)
    def pub_txt():
        """The same key as bare hex — what vault.load_pub() reads. Packaging a client build is
        then one line: curl -s https://api.DOMAIN/pub.txt > alphanode/vault_server_key.pub"""
        return pub_hex + '\n'

    @app.post('/signup')
    def signup(body: SignupIn, conn=Depends(get_db)):
        """A free demo account (3 seats). Ties the demo tier to an identity so it can't be farmed
        anonymously. The token is returned ONLY when the account is freshly created — never for an
        existing email, or anyone knowing a victim's address could POST /signup and walk away with
        their live credential. Token recovery for an existing account needs proof of email
        ownership (a link emailed to the address), which is out of scope for this prototype."""
        email = (body.email or '').strip()
        if len(email) < 3 or '@' not in email or len(email) > 320:
            raise HTTPException(status_code=422, detail='enter a valid email address')
        if hubdb.get_user_by_email(conn, email) is not None:
            # 409, not a 200 with a note: a signup form must not mistake "you already have an
            # account" for success and show the user an empty key box.
            raise HTTPException(status_code=409, detail='account_exists')
        try:
            token = hubdb.apply_payment(conn, email, 'demo', expires_at=None)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {'ok': True, 'token': token, 'plan': 'demo'}

    @app.post('/request-access')
    def request_access(body: AccessRequestIn, bg: BackgroundTasks, conn=Depends(get_db)):
        """The site's early-access form. Stores the request and (if SMTP is configured) mails it
        to the operator. The DB is the source of truth on purpose — mail gets spam-filtered and
        lost, a row does not, and `admin requests` can always list the waitlist.

        Answers 200 for a repeat submit as well: whether an address is already on the list is not
        something a public form should reveal, and the visitor did nothing wrong either way."""
        if body.website:                                  # honeypot filled -> a bot. Look like
            return {'ok': True}                           # success so it stops retrying.
        try:
            hubdb.add_access_request(conn, body.email, name=body.name,
                                     phone=body.phone, note=body.note)
        except ValueError:
            raise HTTPException(status_code=422, detail='enter a valid email address')
        row = hubdb.get_access_request(conn, body.email)
        # Announce whenever this request has never been announced — not merely when it is new.
        # Mail that was off, misconfigured or down when someone applied would otherwise lose them
        # silently; this way a later submit gets another chance, and `admin catchup` clears the
        # rest. The stamp is written only once a send actually succeeds.
        if row is not None and not row['notified_at']:
            bg.add_task(announce, row['email'], row['name'], row['phone'], row['note'])
        return {'ok': True}

    @app.post('/webhook/payment')
    def webhook(body: PaymentIn, conn=Depends(get_db)):
        """The single mutation a payment provider drives. Shared-secret auth (constant-time).
        Provider-agnostic: an adapter maps Paddle/crypto events to {email, plan, expires_at}."""
        import hmac
        if not hmac.compare_digest(body.secret or '', webhook_secret):
            raise HTTPException(status_code=403, detail='bad webhook secret')
        prior = hubdb.get_user_by_email(conn, body.email)   # BEFORE the write: was this a
        was_paid = False                                     # paying account already?
        if prior is not None:
            st0 = hubdb.subscription_state(conn, prior['id'])
            was_paid = st0['plan'] not in (None, 'demo')
        try:
            hubdb.apply_payment(conn, body.email, body.plan,
                                expires_at=body.expires_at, status=body.status)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # /signup is unverified, so anyone can pre-register a stranger's address and sit on the
        # demo token, waiting for the real owner to pay. Minting a FRESH key on the FIRST paid
        # upgrade makes that squat worthless. Renewals must NOT rotate — the customer's key would
        # die every billing cycle — so this fires only on demo/new -> paid.
        token = None
        if body.status == 'active' and body.plan != 'demo' and not was_paid:
            user = hubdb.get_user_by_email(conn, body.email)
            if user is not None:
                token = hubdb.rotate_token(conn, user['id'])
        return {'ok': True, 'token': token}   # deliver it to the buyer (email is not wired yet)

    @app.post('/activate')
    def activate(body: ActivateIn, conn=Depends(get_db)):
        """Node-count gate. Live subscription required; then claim/refresh a seat."""
        user = _account(conn, body.token)
        st = hubdb.subscription_state(conn, user['id'])
        if not st['active']:
            raise HTTPException(status_code=402,
                                detail=f'subscription not active ({st["status"]})')
        ok, reason = hubdb.register_device(conn, user['id'], body.device_id,
                                           st['node_limit'], body.label)
        if not ok:
            raise HTTPException(status_code=409, detail=reason)   # 409: seat conflict
        st = hubdb.subscription_state(conn, user['id'])           # refresh used count
        return {'ok': True, 'plan': st['plan'], 'node_limit': st['node_limit'],
                'used': st['used'], 'expires_at': st['expires_at']}

    @app.post('/reveal')
    def reveal(body: RevealIn, conn=Depends(get_db)):
        """Subscription gate + the actual unseal. Requires a live subscription AND that this
        device already holds a seat (activate first) — so reveals can't dodge the node count."""
        user = _account(conn, body.token)
        st = hubdb.subscription_state(conn, user['id'])
        if not st['active']:
            raise HTTPException(status_code=402,
                                detail=f'subscription not active ({st["status"]})')
        if hubdb.get_device(conn, user['id'], body.device_id) is None:
            raise HTTPException(status_code=409, detail='node not activated on this account')
        try:
            formula = vault.unseal(body.formula_enc, priv)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))   # tampered / wrong key
        hubdb.log_reveal(conn, user['id'], body.device_id, vault.formula_id(formula))
        return {'ok': True, 'formula': formula}

    @app.post('/reveal_batch')
    def reveal_batch(body: RevealBatchIn, conn=Depends(get_db)):
        """Node activation's workhorse: ONE subscription/seat check, many unseals — the client
        opens its whole local library in a round-trip instead of a card-by-card crawl. Per-item
        results (a bad box doesn't fail the batch); each success is audit-logged like /reveal."""
        user = _account(conn, body.token)
        st = hubdb.subscription_state(conn, user['id'])
        if not st['active']:
            raise HTTPException(status_code=402,
                                detail=f'subscription not active ({st["status"]})')
        if hubdb.get_device(conn, user['id'], body.device_id) is None:
            raise HTTPException(status_code=409, detail='node not activated on this account')
        if len(body.formulas) > 2000:
            raise HTTPException(status_code=400, detail='too many formulas in one batch (max 2000)')
        out, opened = [], 0
        for enc in body.formulas:
            if len(enc) > 16384:                         # a real sealed formula is ~1-2 KB; don't
                out.append({'error': 'token too large'})  # base64-decode arbitrary megabytes
                continue
            try:
                formula = vault.unseal(enc, priv)
            except ValueError as e:
                out.append({'error': str(e)})
                continue
            hubdb.log_reveal(conn, user['id'], body.device_id, vault.formula_id(formula))
            out.append({'formula': formula})
            opened += 1
        return {'ok': True, 'count': opened, 'formulas': out}

    @app.post('/me')
    def me(body: MeIn, conn=Depends(get_db)):
        """POST (not GET) so the account token stays in the body, never in a URL that lands in
        access logs / browser history / proxies."""
        user = _account(conn, body.token)
        st = hubdb.subscription_state(conn, user['id'])
        devices = [dict(d) for d in hubdb.list_devices(conn, user['id'])]
        return {'ok': True, 'email': user['email'], **st, 'devices': devices}

    return app


def _pub_hex(priv):
    from cryptography.hazmat.primitives import serialization
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


# module-level app for `uvicorn alphahub.server:app` — built LAZILY (PEP 562) so that merely
# importing this module (tests importing create_app, tooling) never creates a db / key file.
# Only actually resolving `alphahub.server.app` (what uvicorn does) builds it from env.
_app_cache = None


def _env_app():
    db_path = os.environ.get('ALPHAHUB_DB', os.path.join(HERE, 'hub.db'))
    key_path = os.environ.get('ALPHAHUB_VAULT_KEY', os.path.join(HERE, 'vault_key'))
    secret = os.environ.get('ALPHAHUB_WEBHOOK_SECRET')
    if not secret:                                       # fail CLOSED: the webhook grants/cancels
        raise RuntimeError(                              # paid plans — never fall open to a default
            'ALPHAHUB_WEBHOOK_SECRET is required (the payment webhook grants subscriptions). '
            'Set it before starting the server.')
    # ALPHAHUB_SITE_ORIGIN: comma-separated origins the signup form is served from. Unset = no
    # CORS headers at all (fine for a node-only deployment; the desktop app is not a browser).
    origins = [o.strip() for o in (os.environ.get('ALPHAHUB_SITE_ORIGIN') or '').split(',')
               if o.strip()]
    # say it once, at startup: an operator who never sees a request needs to know whether nobody
    # applied or the mail path was never switched on
    cfg = mail_config()
    if cfg:
        print(f'[notify] early-access requests -> {cfg["to"]} via '
              f'{cfg["host"]}:{cfg["port"]} ({cfg["tls"]}) as {cfg["sender"]}', flush=True)
    else:
        print('[notify] OFF: set ALPHAHUB_SMTP_HOST and ALPHAHUB_NOTIFY_TO to get mail. '
              'Requests are still stored — see `admin requests`.', flush=True)
    return create_app(db_path, key_path, secret, site_origins=origins)


def __getattr__(name):
    global _app_cache
    if name == 'app':
        if _app_cache is None:
            _app_cache = _env_app()
        return _app_cache
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
