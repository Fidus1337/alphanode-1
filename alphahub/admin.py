"""AlphaHub admin CLI — manage accounts without the web layer. Same DB the server uses; runs
apply_payment (exactly what the payment webhook does) so grants/cancels are consistent.

    python -m alphahub.admin grant  <email> <plan> [--days N]   # create/upgrade; --days sets expiry
    python -m alphahub.admin cancel <email>                     # mark canceled (reveals stop)
    python -m alphahub.admin show   <email>                     # plan, seats used, devices
    python -m alphahub.admin unseat <email> <device_id>         # free a seat
    python -m alphahub.admin rotate <email>                     # new token (revokes the old)
    python -m alphahub.admin list                               # all accounts

Config: ALPHAHUB_DB (default alphahub/hub.db).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

from alphahub import db as hubdb                          # noqa: E402


def _db():
    conn = hubdb.connect(os.environ.get('ALPHAHUB_DB', os.path.join(HERE, 'hub.db')))
    hubdb.init_db(conn)
    return conn


def cmd_grant(a):
    conn = _db()
    expires = None
    if a.days:
        expires = (datetime.now(timezone.utc) + timedelta(days=a.days)).isoformat(timespec='seconds')
    token = hubdb.apply_payment(conn, a.email, a.plan, expires_at=expires)
    st = hubdb.subscription_state(conn, hubdb.get_user_by_email(conn, a.email)['id'])
    print(f'granted {a.email}: plan={st["plan"]} seats={st["node_limit"]} '
          f'expires={expires or "never"}')
    print(f'token: {token}')


def cmd_cancel(a):
    conn = _db()
    row = hubdb.get_user_by_email(conn, a.email)
    if not row:
        sys.exit(f'no such account: {a.email}')
    sub = conn.execute('SELECT plan, expires_at FROM subscriptions WHERE user_id=?',
                       (row['id'],)).fetchone()
    plan = sub['plan'] if sub else 'demo'
    hubdb.apply_payment(conn, a.email, plan,
                        expires_at=(sub['expires_at'] if sub else None), status='canceled')
    print(f'canceled {a.email} (reveals/activations now refused)')


def cmd_show(a):
    conn = _db()
    row = hubdb.get_user_by_email(conn, a.email)
    if not row:
        sys.exit(f'no such account: {a.email}')
    st = hubdb.subscription_state(conn, row['id'])
    print(f'{a.email}  token={row["token"]}')
    print(f'  plan={st["plan"]} status={st["status"]} active={st["active"]} '
          f'seats={st["used"]}/{st["node_limit"]} expires={st["expires_at"] or "never"}')
    for d in hubdb.list_devices(conn, row['id']):
        print(f'  device {d["device_id"]}  {d["label"] or ""}  last_seen={d["last_seen"]}')


def cmd_unseat(a):
    conn = _db()
    row = hubdb.get_user_by_email(conn, a.email)
    if not row:
        sys.exit(f'no such account: {a.email}')
    ok = hubdb.remove_device(conn, row['id'], a.device_id)
    print('seat freed' if ok else 'no such device')


def cmd_rotate(a):
    conn = _db()
    row = hubdb.get_user_by_email(conn, a.email)
    if not row:
        sys.exit(f'no such account: {a.email}')
    print('new token:', hubdb.rotate_token(conn, row['id']))


def cmd_list(_a):
    conn = _db()
    for u in conn.execute('SELECT id, email, token FROM users ORDER BY id'):
        st = hubdb.subscription_state(conn, u['id'])
        print(f'{u["email"]:<32} {st["plan"] or "-":<6} '
              f'{st["used"]}/{st["node_limit"]} seats  {"active" if st["active"] else st["status"]}')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='alphahub.admin')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('grant'); g.add_argument('email'); g.add_argument('plan')
    g.add_argument('--days', type=int, default=0); g.set_defaults(fn=cmd_grant)
    c = sub.add_parser('cancel'); c.add_argument('email'); c.set_defaults(fn=cmd_cancel)
    s = sub.add_parser('show'); s.add_argument('email'); s.set_defaults(fn=cmd_show)
    u = sub.add_parser('unseat'); u.add_argument('email'); u.add_argument('device_id')
    u.set_defaults(fn=cmd_unseat)
    r = sub.add_parser('rotate'); r.add_argument('email'); r.set_defaults(fn=cmd_rotate)
    ls = sub.add_parser('list'); ls.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == '__main__':
    main()
