# Tech Stack, Services & Billing — AskCruz

*Operational/financial inventory of everything AskCruz (Cruz's public name) runs on
and pays for — technical dependencies plus vendor billing. This is a different kind
of document from the rest of `docs/` — those describe how the system works
technically; this one tracks what it costs and who to pay. Compiled 2026-08-28 from
the live server, this repo's code, and `.env`'s configured credentials — everything
under "Billing" is a placeholder (`[FILL IN]`) unless stated otherwise, since plan
tier / cost / renewal date live in vendor accounts this session has no access to,
not in code.*

**Scope**: `eoxs-wiki-db` (this repo, the backend) + `eoxs-frontend-threads`
(sibling repo, same box) + every external vendor either depends on. Does **not**
cover `askcruz.com` itself (the chat frontend) — that's Jaskeerat's Vercel app, a
separate codebase this session has no access to; its stack/hosting/billing needs to
come from him directly. `raj-wiki-vault`/`raj-wiki-vault-main`/`claude-notes-vault`
are excluded per standing instruction (out of scope for this repo's work).

---

## 1. How to keep this current

Nothing here is auto-synced — unlike the rest of `docs/`, there's no code that
reads a vendor's billing API. **Update this file by hand** whenever:
- A new vendor/service gets added or removed (check `.env` for new credential
  names — every external dependency has one, per §3 below).
- A plan tier changes, a price changes, or a renewal happens.
- The server itself changes (host, size, IP) — see `docs/migration-status.md` for
  the current authoritative state of that, since it changes independently of this
  file and this file should not try to duplicate it.

This file is `doc_type='doc'` in `repo_docs` like every other file in `docs/` — it
flows into the wiki automatically on the next 6-hourly cycle after any edit (see
`docs/wiki-ingestion.md` Phase 3), same as everything else here.

---

## 2. The two codebases in scope

| Repo | What it is | Where it runs | Own DB |
|---|---|---|---|
| `eoxs-wiki-db` (this repo) | Raw ingestion → AI-synthesized wiki → tiered MCP connectors. The actual "second brain." | `/home/deploy/eoxs-wiki-db` on the live server (`docs/backend-server.md`) | `eoxs_wiki` (+ `eoxs_wiki_staging`) |
| `eoxs-frontend-threads` | `save_chat_transcript`/`get_thread`/`list_threads` — append-only Postgres storage for frontend chat transcripts, per-user secret-URL identity. Deliberately kept out of this repo's codebase so conversation volume never bloats it. | `/home/deploy/eoxs-frontend-threads`, same physical server, own systemd unit (`eoxs-frontend-threads.service`, port 8094) | `eoxs_frontend_threads` (same Postgres instance) |

Both run as Linux user `deploy` on the one VPS described in `docs/backend-server.md`
— see that file (or `docs/backend-server-digitalocean.md` for the parallel-track
migration plan) for the host itself; not duplicated here.

---

## 3. External dependencies — every paid/free vendor, and why

One row per credential family actually present in `eoxs-wiki-db/.env` (verified
live 2026-08-28) or otherwise load-bearing for either repo. Grouped by function.

### AI / LLM

| Service | Used for | Env var(s) | Plan | Billing cycle | Amount |
|---|---|---|---|---|---|
| **Anthropic API** | Two separate keys, deliberately: `ANTHROPIC_API_KEY` (spam/relevance filtering, call-noise filtering — `spam_filter.py`, `call_relevance.py`) and `CLASSIFIER_ANTHROPIC_API_KEY` (access-tier classification — `inline_tier_classifier.py`, `tier_classifier.py`), kept separate for independent cost tracking per the code's own convention. Also the underlying model for every `claude -p` wiki-ingestion sub-agent call (`wiki_ingestion/headless_agent.py`) and the MCP redaction safety net (`mcp_server/redaction.py`, Sonnet). | `ANTHROPIC_API_KEY`, `CLASSIFIER_ANTHROPIC_API_KEY` | [FILL IN — usage-based API, not a flat plan] | [FILL IN — likely monthly invoice] | [FILL IN — variable, scales with ingestion + wiki-cycle volume] |
| **Claude CLI** (`claude`, `/usr/bin/claude`) | Runs the actual `claude -p` sub-agent processes the wiki pipeline spawns (Phase 3/4/5). Authenticated via an interactive `deploy`-user login (`~/.claude`), separate from the API keys above — see `docs/backend-server.md` §7 for the distinction. | none (interactive login, not an env var) | [FILL IN — which plan/seat this login is tied to] | [FILL IN] | [FILL IN] |

### Raw data sources (ingested every 2 hours + real-time webhooks where available)

| Service | Used for | Env var(s) | Plan | Billing cycle | Amount |
|---|---|---|---|---|---|
| **Google Workspace / Gmail** | 3 mailboxes ingested (`raj_gmail`, `ron_gmail`, `remya_gmail`), plus self-serve OAuth connect for any new account (`ingestion/oauth_gmail.py`). Two registered OAuth clients (Desktop + Web) — `GMAIL_OAUTH_CLIENT_ID/SECRET`, `GMAIL_OAUTH_WEB_CLIENT_ID/SECRET`. Per-account refresh tokens now DB-backed (`oauth_accounts` table), legacy `.env` triplets (`RAJ_/RON_/REMYA_GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`) still present but superseded. | `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, `GMAIL_OAUTH_WEB_CLIENT_ID`, `GMAIL_OAUTH_WEB_CLIENT_SECRET`, + per-account legacy triplets | [FILL IN — which Workspace plan, how many seats] | [FILL IN] | [FILL IN] |
| **Google Cloud Pub/Sub** | Gmail push-notification subscription (real-time webhook trigger for new mail), backing `POST /webhook/gmail` — see `docs/raw-ingestion.md` §3. | (uses the same Google OAuth credentials above) | [FILL IN — GCP project billing account] | [FILL IN] | [FILL IN — Pub/Sub is usage-based, likely negligible at this volume] |
| **Zoho Mail** | `support_zoho` (legacy) + self-serve-connected accounts (`ingestion/oauth_zoho.py`). Two OAuth clients (`ZOHO_CLIENT_ID/SECRET`, `ZOHO_WEB_CLIENT_ID/SECRET`). | `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_WEB_CLIENT_ID`, `ZOHO_WEB_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` | [FILL IN — Zoho Mail plan tier, seat count] | [FILL IN] | [FILL IN] |
| **Fireflies.ai** | Call-transcript ingestion (`fireflies_fetcher.py`), plus a `transcript.completed` webhook. | `FIREFLIES_API_KEY` | [FILL IN] | [FILL IN] | [FILL IN] |
| **Fathom** | Call-recording ingestion (`fathom_fetcher.py`), plus a `recording.completed` webhook (Ron's account only — `RON_FATHOM_API_KEY`). | `RON_FATHOM_API_KEY` | [FILL IN] | [FILL IN] | [FILL IN] |
| **Odoo (6 client tenants + 1 central)** | Per-client implementation/onboarding Kanban boards (`odoo_fetcher.py`) for Greer, ESS, DPS (Discount Pipe & Steel), PPC, 3GM Steel, Sabre Alloys — plus a 7th, central instance for EOXS's own support tickets/invoices (now read via the separate `eoxs-teams` connector, not this repo — see `CLAUDE.md`). | `GREER_ODOO_USERNAME/PASSWORD`, `ESS_ODOO_USERNAME/PASSWORD`, `DPS_ODOO_USERNAME/PASSWORD`, `PPC_ODOO_USERNAME/PASSWORD`, `THREEGM_ODOO_USERNAME/PASSWORD`, `SABRE_ODOO_USERNAME/PASSWORD`, `EOXS_TICKETS_ODOO_USERNAME/PASSWORD` | [FILL IN — whose Odoo instances these are; likely each client's own paid instance, not EOXS's cost] | [FILL IN] | [FILL IN — probably $0 to EOXS if client-owned] |

### Reporting / tracking

| Service | Used for | Env var(s) | Plan | Billing cycle | Amount |
|---|---|---|---|---|---|
| **Linear** | One-way status reporting sink (`docs/linear-integration.md`) — raw-ingestion sweeps and wiki-ingestion cycles push progress to a dedicated `EDB` team. `LINEAR_API_KEY`/`LINEAR_TEAM_KEY` are a **dead, unused pair** — only the `_EDB_` variants are actually read. | `LINEAR_EDB_API_KEY`, `LINEAR_EDB_TEAM_KEY` (ignore `LINEAR_API_KEY`/`LINEAR_TEAM_KEY`) | [FILL IN — Linear plan tier for this workspace] | [FILL IN] | [FILL IN] |

### Infrastructure

| Service | Used for | Env var(s) | Plan | Billing cycle | Amount |
|---|---|---|---|---|---|
| **VPS host** | The one physical machine everything runs on. See `docs/backend-server.md` for the live Hetzner-origin box's full spec, `docs/backend-server-digitalocean.md` for the parallel DigitalOcean-migration track, and `docs/migration-status.md` for which is authoritative right now — deliberately not restated here to avoid two places drifting out of sync. | n/a (host-level, not a credential) | [FILL IN] | [FILL IN] | [FILL IN] |
| **DigitalOcean Managed Postgres** (`eoxs-db-live-cluster`, SGP1) | Planned re-architecture target (cluster already exists per `docs/migration-status.md` §4) — not yet the live database (that's still self-hosted Postgres 16 on the VPS, `docs/postgres-database.md`). | n/a | [FILL IN — cluster size/tier] | [FILL IN] | [FILL IN] |
| **DigitalOcean App Platform** | Planned re-architecture target for the stateless services (MCP connectors, webhook receiver, sweep) — `.do/app.yaml`, not yet live. | n/a | [FILL IN] | [FILL IN] | [FILL IN] |
| **GoDaddy** | DNS registrar for `askcruz.com` (the frontend's domain — apex is a live Vercel site, **do not touch**) and the planned `mcp.askcruz.com` subdomain for this backend (`docs/migration-status.md` §4 item 2). DNS is *not* hosted on DigitalOcean. | n/a | [FILL IN — domain registration, likely annual] | Annual (typical for domain registration) | [FILL IN] |
| **Let's Encrypt** (via `certbot`) | TLS certificates — free, automated. Short-lived (~7-day) IP-address cert on the live Hetzner box, normal 90-day cert on the DigitalOcean box. Auto-renews via `certbot.timer`. | n/a | Free | n/a | $0 |

### Not billed separately (bundled into the above or genuinely free)

- **`pgweb`** — self-hosted read-only DB browser, no external service, no cost.
- **nginx, fail2ban, ufw, unattended-upgrades** — bundled with the VPS, no separate cost.
- **`mcp` Python package (`mcp==1.29.0`)** — open-source library, no cost.

---

## 4. Total monthly cost — rollup

*Fill in once every row in §3 has a real number. Structure kept ready so this
becomes a one-line answer rather than a re-derivation every time someone asks.*

| Category | Monthly (or monthly-equivalent) | Notes |
|---|---|---|
| AI / LLM (Anthropic API + Claude CLI seat) | [FILL IN] | Usage-based — expect this to be the most variable line item, scaling with ingestion volume and wiki-cycle frequency |
| Raw data sources (Gmail/Workspace, Zoho, Fireflies, Fathom) | [FILL IN] | |
| Odoo (client tenants) | [FILL IN — likely $0 if client-owned] | |
| Reporting (Linear) | [FILL IN] | |
| Infrastructure (VPS + planned DO managed services + domain, amortized) | [FILL IN] | Domain renewal is annual — divide by 12 for this rollup |
| **Total** | **[FILL IN]** | |

---

## 5. Dependency map — what breaks if a vendor goes down or a key expires

Ordered by blast radius, worst first. Cross-referenced to the fuller technical
detail already documented elsewhere rather than restating it.

1. **Postgres (self-hosted, this VPS)** — everything reads/writes here. No
   managed failover, no automated backup yet (`docs/migration-status.md` §4,
   `docs/postgres-database.md` §8). Single point of failure for the entire system.
2. **Anthropic API** — both ingestion filtering/classification AND the entire
   wiki-synthesis pipeline (`claude -p` sub-agents) stop working without it. Raw
   ingestion of *unfiltered* data would likely still partially function (fetchers
   don't all require a Claude call), but nothing gets classified or synthesized.
3. **The `deploy` user's `claude` CLI login** — separate failure mode from the API
   key above (see `docs/backend-server.md` §7's real incident: the DigitalOcean
   migration shipped with the binary but not the login, and the pipeline ran
   `FileNotFoundError`/`Not logged in` for a full day undetected). This credential
   doesn't show up in any `.env` scan — it's easy to forget exists.
4. **Gmail, Zoho, Fireflies, Fathom, Odoo credentials** — each is independent; one
   expiring/revoking only stops that source's sweep (`ingestion/server.py`'s
   `_sweep_lock` isolates failures per-source), never the whole system. OAuth
   refresh tokens can go stale; see `docs/raw-ingestion.md` for per-source
   token-lifetime behavior.
5. **Linear (`LINEAR_EDB_API_KEY`)** — pure reporting sink, zero effect on
   ingestion/synthesis if it goes down (`docs/linear-integration.md` §7 — every
   call is wrapped to never raise past itself). Lowest blast radius of anything
   in this table.
6. **GoDaddy / DNS** — currently low-impact for this backend specifically (no
   domain in production use yet — both live boxes are on bare-IP/`nip.io`
   addresses, `docs/backend-server.md` §4, `docs/backend-server-digitalocean.md`
   §10). Would matter immediately once `mcp.askcruz.com` goes live.
7. **`askcruz.com` (Vercel, out of scope)** — the actual end-user-facing failure
   point if the *frontend* goes down, independent of everything in this document.
   Not tracked here; ask Jaskeerat.

---

## 6. Open items

- [ ] Fill in every `[FILL IN]` above — needs access to the actual vendor
  accounts/invoices (Anthropic Console, Google Workspace admin, Zoho billing,
  Fireflies/Fathom account settings, Linear workspace settings, DigitalOcean
  billing, GoDaddy account) — none of this is derivable from code or the live
  server, only from those dashboards directly.
- [ ] Decide an owner for keeping this current — per `docs/backend-server.md`
  §1 note in `ARCHITECTURE.md` §5, credentials/access for the server and
  database are held by **Ayan**; the Linear board's credentials by **Ayan and
  Nidhi**. Whoever owns vendor billing access should own this file's upkeep,
  since (unlike the rest of `docs/`) nothing here can be verified by reading
  code.
- [ ] Once `askcruz.com`'s stack is documented by Jaskeerat, either fold a
  summary into this file (with a pointer to wherever the full detail lives) or
  cross-link from here — deliberately left out of scope today rather than
  guessed at.
