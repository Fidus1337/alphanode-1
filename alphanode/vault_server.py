"""Mock vault server (prototype): the ONLY holder of the key that opens locked formulas.

    python alphanode/vault_server.py [--port 8790] [--key <path>]

    GET  /pub     -> {"pub": "<hex>"}                     # miners seal to this key
    POST /reveal  -> {"token": "v1:...", "license": "..."}
                  -> {"ok": true, "formula": "..."}       # subscription valid
                  -> {"ok": false, "error": "..."}        # denied / bad token

The license check is a deliberate STUB (the demo accepts the literal key 'demo'): the real
server would verify an Ed25519-signed subscription document — user, plan, expiry, machine
binding — before unsealing, and meter/reveal per the client's tier. Everything else (the
sealed-box crypto, the reveal flow, the GUI contract) is the real shape.

The keypair is auto-generated on first run next to this file (vault_server_key + .pub),
0600 and gitignored — same handling as the node's other secrets.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import serialization

import vault

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 64 * 1024                                     # a /reveal body is a tiny token — cap it
DEMO_LICENSE = 'demo'                                    # stub: the real server checks a signed doc


def load_or_create_keys(key_path):
    if not os.path.exists(key_path):
        pub = vault.generate_keys(key_path)
        print(f'new vault keypair at {key_path} (pub {pub[:16]}…)')
    priv = vault.load_priv(key_path)
    # derive the advertised public key FROM the private key, never from the .pub file: a stale
    # or missing .pub would otherwise make /pub hand out a key whose private half nobody holds,
    # and every formula sealed to it would be lost forever.
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub_hex


def make_handler(priv, pub_hex):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                # noqa: N802
            if self.path == '/pub':
                self._json(200, {'pub': pub_hex})
            else:
                self._json(404, {'ok': False, 'error': 'unknown endpoint'})

        def do_POST(self):                               # noqa: N802
            if self.path != '/reveal':
                self._json(404, {'ok': False, 'error': 'unknown endpoint'})
                return
            try:
                n = int(self.headers.get('Content-Length', 0))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:                     # negative would read(-1) and pin the thread;
                self._json(400, {'ok': False, 'error': 'bad or oversized body'})   # huge would balloon RAM
                return
            try:
                req = json.loads(self.rfile.read(n).decode())
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {'ok': False, 'error': 'bad request body'})
                return
            if req.get('license') != DEMO_LICENSE:       # <- real server: verify signed subscription
                self._json(403, {'ok': False, 'error': 'license invalid or expired'})
                return
            try:
                formula = vault.unseal(str(req.get('token', '')), priv)
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
                return
            self._json(200, {'ok': True, 'formula': formula})

        def log_message(self, fmt, *args):               # quiet default access log -> one line
            print(f'{self.address_string()} {fmt % args}')
    return Handler


def main():
    ap = argparse.ArgumentParser(description='AlphaNode vault server (prototype)')
    ap.add_argument('--port', type=int, default=int(os.environ.get('ALPHANODE_VAULT_PORT', 8790)))
    ap.add_argument('--key', default=os.path.join(HERE, 'vault_server_key'))
    args = ap.parse_args()
    priv, pub_hex = load_or_create_keys(args.key)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(priv, pub_hex))
    print(f'vault server on http://127.0.0.1:{args.port}  (pub {pub_hex[:16]}…, license stub: '
          f"'{DEMO_LICENSE}')")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
