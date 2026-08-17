"""Sealed-envelope crypto invariants for alphanode/vault.py.

Formulas are the product: the miner seals every catch client-side with the server's PUBLIC
key, and a v2 envelope carries {"f": formula, "n": owner_device} inside the AEAD with its
own HKDF info string. These tests guard the failures that would have shipped real damage:

- a v2 token relabeled 'v1:' opening as an UNBOUND box would strip ownership from a stolen
  library file (domain separation is the whole defense — each version must derive a
  different key, so the swap must die in AEAD authentication, not silently succeed);
- a tampered or wrong-server token leaking anything but ValueError would let the hub's
  error handling be confused into revealing state;
- non-fresh ephemerals/nonces would make identical formulas produce related tokens, so
  library.jsonl would leak equality information;
- a falsy-but-not-None owner ('' from an unwritable state dir) minting a 'v2:' box that
  unseal_owned can NEVER open would silently orphan every formula that node mines.
"""
import base64

import pytest

import vault


def _priv(keypair):
    priv_path, _pub_path, _pub = keypair
    return vault.load_priv(priv_path)


def _flip_char(token, index):
    """Replace one base64 character of the token body with a different valid one."""
    c = token[index]
    return token[:index] + ('A' if c != 'A' else 'B') + token[index + 1:]


# ---------------------------------------------------------------- roundtrips

def test_v1_roundtrip_unbound(keypair):
    _, _, pub = keypair
    tok = vault.seal('rank(close/open)', pub)
    assert tok.startswith('v1:')
    assert vault.unseal_owned(tok, _priv(keypair)) == ('rank(close/open)', None)


def test_v2_roundtrip_carries_owner(keypair):
    _, _, pub = keypair
    tok = vault.seal('ts_sum(volume,5)', pub, owner='a' * 16)
    assert tok.startswith('v2:')
    assert vault.unseal_owned(tok, _priv(keypair)) == ('ts_sum(volume,5)', 'a' * 16)


def test_unseal_dev_wrapper_opens_v1(keypair):
    _, _, pub = keypair
    tok = vault.seal('delta(close,3)', pub)
    assert vault.unseal(tok, _priv(keypair)) == 'delta(close,3)'


def test_unseal_dev_wrapper_opens_v2_dropping_owner(keypair):
    _, _, pub = keypair
    tok = vault.seal('delta(close,3)', pub, owner='deadbeef00112233')
    assert vault.unseal(tok, _priv(keypair)) == 'delta(close,3)'


def test_seal_accepts_hex_string_public_key(keypair):
    _, _, pub = keypair
    tok = vault.seal('sign(returns)', pub.hex())          # str path -> load_pub
    assert vault.unseal_owned(tok, _priv(keypair)) == ('sign(returns)', None)


# ------------------------------------------------------- tampering and keys

@pytest.mark.parametrize('owner', [None, 'a' * 16], ids=['v1', 'v2'])
def test_tampered_body_raises_valueerror(keypair, owner):
    _, _, pub = keypair
    tok = vault.seal('close - open', pub, owner=owner)
    with pytest.raises(ValueError):
        vault.unseal_owned(_flip_char(tok, len(tok) // 2), _priv(keypair))


def test_tampered_ephemeral_pub_raises_valueerror(keypair):
    """A flip inside the first 32 bytes (the ephemeral pub, bound into the HKDF info)
    must also fail — the KDF binding, not just the AEAD tag, covers the key material."""
    _, _, pub = keypair
    tok = vault.seal('close - open', pub)
    with pytest.raises(ValueError):
        vault.unseal_owned(_flip_char(tok, len('v1:') + 4), _priv(keypair))


def test_wrong_server_key_raises_valueerror(keypair, tmp_path):
    _, _, pub = keypair
    tok = vault.seal('log(volume)', pub)
    other_priv_path = str(tmp_path / 'other_key')
    vault.generate_keys(other_priv_path)
    with pytest.raises(ValueError):
        vault.unseal_owned(tok, vault.load_priv(other_priv_path))


# ------------------------------------------------ version-prefix domain separation

def test_relabel_v2_as_v1_cannot_strip_ownership(keypair):
    _, _, pub = keypair
    tok = vault.seal('ts_rank(close,10)', pub, owner='a' * 16)
    assert tok.startswith('v2:')
    downgraded = 'v1:' + tok[len('v2:'):]
    with pytest.raises(ValueError):
        vault.unseal_owned(downgraded, _priv(keypair))


def test_relabel_v1_as_v2_raises(keypair):
    _, _, pub = keypair
    tok = vault.seal('ts_rank(close,10)', pub)
    assert tok.startswith('v1:')
    upgraded = 'v2:' + tok[len('v1:'):]
    with pytest.raises(ValueError):
        vault.unseal_owned(upgraded, _priv(keypair))


# ---------------------------------------------------------------- junk input

@pytest.mark.parametrize('junk', ['', 'zzz', 'v3:AAAA'])
def test_unrecognized_prefix_raises_not_a_vault_token(keypair, junk):
    with pytest.raises(ValueError, match='not a vault token'):
        vault.unseal_owned(junk, _priv(keypair))


def test_invalid_base64_body_raises_valueerror(keypair):
    with pytest.raises(ValueError, match='bad token encoding'):
        vault.unseal_owned('v1:%%%%', _priv(keypair))


def test_truncated_blob_raises_valueerror(keypair):
    short = 'v1:' + base64.b64encode(b'\x00' * 20).decode()
    with pytest.raises(ValueError, match='token too short'):
        vault.unseal_owned(short, _priv(keypair))


# ---------------------------------------------------------------- formula_id

def test_formula_id_deterministic_shape_and_distinct():
    fid = vault.formula_id('rank(close/open)')
    assert fid == vault.formula_id('rank(close/open)')     # deterministic
    assert len(fid) == 12
    assert fid == fid.lower()
    assert all(c in '0123456789abcdef' for c in fid)
    assert fid != vault.formula_id('rank(open/close)')


# ----------------------------------------------------------- falsy owner = v1

def test_seal_owner_none_is_v1(keypair):
    _, _, pub = keypair
    assert vault.seal('close', pub, owner=None).startswith('v1:')


def test_seal_owner_empty_string_is_v1_unbound(keypair):
    """Regression: seal once gated on `owner is None`, so owner='' minted a 'v2:' box whose
    empty owner failed unseal_owned's validation — sealed fine, openable by NO ONE, ever.
    A falsy owner must mean 'unbound v1', exactly like the node's `_device_id() or None`."""
    _, _, pub = keypair
    tok = vault.seal('close', pub, owner='')
    assert tok.startswith('v1:')
    assert vault.unseal_owned(tok, _priv(keypair)) == ('close', None)


# --------------------------------------------------------------- no equality leak

def test_identical_formulas_seal_to_unrelated_tokens(keypair):
    """Fresh ephemeral + nonce per seal: the library file must not leak which sealed
    boxes contain the same formula."""
    _, _, pub = keypair
    a = vault.seal('close/open', pub)
    b = vault.seal('close/open', pub)
    assert a != b
    body_a = base64.b64decode(a[len('v1:'):])
    body_b = base64.b64decode(b[len('v1:'):])
    assert body_a[:32] != body_b[:32]                      # ephemeral pubs differ
    assert body_a[32:44] != body_b[32:44]                  # nonces differ
