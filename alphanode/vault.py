"""AlphaNode vault: mined formulas are sealed BEFORE they touch the disk (prototype).

The business model this serves: the node mines on the client's machine, but what lands in
library.jsonl is a sealed box only the vault SERVER can open — the client's subscription is
the key to their own catch. The plaintext lives solely in the mining process's memory (it
must: the engine evaluates formulas), so this is DRM economics — it stops the 95% who would
copy a JSON file, not a debugger attached to a running miner. The server never has to be
trusted with keys on the client side: sealing needs only the server's PUBLIC key.

Construction (over the `cryptography` package — no new dependency):
    token = 'v1:' + b64( ephemeral_x25519_pub(32) | nonce(12) | chacha20poly1305_ct )
    key   = HKDF-SHA256( X25519(ephemeral_priv, server_pub),
                         info = 'alphanode-vault-v1' | ephemeral_pub | server_pub )
Binding both public keys into the KDF means a token unseals only with the exact server key
it was sealed to. Every seal uses a fresh ephemeral key and nonce — identical formulas
produce unrelated tokens, so the library file leaks no equality information either.

v2 adds OWNERSHIP: the plaintext becomes {"f": formula, "n": <device_id of the minting
node>}, so the hub can refuse to reveal a box to any account that does not own that node —
a stolen library file is worthless to other subscribers. The owner id rides INSIDE the
AEAD ciphertext (unforgeable after sealing), and v2 uses its own HKDF info string: a v2
token relabeled 'v1:' (or vice versa) derives a different key and fails authentication,
so the ownership check cannot be stripped by downgrading the version prefix. Sealing is
client-side with a public key, so a hostile client can always mint boxes claiming any
owner it wants — that forges nothing: reveal still demands the OWNER's account token, so
the only thing you can do with a forged owner id is give your formulas away to them.
"""
import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                              X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = 'v1:'                                            # legacy: plaintext is the bare formula
MAGIC2 = 'v2:'                                           # owned: plaintext is {"f":…, "n":…}
_INFO = b'alphanode-vault-v1'
_INFO2 = b'alphanode-vault-v2'                           # domain separation kills prefix swaps


def formula_id(formula):
    """Stable public id of a formula (md5 tail, matching the GUI's alpha_ ids): safe to store
    next to the sealed token — it identifies without revealing, and lets the GUI verify that
    what the server revealed is what the miner sealed."""
    return hashlib.md5(formula.encode()).hexdigest()[:12]


def generate_keys(priv_path):
    """New server keypair: raw-hex private key at priv_path (0600), public at priv_path.pub.
    Returns the public key hex. O_EXCL (not O_TRUNC): this is the ONE key that opens every sealed
    formula — refuse to overwrite an existing one rather than silently orphan the whole corpus.
    Raises FileExistsError if priv_path already exists; callers create only when absent."""
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # refuse to clobber
    with os.fdopen(fd, 'w') as f:
        f.write(raw.hex() + '\n')
    with open(priv_path + '.pub', 'w', encoding='utf-8') as f:
        f.write(pub.hex() + '\n')
    return pub.hex()


def load_priv(path):
    with open(path, encoding='utf-8') as f:
        return X25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))


def load_pub(path_or_hex):
    """Server public key from a .pub file path or a bare 64-char hex string -> raw 32 bytes."""
    s = path_or_hex.strip()
    if os.path.exists(s):
        with open(s, encoding='utf-8') as f:
            s = f.read().strip()
    raw = bytes.fromhex(s)
    if len(raw) != 32:
        raise ValueError('vault public key must be 32 bytes')
    return raw


def _derive(shared, eph_pub, srv_pub, info=_INFO):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=info + eph_pub + srv_pub).derive(shared)


def seal(text, server_pub, owner=None):
    """Plaintext formula -> token only the holder of the server PRIVATE key can open.
    server_pub: raw 32 bytes (see load_pub). With `owner` (the minting node's device_id)
    the token is 'v2:' and carries the owner INSIDE the ciphertext — the hub reveals it
    only to the account that owns that node. owner=None keeps the legacy unbound 'v1:'."""
    if isinstance(server_pub, str):
        server_pub = load_pub(server_pub)
    if owner is None:
        payload, magic, info = text.encode(), MAGIC, _INFO
    else:
        payload = json.dumps({'f': text, 'n': str(owner)},
                             separators=(',', ':')).encode()
        magic, info = MAGIC2, _INFO2
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(server_pub))
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_derive(shared, eph_pub, server_pub, info)).encrypt(
        nonce, payload, None)
    return magic + base64.b64encode(eph_pub + nonce + ct).decode()


def unseal_owned(token, server_priv):
    """Token -> (formula, owner). owner is None for legacy 'v1:' boxes, the minting node's
    device_id for 'v2:'. Raises ValueError on a malformed, tampered, or wrong-key token
    (AEAD authentication catches all three) — including a version-prefix swap, because each
    version derives its key with its own HKDF info string."""
    if token.startswith(MAGIC):
        magic, info = MAGIC, _INFO
    elif token.startswith(MAGIC2):
        magic, info = MAGIC2, _INFO2
    else:
        raise ValueError('not a vault token')
    try:
        blob = base64.b64decode(token[len(magic):], validate=True)
    except Exception as e:                               # noqa: BLE001
        raise ValueError('bad token encoding') from e
    if len(blob) < 32 + 12 + 16:                         # eph_pub + nonce + AEAD tag minimum
        raise ValueError('token too short')
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    srv_pub = server_priv.public_key().public_bytes(serialization.Encoding.Raw,
                                                    serialization.PublicFormat.Raw)
    shared = server_priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    try:
        payload = ChaCha20Poly1305(_derive(shared, eph_pub, srv_pub, info)).decrypt(
            nonce, ct, None).decode()
    except Exception as e:                               # noqa: BLE001
        raise ValueError('token does not open with this key (tampered or wrong server)') from e
    if magic == MAGIC:
        return payload, None
    try:
        doc = json.loads(payload)
        formula, owner = doc['f'], doc['n']
        if not (isinstance(formula, str) and isinstance(owner, str) and owner):
            raise ValueError
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError('v2 envelope is malformed') from e
    return formula, owner


def unseal(token, server_priv):
    """Token -> plaintext formula, either version, ownership ignored — for the dev mock and
    vendor-side tools that hold the private key anyway. The hub uses unseal_owned."""
    return unseal_owned(token, server_priv)[0]
