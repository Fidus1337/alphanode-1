"""Stamp this build with an identity, written just before PyInstaller runs.

One shipped binary serves every customer (public GitHub Release), so there is no per-buyer
build to watermark. What IS achievable — and what this gives — is per-BUILD provenance: a
leaked binary or a stolen library carries a build_id that the hub logs at /activate and
/reveal, and the device->account claims ledger ties that activation to a person. So the
trace is (leaked artifact -> build_id in its stamp) x (build_id + account in the hub log).

Also records the fingerprint of the vault public key the build shipped with, so --role
selfcheck can refuse a bundle whose key was swapped or corrupted (tamper-EVIDENT, not
tamper-proof: a determined attacker patches the check out — the honest limit, same as the
sealed-box DRM, which a debugger on the running miner already defeats).

Written to alphanode/_build_stamp.json (gitignored — generated per build). Absent in dev
runs, where buildinfo falls back to a 'dev' identity.

Also bakes ALPHANODE_VAULT_URL into the stamp, so the shipped app knows its licence server on
EVERY platform — the Linux launchers export the env var, but the Windows .exe runs with no
wrapper, so the stamp is the only place a Windows build can carry it. The app prefers an
explicit env var (self-host / dev), then the stamp, then the localhost default.

Usage:  python packaging/make_build_stamp.py
Env:    ALPHANODE_VAULT_URL (the hub the build talks to), ALPHANODE_BUILD_ID (override; CI
        passes the run id), GITHUB_SHA (CI git sha).
"""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
APP = os.path.join(PROJ, 'alphanode')
STAMP = os.path.join(APP, '_build_stamp.json')

sys.path.insert(0, APP)
from version import __version__                             # noqa: E402


def _git_sha():
    sha = os.environ.get('GITHUB_SHA')
    if sha:
        return sha[:12]
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short=12', 'HEAD'],
                                       cwd=PROJ, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:                                       # noqa: BLE001 — not a git checkout
        return 'nogit'


def _vault_pub_fp():
    """sha256(raw 32-byte key)[:16] of the .pub the build will ship, or None if unsealed."""
    pub = os.path.join(APP, 'vault_server_key.pub')
    try:
        raw = bytes.fromhex(open(pub, encoding='utf-8').read().strip())
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()[:16]


def main():
    stamp = {
        'version': __version__,
        'build_id': (os.environ.get('ALPHANODE_BUILD_ID') or uuid.uuid4().hex)[:16],
        'git': _git_sha(),
        'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'vault_pub_fp': _vault_pub_fp(),
        'vault_url': (os.environ.get('ALPHANODE_VAULT_URL') or '').strip() or None,
    }
    with open(STAMP, 'w', encoding='utf-8') as f:
        json.dump(stamp, f, indent=2, sort_keys=True)
    print(f'build stamp: v{stamp["version"]} build {stamp["build_id"]} '
          f'git {stamp["git"]} key {stamp["vault_pub_fp"] or "UNSEALED"} '
          f'hub {stamp["vault_url"] or "LOCALHOST (dev)"}')


if __name__ == '__main__':
    main()
