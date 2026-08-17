"""The node's sealing gate, exercised through REAL subprocesses.

The gate runs at `import node` time, so every case gets a fresh interpreter with a
controlled environment. These tests guard the invariants whose violation actually
shipped: builds silently mined PLAINTEXT libraries whenever the GUI failed to resolve
the vault key and passed no ALPHANODE_VAULT_PUB — an env-resolution bug became a full
crypto downgrade. The fix has two halves, both pinned here:

  1. the node resolves the BUNDLED key itself — a clean/wiped environment still seals
     to the vendor key (fp 64bb2be8754fdffa), so `unset ALPHANODE_VAULT_PUB` is not an
     unseal button (cases 1, 2);
  2. OPEN (plaintext) mining requires the node's OWN successful hub verification —
     flag alone, refused licence, or unreachable hub all fail CLOSED back to sealing
     (cases 2-5). Case 6 pins the dev/self-host override: an explicit VAULT_PUB seals
     to THAT key, visibly (its fingerprint in the log).
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, '.venv', 'bin', 'python')
REPO_KEY_FP = '64bb2be8754fdffa'                          # sha256(vault_server_key.pub)[:16]
SEAL_LINE = '[vault] sealing to key '
PROBE = ("import sys; sys.path[:0]=['alphanode','evolution','.']; "
         "import node; print('PUBSET', node.VAULT_PUB is not None)")


def run_node(tmp_path, **overrides):
    """Import node in a fresh interpreter: sandboxed state dir, all ALPHANODE_VAULT_*
    scrubbed unless the case sets them. Returns combined stdout+stderr."""
    env = os.environ.copy()
    for k in ('ALPHANODE_VAULT_PUB', 'ALPHANODE_VAULT_OPEN',
              'ALPHANODE_VAULT_LICENSE', 'ALPHANODE_VAULT_URL'):
        env.pop(k, None)
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    env['ALPHANODE_STATE_DIR'] = str(state)
    env.update(overrides)
    proc = subprocess.run([PY, '-c', PROBE], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f'node import died (rc={proc.returncode}):\n{out}'
    return out


class _ActivateHandler(BaseHTTPRequestHandler):
    """Minimal AlphaHub stand-in: answers POST /activate with a canned verdict."""
    ok = True
    seen = None                                          # list shared with the test

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        if self.seen is not None:
            self.seen.append((self.path, body))
        payload = json.dumps({'ok': self.__class__.ok}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):                           # keep pytest output clean
        pass


@contextmanager
def fake_hub(ok):
    """A live localhost hub in this test process; yields (base_url, seen_requests)."""
    seen = []
    handler = type('H', (_ActivateHandler,), {'ok': ok, 'seen': seen})
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}', seen
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _closed_port():
    """A port that was just free — connecting to it gets ECONNREFUSED."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------- cases


def test_clean_env_seals_to_bundled_repo_key(tmp_path):
    """The regression that shipped: no env hint at all must STILL seal — the node finds
    the bundled vendor key on its own."""
    out = run_node(tmp_path)
    assert SEAL_LINE + REPO_KEY_FP in out, out
    assert 'PUBSET True' in out, out


def test_open_flag_without_licence_stays_sealed_offline(tmp_path):
    """OPEN=1 alone is not a privilege. With no licence token the node must not even
    try the hub (URL points at a dead port to prove no network is involved)."""
    out = run_node(tmp_path, ALPHANODE_VAULT_OPEN='1',
                   ALPHANODE_VAULT_URL='http://127.0.0.1:1')
    assert SEAL_LINE + REPO_KEY_FP in out, out
    assert 'PUBSET True' in out, out
    assert '[vault] open-mining check' not in out, out    # gate closed before any request


def test_open_with_licence_and_active_hub_mines_plaintext(tmp_path):
    """The one legitimate unsealed path: flag + licence + the node's OWN hub check
    coming back ok. No sealing line, VAULT_PUB stays None."""
    with fake_hub(ok=True) as (url, seen):
        out = run_node(tmp_path, ALPHANODE_VAULT_OPEN='1',
                       ALPHANODE_VAULT_LICENSE='tok', ALPHANODE_VAULT_URL=url)
    assert 'subscription active' in out, out
    assert SEAL_LINE not in out, out
    assert 'PUBSET False' in out, out
    assert seen and seen[0][0] == '/activate', seen       # the node really asked the hub


def test_hub_refusal_falls_back_to_sealing(tmp_path):
    """Hub says no -> the flag and token are worthless; seal to the bundled key."""
    with fake_hub(ok=False) as (url, _seen):
        out = run_node(tmp_path, ALPHANODE_VAULT_OPEN='1',
                       ALPHANODE_VAULT_LICENSE='tok', ALPHANODE_VAULT_URL=url)
    assert 'REFUSED' in out, out
    assert SEAL_LINE + REPO_KEY_FP in out, out
    assert 'PUBSET True' in out, out


def test_unreachable_hub_fails_closed(tmp_path):
    """Hub down (connection refused) must read as 'not verified', never as 'open'."""
    out = run_node(tmp_path, ALPHANODE_VAULT_OPEN='1', ALPHANODE_VAULT_LICENSE='tok',
                   ALPHANODE_VAULT_URL=f'http://127.0.0.1:{_closed_port()}')
    assert 'check failed' in out, out
    assert SEAL_LINE + REPO_KEY_FP in out, out
    assert 'PUBSET True' in out, out


def test_explicit_vault_pub_env_seals_to_that_key(tmp_path):
    """Dev/self-host override: ALPHANODE_VAULT_PUB wins over the bundled key, and the
    log names the substituted key's fingerprint so the swap is visible.

    NOTE: in a FROZEN release the build-stamp pin (buildinfo vault_pub_fp) makes this
    exact substitution FATAL (exit 3) — that branch only exists under sys.frozen and
    is covered by the packaged selfcheck, not runnable from an unfrozen source tree.
    """
    sys.path[:0] = [os.path.join(ROOT, 'alphanode')]
    import vault
    priv = str(tmp_path / 'other_key')
    vault.generate_keys(priv)
    pub_path = priv + '.pub'
    other_fp = hashlib.sha256(vault.load_pub(pub_path)).hexdigest()[:16]
    assert other_fp != REPO_KEY_FP                        # sanity: a genuinely new key

    out = run_node(tmp_path, ALPHANODE_VAULT_PUB=pub_path)
    assert SEAL_LINE + other_fp in out, out
    assert REPO_KEY_FP not in out, out
    assert 'PUBSET True' in out, out
