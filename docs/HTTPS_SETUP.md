# HTTPS Setup — Guaritradbot via Coolify Traefik

**Sprint 46P (audit A10)** — Audit finding A10 of `AUDITORIA_COMPLETA_2026-07-11.md`
flagged the bot's API as exposed in **plain HTTP** on the VPS public IP
(`http://13.140.181.29:8088`), with the bearer token traveling in clear
text. The audit's recommendation: **terminate TLS at Coolify's Traefik
with a hostname**.

This doc is the reproduction recipe.

---

## Current state (pre-Sprint 46P)

- The bot listens on `0.0.0.0:8080` inside the container.
- `docker-compose.yml` exposes it as `host:8088 -> container:8080` directly,
  in **plain HTTP** (no TLS).
- The dashboard (Next.js) is similarly exposed as `host:3050 -> container:3000`
  in plain HTTP.
- Both ports are reachable from the public internet.

The audit's `audit A10` finding: *"El token viaja en claro (compose publica
8088 sin TLS) y con 12h de vida gatilla `POST /api/restart` (reiniciable en
bucle = DoS que además borra el drawdown kill switch, ver A1), el toggle
PAPER→LIVE y `close-all`."*

## Target state

- The bot is reachable **only** via `https://guaritradbot.13.140.181.29.sslip.io`
  (HTTPS, Let's Encrypt cert, behind Coolify's Traefik reverse proxy).
- The dashboard is reachable via `https://guaritradbot-dash.13.140.181.29.sslip.io`.
- The plain-HTTP `ports:` mappings in `docker-compose.yml` are removed.
- Traefik injects the `X-Forwarded-Proto: https` header so the bot's own
  CORS + auth code can know it's behind TLS.

## How other projects in this VPS already do it

Two of the four other Coolify-managed apps on this host already use this
pattern with `*.13.140.181.29.sslip.io` (a `sslip.io` subdomain — auto-
resolves to the embedded IP, no DNS configuration required):

- `riskfxprep.13.140.181.29.sslip.io` → app `ypqqk292i4ucih866l5em119`
  (see `/data/coolify/applications/ypqqk292i4ucih866l5em119/docker-compose.yaml`
  on the VPS — the Traefik labels there are Coolify-generated)
- `medsysve.com` / `www.medsysve.com` → app `hze8mocuh4xqskqwrm3mx50b`
  (real DNS, apex→www redirect via Traefik middlewares)

The pattern to copy is the sslip.io one (zero DNS work, free cert, ~60s
provisioning — already battle-tested in the riskfxprep app).

## Setup procedure

**1. In Coolify UI** (https://13.140.181.29:8000):
- Open the `guaritradbot` project.
- Open the `guaritradbot` service (the one running the bot, NOT the
  dashboard service).
- Go to the "Domains" / "Service Domain" panel.
- Add `guaritradbot.13.140.181.29.sslip.io`.
- Repeat for the `dashboard` service with
  `guaritradbot-dash.13.140.181.29.sslip.io`.
- Coolify regenerates the docker-compose.yaml under
  `/data/coolify/applications/wyn2ah6rflg6ufwzpvzk436f/`
  with the Traefik labels and Let's Encrypt `certresolver=letsencrypt`
  (already configured in `/data/coolify/proxy/docker-compose.yml`).
- Click "Deploy" (or it auto-deploys on domain save).

**2. In this repo** (after confirming HTTPS works in step 1):
- Edit `docker-compose.yml` and remove the `ports:` mappings for the
  bot (`"8088:8080"`) and the dashboard (`"3050:3000"`). Once Traefik
  is the only ingress, direct port mappings are an attack surface (a
  second, unprotected way in) and should be closed.
- Rebuild the dashboard with the new public URL: in Coolify's
  environment variables for the dashboard service, set
  `NEXT_PUBLIC_API_URL=https://guaritradbot.13.140.181.29.sslip.io`
  (the dashboard bakes this URL at build time — see
  `Dockerfile.dashboard` and the `args:` section of `docker-compose.yml`).
- Push the docker-compose change so the next redeploy from a webhook
  also closes the plain-HTTP mappings.

**3. Verify**:
- `curl -i https://guaritradbot.13.140.181.29.sslip.io/api/health`
  returns HTTP 200 with a Let's Encrypt cert (issuer `Let's Encrypt` or
  `R10`/`R11`).
- `curl -i http://13.140.181.29:8088/api/health` (the old port) returns
  connection refused or "no service listening" — the port is closed.
- The browser, hitting `https://guaritradbot-dash.13.140.181.29.sslip.io`,
  loads the dashboard, logs in successfully, and shows live data.

## What Sprint 46N (audit A9/A10) already shipped

The code half of A10 was already done in commit `bc3185a` (Sprint 46N
"dashboard API auth hardening"):

- `Depends(auth.require_auth)` on every read endpoint (C5).
- `DASHBOARD_CORS_ORIGINS` env var replaces the previous `*` default
  with an exact-origin allowlist (A9).
- Login uses `hmac.compare_digest` for timing-safe signature verification
  (A9).
- Rate limit on `/api/auth/login` with lockout (A9).
- `/api/restart` is gated by a 1-hour cooldown + `SYSTEM_ERROR` audit
  event on every call — even a token compromise can't rapid-fire restart
  the bot and clobber the drawdown kill switch (A1 → A10 link).
- Independent `DASHBOARD_TOKEN_SECRET` for HMAC signing, separate from
  `DASHBOARD_PASSWORD` — a weak password no longer lets an attacker
  forge tokens offline (A9).

What **remains** after the TLS step:

- (Optional, deferred) Move the WS auth from a `?token=` query string
  to a `HttpOnly` cookie set at login + an ephemeral single-use ticket
  exchanged for the WS upgrade. The audit's wording: *"considerar cookie
  HttpOnly y ticket efímero para el WS"*. The query-string token is
  logged by any proxy in the path (a real problem with most production
  reverse proxies and load balancers). Tracking: not required for LIVE
  in 2026-Q3; revisit when adding a second dashboard deployment.

## Why sslip.io and not a real domain

Three reasons, in order of weight:

1. **Zero DNS work.** `*.13.140.181.29.sslip.io` resolves to
   `13.140.181.29` automatically — no A/CNAME records, no registrar
   API, no waiting for TTLs.
2. **Same approach as `riskfxprep`.** Lower cognitive load to have all
   trading bots on the same pattern; easier to reason about
   certificate provisioning, rate limits, and `Host()` Traefik
   matching.
3. **Reversible.** If Carlos later wants `guaritradbot.yogui.tech` (or
   similar), the Coolify change is "edit the domain, redeploy" — no
   code change required, because Traefik labels are Coolify-managed.

A real `*.yogui.tech` (or similar) can be added in parallel or as a
swap-in replacement; Coolify supports both routes on the same service
at the same time.
