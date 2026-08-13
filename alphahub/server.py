"""AlphaHub HTTP API (FastAPI). Two gates, as designed:

  * POST /activate  — the NODE-COUNT gate: an account may register up to plan.node_limit distinct
                      machines (seats). Hit on node start / periodically.
  * POST /reveal    — the SUBSCRIPTION gate: unseal a formula only for a live subscription on an
                      already-activated device. This is where the vendor's private key is used.

Plus /signup (free demo account), /webhook/payment (provider-agnostic; Paddle/crypto call it),
/me (account status for the app + web dashboard), /pub (the key the node seals to), /health.

Run:  uvicorn alphahub.server:app --host 127.0.0.1 --port 8790
Config (env): ALPHAHUB_DB, ALPHAHUB_VAULT_KEY, ALPHAHUB_WEBHOOK_SECRET.
NOTE: terminate TLS in front of this (a reverse proxy) — /reveal returns plaintext formulas.
Cap request bodies at that proxy too (e.g. Caddy `request_body max_size 2MB`): FastAPI parses
the whole JSON body before any auth check runs, so the proxy is the real pre-auth size gate.
"""
import os
import sys

from fastapi import Depends, FastAPI, HTTPException
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


def create_app(db_path, key_path, webhook_secret):
    """Build an app bound to explicit config (tests pass scratch paths; the module-level `app`
    below builds this from env). The vault keypair is created on first run if absent."""
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

    def get_db():
        conn = hubdb.connect(db_path)                     # a fresh connection per request (SQLite
        try:                                              # + threadpool: never share across threads)
            yield conn
        finally:
            conn.close()

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

    @app.post('/signup')
    def signup(body: SignupIn, conn=Depends(get_db)):
        """A free demo account (3 seats). Ties the demo tier to an identity so it can't be farmed
        anonymously. The token is returned ONLY when the account is freshly created — never for an
        existing email, or anyone knowing a victim's address could POST /signup and walk away with
        their live credential. Token recovery for an existing account needs proof of email
        ownership (a link emailed to the address), which is out of scope for this prototype."""
        if hubdb.get_user_by_email(conn, body.email) is not None:
            return {'ok': True, 'exists': True,
                    'note': 'account already exists; recover the key via email (not yet wired)'}
        token = hubdb.apply_payment(conn, body.email, 'demo', expires_at=None)
        return {'ok': True, 'token': token, 'plan': 'demo'}

    @app.post('/webhook/payment')
    def webhook(body: PaymentIn, conn=Depends(get_db)):
        """The single mutation a payment provider drives. Shared-secret auth (constant-time).
        Provider-agnostic: an adapter maps Paddle/crypto events to {email, plan, expires_at}."""
        import hmac
        if not hmac.compare_digest(body.secret or '', webhook_secret):
            raise HTTPException(status_code=403, detail='bad webhook secret')
        try:
            hubdb.apply_payment(conn, body.email, body.plan,
                                expires_at=body.expires_at, status=body.status)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {'ok': True}

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
    return create_app(db_path, key_path, secret)


def __getattr__(name):
    global _app_cache
    if name == 'app':
        if _app_cache is None:
            _app_cache = _env_app()
        return _app_cache
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
