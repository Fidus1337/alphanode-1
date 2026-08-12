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
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                              X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = 'v1:'
_INFO = b'alphanode-vault-v1'


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


def _derive(shared, eph_pub, srv_pub):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INFO + eph_pub + srv_pub).derive(shared)


def seal(text, server_pub):
    """Plaintext formula -> 'v1:...' token that only the holder of the server PRIVATE key
    can open. server_pub: raw 32 bytes (see load_pub)."""
    if isinstance(server_pub, str):
        server_pub = load_pub(server_pub)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(server_pub))
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(_derive(shared, eph_pub, server_pub)).encrypt(
        nonce, text.encode(), None)
    return MAGIC + base64.b64encode(eph_pub + nonce + ct).decode()


def unseal(token, server_priv):
    """'v1:...' token -> plaintext formula. Raises ValueError on a malformed, tampered,
    or wrong-key token (AEAD authentication catches all three)."""
    if not token.startswith(MAGIC):
        raise ValueError('not a vault token')
    try:
        blob = base64.b64decode(token[len(MAGIC):], validate=True)
    except Exception as e:                               # noqa: BLE001
        raise ValueError('bad token encoding') from e
    if len(blob) < 32 + 12 + 16:                         # eph_pub + nonce + AEAD tag minimum
        raise ValueError('token too short')
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    srv_pub = server_priv.public_key().public_bytes(serialization.Encoding.Raw,
                                                    serialization.PublicFormat.Raw)
    shared = server_priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    try:
        return ChaCha20Poly1305(_derive(shared, eph_pub, srv_pub)).decrypt(
            nonce, ct, None).decode()
    except Exception as e:                               # noqa: BLE001
        raise ValueError('token does not open with this key (tampered or wrong server)') from e
