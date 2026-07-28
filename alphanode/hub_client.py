"""Thin AlphaHub push client — protocol v1, stdlib only (hmac + urllib).

The hub is a forward-track notary: it accepts target weights strictly BEFORE the bar
closes and scores them on the next bar (see alphahub/protocol.md). This module only
builds, signs and posts the payload; deciding WHAT to push stays with the caller.

Credentials: node_id travels in the payload, the secret only signs it. The secret is
stored like the Anthropic key — its own 0600 file, never in gui_settings.json/git.
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.request

PROTOCOL_V = 1


def sign(secret, payload):
    """HMAC-SHA256 over the canonical JSON of the payload without 'sig' (hex)."""
    body = {k: v for k, v in payload.items() if k != 'sig'}
    canon = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(secret.encode(), canon, hashlib.sha256).hexdigest()


def build_payload(node_id, secret, tf, bar_close_iso, weights):
    """-> signed payload dict ready to POST. Weights are rounded to 6 decimals so the
    signed bytes are exactly what lands in the ledger (float repr surprises excluded)."""
    payload = {
        'v': PROTOCOL_V,
        'node_id': node_id,
        'tf': tf,
        'bar_close_ts': bar_close_iso,
        'weights': {str(s): round(float(w), 6) for s, w in weights.items()
                    if abs(float(w)) > 1e-9},
    }
    payload['sig'] = sign(secret, payload)
    return payload


def push(hub_url, node_id, secret, tf, bar_close_iso, weights, timeout=15):
    """POST the signal. -> (ok: bool, detail: str). Never raises — a hub outage must
    not disturb the node; the caller just logs the detail."""
    payload = build_payload(node_id, secret, tf, bar_close_iso, weights)
    if not payload['weights']:
        return False, 'nothing to push (all weights ~0)'
    req = urllib.request.Request(
        hub_url.rstrip('/') + '/v1/signals',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read())
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
