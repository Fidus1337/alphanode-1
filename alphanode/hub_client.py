"""AlphaHub client — protocol v2: self-sovereign node identity (Ed25519).

The keypair is generated ON the node at first use and never leaves it — the hub (and the
wire) only ever see the public key. node_id is derived from the public key, so identity
is self-authenticating: nobody, including the hub, can impersonate a node.

Flow: ensure_identity() -> register() once (hub stores the pubkey, operator approves)
-> push() weights before every bar close. See alphahub/protocol.md.

Needs the `cryptography` package (Ed25519). Everything else is stdlib.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

PROTOCOL_V = 2
DEFAULT_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'alphahub_identity')


def _canonical(payload):
    body = {k: v for k, v in payload.items() if k != 'sig'}
    return json.dumps(body, sort_keys=True, separators=(',', ':')).encode()


def _node_id(pk_hex):
    return 'nd_' + hashlib.sha256(bytes.fromhex(pk_hex)).hexdigest()[:16]


def ensure_identity(path=None):
    """Load the node identity, generating it on first call. -> {node_id, sk, pk}.
    The file is 0600: it holds the PRIVATE key. Delete it = a brand-new identity."""
    path = path or os.environ.get('ALPHANODE_HUB_IDENTITY') or DEFAULT_ID_FILE
    try:
        with open(path, encoding='utf-8') as f:
            ident = json.load(f)
        if ident.get('node_id') == _node_id(ident['pk']):
            return ident
    except (OSError, ValueError, KeyError):
        pass
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    sk_hex = sk.private_bytes_raw().hex()
    pk_hex = sk.public_key().public_bytes_raw().hex()
    ident = {'node_id': _node_id(pk_hex), 'sk': sk_hex, 'pk': pk_hex}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(ident, f, indent=2)
    os.chmod(path, 0o600)
    return ident


def sign(ident, payload):
    """Ed25519 signature (hex) over the canonical JSON of the payload without 'sig'."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(ident['sk']))
    return sk.sign(_canonical(payload)).hex()


def _post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def register(hub_url, ident, name, timeout=15):
    """Introduce this node to the hub (idempotent). -> (ok, detail)."""
    payload = {'v': PROTOCOL_V, 'node_id': ident['node_id'], 'name': str(name)[:60],
               'pubkey': ident['pk']}
    payload['sig'] = sign(ident, payload)
    try:
        res = _post(hub_url.rstrip('/') + '/v1/register', payload, timeout)
        return True, ('registered — approved, you are live'
                      if res.get('approved') else
                      'registered — waiting for the hub operator to approve')
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('detail', '')
        except Exception:                                # noqa: BLE001
            detail = ''
        return False, f'hub rejected ({e.code}): {detail}'
    except Exception as e:                               # noqa: BLE001
        return False, f'hub unreachable ({type(e).__name__})'


def push(hub_url, ident, tf, bar_close_iso, weights, timeout=15):
    """POST a signed signal. -> (ok, detail). Never raises — a hub outage must not
    disturb the node; the caller just logs the detail. Weights are the WHOLE story:
    the hub judges nodes by facts (timestamped weights -> its own PnL), never by
    what produced them — formulas stay on this machine."""
    payload = {
        'v': PROTOCOL_V,
        'node_id': ident['node_id'],
        'tf': tf,
        'bar_close_ts': bar_close_iso,
        'weights': {str(s): round(float(w), 6) for s, w in weights.items()
                    if abs(float(w)) > 1e-9},
    }
    if not payload['weights']:
        return False, 'nothing to push (all weights ~0)'
    payload['sig'] = sign(ident, payload)
    try:
        res = _post(hub_url.rstrip('/') + '/v1/signals', payload, timeout)
        return True, ('replaced earlier signal' if res.get('replaces_earlier')
                      else f'accepted at {res.get("received_at", "?")}')
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get('detail', '')
        except Exception:                                # noqa: BLE001
            detail = ''
        return False, f'hub rejected ({e.code}): {detail}'
    except Exception as e:                               # noqa: BLE001 — DNS/timeout/conn refused
        return False, f'hub unreachable ({type(e).__name__})'
