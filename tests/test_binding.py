"""Formula<->account binding on the hub: the whole attack matrix.

Invariants guarded (each maps to a real way a paying customer's mined library could be
stolen or a freeloader could ride someone else's subscription):

  * v2 sealed boxes carry their minting node INSIDE the AEAD ciphertext — relabeling the
    version prefix ('v2:'->'v1:') must NOT strip the ownership check (a downgrade here
    would make every stolen library revealable as "legacy").
  * The first account to /activate a device_id owns it forever (device_claims ledger):
    re-registering a stolen state dir (device_id + library ride together) under a thief's
    subscription must 409, and the claim must survive seat removal and plan churn —
    ownership is not a billing artifact.
  * /reveal opens a v2 box only for the claim-holding account (any of its seats), 403s
    every other account, 403s boxes minted by never-activated nodes, and honors the
    ALPHAHUB_V1_REVEAL=deny kill switch without breaking v2.
  * Subscription gates stay in front of everything: canceled -> 402, unknown token -> 403,
    unactivated device -> 409, seat limit -> 409.
  * The support path (release_claim / `admin release-node`) is the ONE way ownership moves,
    and the migration backfill rebuilds claims for pre-ledger databases (without it, every
    existing customer's boxes would 403 as "never activated" after the upgrade).
  * node._device_id() is stable across processes — an id that drifted per-process would
    scatter one machine's boxes across many phantom "owners".

Ported from the ad-hoc binding matrix (scratchpad/binding_test.py, ~27 checks).
"""
import os
import subprocess
import sys

import pytest

import vault
from alphahub import db as hubdb
from alphahub.server import create_app
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORMULA = 'cs_scale(ts_mean:5(close))'
DA, DA2, DB = 'a1' * 8, 'a2' * 8, 'b1' * 8          # alice's two nodes, bob's one
ORPHAN = 'ffff0000ffff0000'                          # a device_id that never activated


# ---- helpers ----
def _activate(client, token, device, **extra):
    return client.post('/activate', json={'token': token, 'device_id': device, **extra})


def _reveal(client, token, device, enc):
    return client.post('/reveal', json={'token': token, 'device_id': device,
                                        'formula_enc': enc})


def _two_accounts(client, conn):
    """The standard scene: alice (seats DA, DA2) and bob (seat DB), all activated."""
    tok_a = hubdb.apply_payment(conn, 'alice@x.io', 'demo')
    tok_b = hubdb.apply_payment(conn, 'bob@x.io', 'demo')
    for tok, dev in ((tok_a, DA), (tok_a, DA2), (tok_b, DB)):
        r = _activate(client, tok, dev)
        assert r.status_code == 200, r.text
    return tok_a, tok_b


# ---- crypto layer: envelopes, downgrade, tamper ----
def test_v1_and_v2_seal_roundtrip(keypair):
    _priv_path, _pub_path, pub = keypair
    priv = vault.load_priv(_priv_path)
    t1 = vault.seal(FORMULA, pub)                            # legacy unbound
    t2 = vault.seal(FORMULA, pub, owner='aabbccdd00112233')  # owned
    assert t1.startswith('v1:')
    assert t2.startswith('v2:')
    assert vault.unseal_owned(t1, priv) == (FORMULA, None)
    assert vault.unseal_owned(t2, priv) == (FORMULA, 'aabbccdd00112233')
    # the ownership-blind wrapper (vendor tools) opens both
    assert vault.unseal(t1, priv) == FORMULA
    assert vault.unseal(t2, priv) == FORMULA


def test_version_prefix_swap_rejected(keypair):
    """The downgrade attack: relabel a v2 box 'v1:' and the ownership check would vanish —
    domain-separated HKDF info strings must make either swap fail authentication."""
    priv = vault.load_priv(keypair[0])
    t1 = vault.seal(FORMULA, keypair[2])
    t2 = vault.seal(FORMULA, keypair[2], owner='aabbccdd00112233')
    with pytest.raises(ValueError):
        vault.unseal_owned('v1:' + t2[3:], priv)             # v2 relabeled v1 (downgrade)
    with pytest.raises(ValueError):
        vault.unseal_owned('v2:' + t1[3:], priv)             # v1 relabeled v2 (upgrade)


def test_tampered_v2_rejected(keypair):
    priv = vault.load_priv(keypair[0])
    t2 = vault.seal(FORMULA, keypair[2], owner='aabbccdd00112233')
    forged = t2[:-8] + ('AAAAAAA=' if t2[-8:] != 'AAAAAAA=' else 'BBBBBBB=')
    with pytest.raises(ValueError):
        vault.unseal_owned(forged, priv)


# ---- /activate: registration, claims, seat limits ----
def test_hub_pub_matches_fixture_keypair(hub, keypair):
    """Every other test seals to keypair's pub — prove that IS the hub's key."""
    client, _conn = hub
    assert client.get('/pub').json()['pub'] == keypair[2].hex()


def test_activate_registers_device_and_creates_claim(hub):
    client, conn = hub
    tok = hubdb.apply_payment(conn, 'alice@x.io', 'demo')
    r = _activate(client, tok, DA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['ok'] is True
    assert body['plan'] == 'demo'
    assert body['node_limit'] == 3
    assert body['used'] == 1
    alice_id = hubdb.get_user_by_email(conn, 'alice@x.io')['id']
    claim = hubdb.get_claim(conn, DA)
    assert claim is not None, 'activation must write the permanent device_claims row'
    assert claim['user_id'] == alice_id
    assert hubdb.get_device(conn, alice_id, DA) is not None


def test_activate_same_device_twice_consumes_one_seat(hub):
    client, conn = hub
    tok = hubdb.apply_payment(conn, 'alice@x.io', 'demo')
    assert _activate(client, tok, DA).status_code == 200
    r = _activate(client, tok, DA)                           # re-activation: known device
    assert r.status_code == 200
    assert r.json()['used'] == 1


def test_activate_seat_limit_enforced(hub):
    client, conn = hub
    tok = hubdb.apply_payment(conn, 'alice@x.io', 'demo')    # demo: 3 seats
    for i in range(3):
        assert _activate(client, tok, f'd{i}' * 8).status_code == 200
    r = _activate(client, tok, 'd9' * 8)
    assert r.status_code == 409
    assert 'node limit reached' in r.json()['detail']


def test_activate_unknown_token_rejected(hub):
    client, _conn = hub
    r = _activate(client, 'no-such-token', DA)
    assert r.status_code == 403
    assert 'invalid account token' in r.json()['detail']


def test_activate_refuses_device_claimed_by_another_account(hub):
    """The stolen-state-dir move: copy a victim's library + device_id, activate it under
    your own subscription. The claims ledger must refuse the re-registration outright."""
    client, conn = hub
    tok_a, tok_b = _two_accounts(client, conn)
    r = _activate(client, tok_b, DA)                         # bob claims alice's node
    assert r.status_code == 409
    assert 'another account' in r.json()['detail']


# ---- /reveal: the ownership gate ----
def test_reveal_owner_account_minting_device(hub, keypair):
    client, conn = hub
    tok_a, _tok_b = _two_accounts(client, conn)
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    r = _reveal(client, tok_a, DA, box_a)
    assert r.status_code == 200, r.text
    assert r.json()['formula'] == FORMULA


def test_reveal_owner_account_sibling_device(hub, keypair):
    """Mine on the desktop, reveal on the laptop: any of the OWNER's seats may open it."""
    client, conn = hub
    tok_a, _tok_b = _two_accounts(client, conn)
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    r = _reveal(client, tok_a, DA2, box_a)
    assert r.status_code == 200, r.text
    assert r.json()['formula'] == FORMULA


def test_reveal_foreign_account_403(hub, keypair):
    """The core theft scenario: bob got hold of a box minted by alice's node."""
    client, conn = hub
    _tok_a, tok_b = _two_accounts(client, conn)
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    r = _reveal(client, tok_b, DB, box_a)
    assert r.status_code == 403
    assert "another account" in r.json()['detail']


def test_reveal_owner_never_activated_403(hub, keypair):
    client, conn = hub
    tok_a, _tok_b = _two_accounts(client, conn)
    box_orphan = vault.seal(FORMULA, keypair[2], owner=ORPHAN)
    r = _reveal(client, tok_a, DA, box_orphan)
    assert r.status_code == 403
    assert 'never activated' in r.json()['detail']


def test_reveal_v1_legacy_allowed_by_default(hub, keypair):
    client, conn = hub
    tok_a, _tok_b = _two_accounts(client, conn)
    box_v1 = vault.seal(FORMULA, keypair[2])                 # unbound legacy
    r = _reveal(client, tok_a, DA, box_v1)
    assert r.status_code == 200, r.text
    assert r.json()['formula'] == FORMULA


def test_reveal_requires_activated_seat(hub, keypair):
    """Reveals must not dodge the node-count gate: an unactivated device gets 409."""
    client, conn = hub
    tok = hubdb.apply_payment(conn, 'dave@x.io', 'demo')
    box = vault.seal(FORMULA, keypair[2])
    r = _reveal(client, tok, 'd4' * 8, box)
    assert r.status_code == 409
    assert 'not activated' in r.json()['detail']


def test_canceled_subscription_gates_reveal_and_activate(hub, keypair):
    client, conn = hub
    tok = hubdb.apply_payment(conn, 'carol@x.io', 'demo')
    dev = 'c1' * 8
    assert _activate(client, tok, dev).status_code == 200
    box = vault.seal(FORMULA, keypair[2], owner=dev)
    hubdb.apply_payment(conn, 'carol@x.io', 'demo', status='canceled')
    r = _reveal(client, tok, dev, box)
    assert r.status_code == 402
    assert 'not active' in r.json()['detail']
    r = _activate(client, tok, dev)
    assert r.status_code == 402


def test_v1_kill_switch_denies_legacy_only(hub, keypair, tmp_path, monkeypatch):
    """ALPHAHUB_V1_REVEAL=deny (a SECOND app over the same db/key) kills unbound v1 boxes
    with the re-mine message while v2 keeps working."""
    client, conn = hub
    tok_a, _tok_b = _two_accounts(client, conn)
    box_v1 = vault.seal(FORMULA, keypair[2])
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    db_path = tmp_path / 'hub.db'                            # same file the hub fixture uses
    assert db_path.exists(), 'hub fixture layout changed — update this test'
    monkeypatch.setenv('ALPHAHUB_V1_REVEAL', 'deny')
    app2 = create_app(str(db_path), keypair[0], webhook_secret='test-secret')
    with TestClient(app2) as deny:
        r = _reveal(deny, tok_a, DA, box_v1)
        assert r.status_code == 403
        assert 're-mine' in r.json()['detail']
        r = _reveal(deny, tok_a, DA, box_a)                  # v2 unaffected by the switch
        assert r.status_code == 200, r.text


# ---- /reveal_batch: per-item verdicts ----
def test_reveal_batch_mixed_per_item_verdicts(hub, keypair):
    client, conn = hub
    _tok_a, tok_b = _two_accounts(client, conn)
    pub = keypair[2]
    r = client.post('/reveal_batch', json={'token': tok_b, 'device_id': DB, 'formulas': [
        vault.seal(FORMULA, pub, owner=DB),                  # bob's own
        vault.seal(FORMULA, pub, owner=DA),                  # alice's
        vault.seal(FORMULA, pub),                            # legacy v1
        'v2:not-base64!!',                                   # garbage
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['ok'] is True
    items = body['formulas']
    assert items[0].get('formula') == FORMULA                # own opens
    assert 'another account' in items[1].get('error', '')    # alice's refused
    assert items[2].get('formula') == FORMULA                # v1 opens (default allow)
    assert 'error' in items[3]                               # garbage: error, batch alive
    assert body['count'] == 2                                # count == OPENED only


# ---- claim permanence and the support path ----
def test_claim_outlives_the_seat(hub, keypair):
    """Unseating a device (downgrade prune, admin unseat) must NOT free its claim: bob still
    can't take it, and alice still opens its boxes through her remaining live seat."""
    client, conn = hub
    tok_a, tok_b = _two_accounts(client, conn)
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    alice_id = hubdb.get_user_by_email(conn, 'alice@x.io')['id']
    assert hubdb.remove_device(conn, alice_id, DA)
    r = _activate(client, tok_b, DA)
    assert r.status_code == 409
    assert 'another account' in r.json()['detail']
    r = _reveal(client, tok_a, DA2, box_a)
    assert r.status_code == 200, r.text


def test_release_claim_frees_device_for_next_account(hub, keypair):
    """release_claim is the ONE legitimate transfer: after it, bob claims the device and —
    the documented support-path consequence — opens the boxes it minted."""
    client, conn = hub
    _tok_a, tok_b = _two_accounts(client, conn)
    box_a = vault.seal(FORMULA, keypair[2], owner=DA)
    assert _activate(client, tok_b, DA).status_code == 409   # locked before release
    assert hubdb.release_claim(conn, DA) is True
    r = _activate(client, tok_b, DA)
    assert r.status_code == 200, r.text
    r = _reveal(client, tok_b, DA, box_a)
    assert r.status_code == 200, r.text
    assert hubdb.release_claim(conn, 'nosuchnode00') is False


def test_admin_nodes_and_release_node_cli(hub, keypair, tmp_path):
    """The operator's view of the same ledger: `admin nodes` lists claims, `admin
    release-node` frees one, and releasing an unknown node fails loudly (exit != 0)."""
    client, conn = hub
    _tok_a, tok_b = _two_accounts(client, conn)

    def admin(*args):
        env = dict(os.environ, ALPHAHUB_DB=str(tmp_path / 'hub.db'))
        return subprocess.run([sys.executable, '-m', 'alphahub.admin', *args],
                              env=env, cwd=ROOT, capture_output=True, text=True)

    p = admin('nodes')
    assert p.returncode == 0, p.stderr
    assert DA in p.stdout and 'alice@x.io' in p.stdout
    p = admin('release-node', DA)
    assert p.returncode == 0 and 'released' in p.stdout, p.stdout + p.stderr
    assert _activate(client, tok_b, DA).status_code == 200
    p = admin('release-node', 'nosuchnode00')
    assert p.returncode != 0


# ---- migration backfill ----
def test_migration_backfills_claims_from_existing_seats(hub):
    """A pre-ledger database (device_claims empty, devices populated) must come out of
    init_db with every existing seat claimed by its holder — otherwise the upgrade would
    403 every existing customer's own boxes as 'never activated'."""
    client, conn = hub
    _tok_a, _tok_b = _two_accounts(client, conn)
    alice_id = hubdb.get_user_by_email(conn, 'alice@x.io')['id']
    conn.execute('DELETE FROM device_claims')                # simulate pre-ledger db
    conn.commit()
    hubdb.init_db(conn)                                      # _migrate backfills
    rows = {r['device_id'] for r in hubdb.list_claims(conn)}
    assert {DA, DA2, DB} <= rows
    assert hubdb.get_claim(conn, DA)['user_id'] == alice_id


# ---- node-side device id: what makes claims meaningful ----
def test_node_device_id_stable_across_processes(tmp_path):
    """Two separate node processes over one state dir must agree on ONE token_hex(8) id —
    a per-process id would scatter a machine's boxes across phantom owners."""
    sdir = tmp_path / 'nodestate'
    sdir.mkdir()
    code = ("import sys; sys.path.insert(0, {!r}); import node; print(node._device_id())"
            .format(os.path.join(ROOT, 'alphanode')))
    env = dict(os.environ, ALPHANODE_STATE_DIR=str(sdir))
    outs = []
    for _ in range(2):
        p = subprocess.run([sys.executable, '-c', code], env=env, cwd=ROOT,
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        outs.append(p.stdout.strip().splitlines()[-1])       # node import logs a vault line
    assert outs[0] and outs[0] == outs[1], outs
    assert len(outs[0]) == 16
    assert all(c in '0123456789abcdef' for c in outs[0])
