"""AlphaHub admin CLI — manage accounts without the web layer. Same DB the server uses; runs
apply_payment (exactly what the payment webhook does) so grants/cancels are consistent.

    python -m alphahub.admin grant  <email> <plan> [--days N]   # create/upgrade; --days sets expiry
    python -m alphahub.admin cancel <email>                     # mark canceled (reveals stop)
    python -m alphahub.admin show   <email>                     # plan, seats used, devices
    python -m alphahub.admin unseat <email> <device_id>         # free a seat
    python -m alphahub.admin rotate <email>                     # new token (revokes the old)
    python -m alphahub.admin list                               # all accounts
    python -m alphahub.admin requests [--new]                   # early-access waitlist
    python -m alphahub.admin invite <email> <plan> [--days N]   # grant + mark the request invited
    python -m alphahub.admin testmail                           # prove the notification mail works
    python -m alphahub.admin catchup                            # mail every request never announced

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


def cmd_requests(a):
    conn = _db()
    rows = hubdb.list_access_requests(conn, 'new' if a.new else None)
    if not rows:
        print('no requests')
        return
    for r in rows:
        who = f'{r["name"]} <{r["email"]}>' if r['name'] else r['email']
        # the marker matters: an unannounced request is one you only ever see by running this
        flag = '' if r['notified_at'] else '  ← never announced'
        print(f'{r["created_at"]}  {r["status"]:<8} {who}{flag}')
        if r['phone']:
            print(f'      tel: {r["phone"]}')
        if r['note']:
            print(f'      {r["note"][:300]}')
    print(f'\n{len(rows)} request(s)')


def _one_request(r):
    who = f'{r["name"]} <{r["email"]}>' if r['name'] else r['email']
    out = [f'{r["created_at"]}  {who}']
    if r['phone']:
        out.append(f'      tel: {r["phone"]}')
    if r['note']:
        out.append(f'      {r["note"]}')
    out.append(f'      invite: admin invite {r["email"]} demo')
    return '\n'.join(out)


def cmd_catchup(a):
    """Mail one digest of every request the operator was never told about, then stamp them.

    This is the command that rescues a backlog: requests that arrived while SMTP was unconfigured
    are not lost, they are simply unannounced, and there is no other way to find that out than
    remembering to run `requests`. Nothing is stamped unless the send succeeds."""
    from alphahub.server import mail_config, send_mail
    conn = _db()
    rows = hubdb.list_unnotified(conn)
    if not rows:
        print('nothing waiting — every request has been announced')
        return
    print(f'{len(rows)} request(s) never announced:\n')
    body = '\n\n'.join(_one_request(r) for r in rows)
    print(body)
    if a.dry_run:
        print('\n(dry run — nothing sent, nothing stamped)')
        return
    if mail_config() is None:
        sys.exit('\nmail is OFF — set ALPHAHUB_SMTP_HOST and ALPHAHUB_NOTIFY_TO '
                 '(deploy/README.md, "Getting the requests by email")')
    plural = 's' if len(rows) > 1 else ''
    ok, detail = send_mail(f'AlphaNode: {len(rows)} early-access request{plural} waiting',
                           body + '\n')
    if not ok:
        sys.exit(f'\nFAILED - {detail}\nnothing stamped; run this again once mail works')
    hubdb.mark_notified(conn, [r['email'] for r in rows])
    print(f'\nOK - {detail}; {len(rows)} marked as announced')


def cmd_testmail(a):
    """Send one real message through the configured SMTP server and print what happened. Worth a
    command of its own: the live path runs in a background task after a 200, so without this the
    only way to test a mail setting is to submit the form and wonder."""
    from alphahub.server import mail_config, send_mail   # imported late: needs no DB, no app
    cfg = mail_config()
    if cfg is None:
        sys.exit('mail is OFF — set ALPHAHUB_SMTP_HOST and ALPHAHUB_NOTIFY_TO '
                 '(deploy/README.md, "Getting the requests by email")')
    print(f'via {cfg["host"]}:{cfg["port"]} ({cfg["tls"]})'
          f'{" as " + cfg["user"] if cfg["user"] else " with no login"}\n'
          f'from {cfg["sender"]} -> {cfg["to"]}')
    ok, detail = send_mail(
        'AlphaNode: test notification',
        'This is `admin testmail`.\n\nIf it reached you, early-access requests will too.\n',
        reply_to=a.reply_to, cfg=cfg)
    print('OK -' if ok else 'FAILED -', detail)
    if not ok:
        sys.exit(1)


def cmd_invite(a):
    """Grant the plan and take the address off the waitlist in one step — the token it prints
    is what you paste into the reply."""
    cmd_grant(a)
    conn = _db()
    if hubdb.mark_access_request(conn, a.email, 'invited'):
        print('waitlist: marked invited')


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
    rq = sub.add_parser('requests'); rq.add_argument('--new', action='store_true',
                                                     help='only the ones not invited yet')
    rq.set_defaults(fn=cmd_requests)
    iv = sub.add_parser('invite'); iv.add_argument('email'); iv.add_argument('plan')
    iv.add_argument('--days', type=int, default=0); iv.set_defaults(fn=cmd_invite)
    tm = sub.add_parser('testmail')
    tm.add_argument('--reply-to', default=None, help='set Reply-To, as a real request would')
    tm.set_defaults(fn=cmd_testmail)
    cu = sub.add_parser('catchup')
    cu.add_argument('--dry-run', action='store_true', help='show the digest without sending it')
    cu.set_defaults(fn=cmd_catchup)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == '__main__':
    main()
