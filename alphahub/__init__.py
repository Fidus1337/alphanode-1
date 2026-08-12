"""AlphaHub — the vendor-side licence + vault server for AlphaNode.

A single small service that owns the crown jewels: the vault private key (the ONLY thing that
opens sealed formulas), the customer accounts, their subscriptions, and the per-account device
roster that enforces the node limit. The desktop node never sees any of this — it just carries
an account token and a per-machine device id, and asks the hub to activate and to reveal.

Prototype status: paste-a-key auth (the account token IS the subscription key the user enters),
payment-provider-agnostic webhook (Paddle / crypto plug in later without code changes), seat-based
node limits (registered machines, not concurrency). Not yet: TLS termination (front it with a
reverse proxy), password login, rate limiting.
"""
