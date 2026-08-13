# Deploying AlphaNode's vendor side

One cheap VPS runs everything: the marketing site, AlphaHub (licences + vault reveals),
and TLS. Customers' nodes talk to `https://api.<your-domain>`; browsers see
`https://<your-domain>`.

```
customer's node ──► api.DOMAIN ──► Caddy ──► AlphaHub (uvicorn)
                                    │            └── /data: hub.db + vault_key  ← BACK UP
buyer's browser ──► DOMAIN     ─────┘──────► site (nginx, ../site)
```

## Prerequisites

* a VPS (1 GB RAM is plenty) with Docker + the compose plugin
* a domain with two DNS **A records** pointing at the VPS: `DOMAIN` and `api.DOMAIN`
* ports 80 and 443 open

## First launch

```bash
git clone <your-repo> && cd <repo>/deploy
cp .env.example .env            # set DOMAIN + ALPHAHUB_WEBHOOK_SECRET (see the file)
mkdir -p hubdata
```

**Now the single most important step.** The vault private key is the ONE key that opens
every sealed formula ever mined by any customer. If your libraries were already sealed
during development, that key must move to the server — a freshly generated one cannot
open them:

```bash
scp alphanode/vault_server_key   you@vps:<repo>/deploy/hubdata/vault_key
```

(Skip this only for a truly fresh start — the hub then generates a new key on first run.)

```bash
docker compose up -d --build
```

Caddy fetches certificates automatically on the first request. Check:

```bash
curl https://api.<DOMAIN>/health        # {"ok":true}
curl https://api.<DOMAIN>/pub           # the public key nodes seal to
```

## Wiring the clients

The node reads the hub address from `ALPHANODE_VAULT_URL` (defaults to localhost for
dev). A shipped build sets:

```
ALPHANODE_VAULT_URL=https://api.<DOMAIN>
```

and ships `vault_server_key.pub` (the PUBLIC half only — `curl https://api.<DOMAIN>/pub`)
next to the app as `alphanode/vault_server_key.pub`. The private key must never leave
the server.

## Payments

Any provider that can POST a webhook works (Paddle, crypto processors, …). Point it at:

```
POST https://api.<DOMAIN>/webhook/payment
{"secret": "<ALPHAHUB_WEBHOOK_SECRET>", "email": "buyer@x.io",
 "plan": "pro", "expires_at": "2026-09-12T00:00:00Z"}
```

`plan`: `demo` (3 nodes, free) · `pro` (5 nodes) · `scale` (50 nodes).
`status: "canceled"` cancels; omitted `expires_at` means no expiry.

Until a provider is wired, grant manually on the server:

```bash
docker compose exec hub python -m alphahub.admin grant buyer@x.io pro --days 30
docker compose exec hub python -m alphahub.admin show  buyer@x.io
docker compose exec hub python -m alphahub.admin list
```

## Backups

Everything that matters is two files in `./hubdata`:

* `vault_key` — irreplaceable. Copy it somewhere offline **today**.
* `hub.db` — accounts, subscriptions, seats. `sqlite3 hubdata/hub.db ".backup b.db"`
  or just copy the file; nightly cron + `scp` is fine at this scale.

## Updating

```bash
git pull && docker compose up -d --build
```

The db schema is created idempotently on start; `hubdata` is untouched by rebuilds.
