# Deploying AlphaNode's vendor side

One cheap VPS runs everything: the marketing site, AlphaHub (licences + vault reveals), and TLS.
Customers' nodes talk to `https://api.<your-domain>`; browsers see `https://<your-domain>`.

```
customer's node ──► api.DOMAIN ──► Caddy ──► AlphaHub (uvicorn)
                                    │            └── /srv/alphahub: hub.db + vault_key  ← BACK UP
buyer's browser ──► DOMAIN     ─────┘──────► site (nginx, ../site)
```

---

## 0. Before you touch the server

**Push the branch.** The whole vendor side (`alphahub/`, `deploy/`) lives on `feat/alpha-vault`.
`main` does not have it — a clone of `main` on the VPS gets you nothing.

```bash
git push origin feat/alpha-vault
```

**Commit the site's build inputs.** Only `site/index.html` and `site/start.html` are tracked
today. Without the rest, `docker compose up` fails at the *site* build and therefore never starts
the hub or Caddy either:

```bash
git add site/Dockerfile site/.dockerignore site/app.js site/style.css site/docs/
git commit -m "Site: build inputs"
git push
```

Verify the clone will be complete (this must list Dockerfile, app.js, style.css, docs/):

```bash
git ls-tree --name-only HEAD site/
```

**Check nothing secret is staged.** `alphanode/gui_settings.json` is tracked and picks up your
personal subscription key at runtime; keep it out of commits (`git checkout -- alphanode/gui_settings.json`).

---

## 1. Prerequisites

* a VPS (1 GB RAM is plenty), Ubuntu 24.04
* a domain with **two A records** pointing at the VPS IP: `DOMAIN` and `api.DOMAIN`
* ports 80 and 443 open in both the OS firewall and the provider's security group

On the VPS:

```bash
curl -fsSL https://get.docker.com | sh          # Ubuntu's docker.io has no compose plugin
sudo usermod -aG docker $USER                    # then LOG OUT and back in
sudo ss -lntp | grep -E ':(80|443)'              # must be empty (stop any distro nginx/apache)
sudo ufw allow 80,443/tcp                        # if ufw is on
```

Check DNS **before** deploying — Caddy asks Let's Encrypt for certificates the moment it starts,
and a wrong A record means a failed challenge and a rate-limit timeout:

```bash
dig +short example.com api.example.com           # both must print the VPS IP
```

DNS can take minutes to hours to propagate. Wait for it.

---

## 2. Get the code and configure

```bash
git clone -b feat/alpha-vault <your-repo> alphanode && cd alphanode/deploy
cp .env.example .env && chmod 600 .env
nano .env                # DOMAIN, ALPHAHUB_WEBHOOK_SECRET, UID/GID (from `id -u` / `id -g`)
```

Create the state directory — it lives **outside** the clone so a re-clone or `git clean -xdf`
can never delete your key:

```bash
sudo mkdir -p /srv/alphahub
sudo chown $(id -u):$(id -g) /srv/alphahub
chmod 700 /srv/alphahub
```

---

## 3. Seed the vault key — before the first start

**This is the step that cannot be undone later.** The vault private key is the ONE key that opens
every sealed formula ever mined by any customer. If you already mined and sealed libraries during
development, that key must move to the server; a freshly generated one cannot open them.

From your **dev machine**, repo root:

```bash
scp -p alphanode/vault_server_key you@VPS:/srv/alphahub/vault_key
```

Then on the VPS: `chmod 600 /srv/alphahub/vault_key`

Skipping this is only correct for a genuinely fresh start (no sealed libraries exist anywhere) —
the hub then generates its own key on first run. If you skip it and later change your mind, every
formula sealed in the meantime is lost: stop the stack, replace the file, restart.

---

## 4. Launch

```bash
docker compose up -d --build
docker compose ps                    # all three must be "Up", not "Restarting"
docker compose logs --tail=50 caddy  # look for "certificate obtained successfully"
```

Then verify — all three must pass:

```bash
curl https://api.<DOMAIN>/health     # {"ok":true}
curl https://<DOMAIN>                # your site's HTML
curl -s https://api.<DOMAIN>/pub.txt # 64 hex chars
```

**The key check.** The last command must print exactly the same hex as your dev machine's
`cat alphanode/vault_server_key.pub`. If it differs, the server is running a different key and
cannot open your existing libraries — stop and redo step 3.

### When it does not come up

| symptom | cause |
|---|---|
| compose exits: `set DOMAIN in deploy/.env` | `.env` missing or the variable is empty |
| caddy log: `no such host` / challenge failed | A records wrong or DNS not propagated |
| caddy: `bind: address already in use` | a distro nginx/apache holds 80/443 |
| hub restarting, log says `ALPHAHUB_WEBHOOK_SECRET is required` | empty secret in `.env` |
| site build fails: `Dockerfile: no such file` | step 0 not done — site files not in git |

---

## 5. Point the desktop app at your hub

The node reads the hub address from `ALPHANODE_VAULT_URL` (default is localhost, for dev) and
seals mined formulas to the public key it finds at `alphanode/vault_server_key.pub`.

**Both must be baked into a build you ship**, otherwise:
* no URL → the customer's node talks to nothing and never unlocks;
* no `.pub` file → **the node mines UNSEALED** — your protection silently disappears.

Fetch the public half from the live hub before building:

```bash
curl -s https://api.<DOMAIN>/pub.txt > alphanode/vault_server_key.pub
```

and export the URL in the launcher (`packaging/AlphaNode.AppDir/AppRun`, the `.deb` wrapper, the
Windows build):

```
ALPHANODE_VAULT_URL=https://api.<DOMAIN>
```

For your own machine you can simply run:

```bash
ALPHANODE_VAULT_URL=https://api.<DOMAIN> .venv/bin/python alphanode/alphanode_gui.py
```

---

## 6. Accounts, keys and money

**A free demo account** (3 nodes) is created by the site's signup form:

```
POST https://api.<DOMAIN>/signup   {"email": "buyer@x.io"}
 -> 200 {"ok":true,"token":"<the subscription key>","plan":"demo"}
 -> 409 {"detail":"account_exists"}     (the key is shown ONCE, at creation)
```

That `token` **is** the subscription key — the customer pastes it into the app's *Activate node*
dialog. One key per account, forever; paying only changes the plan attached to it.

**Paid plans** arrive through the webhook your payment provider calls:

```
POST https://api.<DOMAIN>/webhook/payment
{"secret": "<ALPHAHUB_WEBHOOK_SECRET>", "email": "buyer@x.io",
 "plan": "pro", "expires_at": "2026-09-12T00:00:00Z"}
```

`plan`: `demo` (3 nodes, free) · `pro` (5 nodes) · `scale` (50 nodes).
`status: "canceled"` cancels; omitting `expires_at` means no expiry.

Until a provider is wired, grant by hand on the server — the printed `token:` line is the
customer's key, and you must deliver it to them yourself:

```bash
docker compose exec hub python -m alphahub.admin grant buyer@x.io pro --days 30
docker compose exec hub python -m alphahub.admin show  buyer@x.io   # plan, seats, devices, key
docker compose exec hub python -m alphahub.admin rotate buyer@x.io  # revoke a leaked key
docker compose exec hub python -m alphahub.admin list
```

**Not built yet:** a buyer who pays gets no key automatically — you read the `token:` line and
send it yourself. Signup does not verify the email address either, so a customer who loses their
key cannot recover it without you. (Early-access requests *are* mailed to you — section 9.)

---

## 7. Backups

Two files in `/srv/alphahub` are the whole business:

* **`vault_key`** — irreplaceable. Every sealed formula in every customer's library dies with it.
* **`hub.db`** — accounts, subscriptions, seats.

```bash
tar czf ~/alphahub-$(date +%F).tgz -C /srv alphahub
```

Copy that off the server today, and put it on a nightly cron. The container runs as your user
(`UID`/`GID` in `.env`), so no `sudo` is needed.

---

## 8. Updating

```bash
tar czf ~/alphahub-$(date +%F).tgz -C /srv alphahub     # 2 seconds, and you can go back
cd ~/alphanode && git pull
cd deploy && docker compose up -d --build
docker compose ps                                       # all three Up
curl -s https://api.$DOMAIN/health                      # hub answers
```

The hub creates its schema idempotently on start and adds any new column to a table that already
exists, so an update never needs a manual migration step. `/srv/alphahub` is outside the clone, so
nothing you deploy can touch customer state — the backup above is for your own peace of mind, not
because the update writes over anything.

`--build` matters: without it compose reuses the old image and you pull code that never runs.

If the hub does not come up, its logs say why and the previous image is still on disk:

```bash
docker compose logs --tail=50 hub
docker compose down && git checkout <previous-commit> && docker compose up -d --build
```

---

## 9. Getting the requests by email

The site's early-access form always writes to the database — that is the record, and it survives
a spam filter eating your mail. Mailing you is a convenience on top, and it is off until you
configure it.

### Pick something that can *send*

You need an SMTP server that will accept a login from this VPS. A registrar's mail *forwarding*
is not one: forwarding receives on your domain and passes mail on, it never sends for you. The
usual choices:

| | host / port | notes |
|---|---|---|
| **Zoho Mail** | `smtp.zoho.eu` · 465 · ssl | free mailbox on your own domain; a real `support@` inbox |
| **Google Workspace** | `smtp.gmail.com` · 587 · starttls | needs an *app password*, not your login password |
| **Brevo / Mailgun / Resend** | see their SMTP page · 587 · starttls | sending only — you still need an inbox to receive |

Whatever you pick, `ALPHAHUB_SMTP_FROM` must be an address that provider has verified for you, or
it will refuse the message. `ALPHAHUB_NOTIFY_TO` is just where you read mail — a plain Gmail
address is fine.

### Configure and prove it

Fill the `ALPHAHUB_SMTP_*` block in `deploy/.env` (it is documented in `.env.example`), then:

```bash
docker compose up -d
docker compose logs hub | grep notify        # says where mail goes, or that it is OFF
docker compose exec hub python -m alphahub.admin testmail
```

`testmail` sends one real message and prints the server's answer. Do not skip it — the live path
runs in the background after the visitor already got their "you are on the list", so a broken
setting is invisible from the outside. `--reply-to you@x.io` makes it look like a real request.

### What arrives

Subject is the person's name, `Reply-To` is their address — hit Reply and you are writing to
them. The body carries name, email, phone, their note, and the exact `admin invite` command for
that address.

A repeat submit from the same address does not mail again, and the honeypot field means bots
never do. If the send fails the visitor still sees success (their row is saved either way) and
the reason goes to the log:

```bash
docker compose logs hub | grep '\[notify\]'
docker compose exec hub python -m alphahub.admin requests --new
```
