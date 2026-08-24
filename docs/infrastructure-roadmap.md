# Infrastructure Roadmap

*Where this system's hosting stands today, what's been decided about where it's going,
and what's still open. Written so a fresh session — human or Claude — can pick this up
with full context, without needing chat history that doesn't exist in this repo.*

> **UPDATE 2026-08-21 — partially executed. Read `docs/migration-status.md` first.**
> A migration to DigitalOcean happened on 2026-08-20, but as a **lift-and-shift** (one
> droplet running everything, mirroring Hetzner) rather than the hybrid split this
> document describes. Phase 1 is therefore effectively done in a different shape, and
> phases 2-3 (Managed Database, App Platform) are now in progress against a droplet that
> already exists. Hetzner is **still live and still the system of record**. The reasoning
> below is unchanged and still correct; only the "not started" status lines are stale.*

## Current state — plainly

**(Superseded 2026-08-20 — see the banner above; this describes Hetzner, which is still live.)**
Everything runs on a single Hetzner VPS (`5.223.44.95`, hostname `ubuntu-4gb-sin-1`, 2 vCPU,
~4GB RAM, Singapore). No backup exists for either database. No uptime/health monitoring
exists. TLS is currently on a raw-IP Let's Encrypt certificate (short-lived, ~6-day renewal
cycle) — no domain name is pointed at this server yet. Full detail in
`docs/backend-server.md`.

**Security hardening applied 2026-08-15, after this doc's original phased plan was written**
(`server_tokens off`, TLS locked to 1.2/1.3, security headers + HSTS, MCP-secret log masking,
fail2ban + sshd key-only hardening) — done by hand directly on the box, not by any script, and
until 2026-08-20 not captured anywhere in the repo either. Now tracked in `deploy/HARDENING.md`
(+ `deploy/nginx.conf`, `deploy/fail2ban-jail.local`) specifically so a fresh DigitalOcean
Droplet provision doesn't silently lose it — a brand-new box built from `deploy/setup.sh` alone
would NOT reproduce any of this. Any migration plan below must re-apply it explicitly; see that
file for the full list and for what's still a manual TODO (the fail2ban `ignoreip` office-IP
list, for one).

## Why a migration is even being considered

Not because anything is broken — because the owner is planning to run this same kind of
system for future clients, and standing up the current setup by hand (systemd units, nginx
config, cert bootstrapping, manual backups that don't exist yet) doesn't repeat well. A
managed platform trades a real, ongoing dollar cost for a large chunk of that operational
work becoming someone else's job.

## The one architectural insight that took real back-and-forth to reach

**Not everything on this server can move to a container platform (App Platform, Render,
etc.).** Two categories of work exist here, and they need fundamentally different hosting:

1. **Stateless services** — the customer-facing MCP connectors, the webhook receiver, the
   scheduled raw-ingestion sweep. No dependency on anything specific to this machine. These
   move cleanly to a PaaS.
2. **Claude-CLI-dependent work** — the wiki-synthesis pipeline (which literally spawns
   `claude -p` as a subprocess), the internal MCP server those sub-agents connect to, and —
   just as real — the owner's own interactive `claude --teleport` session and Codex CLI,
   used for day-to-day admin/dev work on this exact box. A container platform is built for
   running a *defined service*, not for hosting an open-ended, persistent, remotely-
   promptable AI agent with real OS access. This category needs an actual VPS (a DigitalOcean
   Droplet, or continuing on Hetzner), not a PaaS.

This means a "full migration to DigitalOcean" is really a **hybrid**: a Droplet for category
2, App Platform for category 1, and a Managed Database underneath both. See the phased
roadmap below for the exact split.

## Platform comparison, conclusion

Evaluated Hetzner (current), DigitalOcean, Render, Railway, Fly.io, AWS, Azure, Google Cloud
SQL, Hostinger, and Neon/Supabase (database-specific). Short version:

- **DigitalOcean** — the recommended target. Same PaaS-plus-managed-Postgres shape as Render,
  but more established, has a real VPS product (Droplets) alongside the PaaS layer for the
  Claude-CLI-dependent side, and excellent documentation for a team that will include less
  senior developers.
- **Render** — a very close second on pure technical fit, and there's already real prior art
  in this org: `eoxssecondbrain/raj-wiki-vault`'s `render.yaml` is a genuine working Render
  deployment (an always-on MCP server + a pipeline service + a cron job) with hard-won
  lessons already documented in that file.
- **AWS/Azure/Google Cloud SQL** — ruled out primarily on operational-complexity grounds, not
  cost: significantly larger surface area (IAM, VPCs, security groups), a well-documented
  history of real security incidents traced to cloud misconfiguration on exactly this kind of
  platform, and more power than a small team trying to *reduce* its own ops burden actually
  wants. Worth revisiting only if a specific future client requires one of these.
- **Hostinger** — not a peer to the others; a budget web-hosting company, not a production
  cloud infrastructure provider. Not on the shortlist.
- **Neon** — worth keeping in mind specifically for its database-branching feature (spin up a
  full disposable copy of a database for one developer or one test), independent of whatever
  platform ends up hosting the application code.

Rough cost, all estimates (verify current pricing before committing): current Hetzner
~$6-10/mo; a DigitalOcean hybrid (Droplet + App Platform + Managed Database) ~$50-55/mo —
3-6× the current cost, buying managed backups, automatic TLS, and no more manual
systemd/nginx work.

## The phased migration roadmap

Full detail (numbered steps per phase, what can go wrong at each one) lives in the artifact
published during planning — reproduced here at the phase level so it survives outside that
session:

0. **Decisions to lock in** — DO account/billing, Droplet region (close to wherever the
   Managed Database ends up), whether to bundle the domain+Streamable-HTTP work into this
   same migration (recommended), the secrets split between the Droplet's own `.env` and App
   Platform's secret manager.
1. **Provision the Droplet** — Claude CLI + Codex CLI + fresh auth, the repo, the two
   Claude-CLI-dependent systemd units (`eoxs-wiki-pipeline`, `eoxs-wiki-mcp`), installed but
   dark until cutover. Near-exact lift of what already exists on Hetzner.
2. **Provision the Managed Database** — one cluster, three logical databases (`eoxs_wiki`,
   `eoxs_wiki_staging`, `eoxs_frontend_threads`), migrate roles then data via
   `pg_dumpall`/`pg_dump`, verify row counts before trusting the copy.
3. **Provision App Platform services** — `eoxs-mcp`, `eoxs-ingestion`, `eoxs-sweep` (as a
   Scheduled Job), and `eoxs-frontend-threads` (its own separate repo), deployed dark.
4. **Domain + transport** — buy the domain if not already done, point it at the new
   deployment, add real Streamable-HTTP transport to `mcp_server/http_server.py` alongside
   the existing SSE routes (the fix for the frontend integration currently needing a local
   bridge process on the frontend developer's own Render instance). Side benefit: retires the
   6-day IP-certificate renewal cycle permanently.
5. **Parallel testing** — Hetzner keeps serving all real traffic throughout. Exercise every
   path against the new stack: MCP tool calls across all 4 identities, webhook receipt, one
   full sweep cycle, one full wiki-pipeline cycle (the highest-risk piece — the `claude -p` ↔
   wiki-mcp connection has documented, hard-won fragility even on the current box), and the
   frontend-threads save/read round-trip.
6. **Cutover** — point DNS at the new deployment (invisible to existing connector holders if
   the domain stays the same), or redistribute URLs if it changed. Defined observation window.
7. **Decommission Hetzner** — only after 1-2 weeks stable. Update
   `docs/local-dev-and-team-onboarding.md`'s tunnel target (it currently points at the
   Hetzner IP — this is the one doc guaranteed to go stale the moment this migration
   happens), rotate any Hetzner-only credentials, then cancel the VPS.
8. **Post-migration hardening** — point the monitoring setup (below) at the new
   infrastructure, confirm the Managed Database's automatic backup/PITR settings match what's
   actually wanted.

**Status: partially executed, in a different shape than planned.** A DigitalOcean droplet
was provisioned and everything moved to it on 2026-08-20 as a lift-and-shift — so phase 1
is done (though the droplet runs all services, not just the Claude-CLI ones), while phases
2-4 are the current work. Phases 5-8 are untouched. `docs/migration-status.md` is the live
progress log; this section remains the plan and the reasoning.

## The backup plan (independent of whether the DO migration happens)

No backup exists today for either database. If DigitalOcean's Managed Database happens
first, this becomes moot (backups included). If it doesn't happen soon, the standalone plan:

- Destination: object storage (Hetzner Object Storage, not a second VPS — a second VPS would
  just be a second thing to maintain for no real benefit here).
- What gets dumped: `pg_dumpall --globals-only` (roles) once, plus `pg_dump -Fc` per database.
- Encryption: recommended before upload, given this is confidential company data.
- Schedule: daily, via a systemd timer matching `eoxs-sweep.timer`'s shape.
- Retention: roughly 14 daily + 8 weekly + 6 monthly, enforced via a bucket lifecycle rule,
  not custom deletion code.
- Not done until a real restore has been tested once, and the restore procedure written down.

**Status: not started — still the single largest unmitigated risk.** Neither the Hetzner box
nor the new droplet has ever had a backup. The DigitalOcean Managed Database (in progress as
of 2026-08-21) makes this a configuration checkbox rather than a build, which is the main
reliability argument for doing it.

## Monitoring (independent of the migration, cheap either way)

Two different things need watching, since they're different failure modes:

- **Uptime**: an external ping (UptimeRobot's free tier is enough at this scale) against the
  existing `/health` endpoint on the ingestion server.
- **"Did the scheduled jobs actually run"**: a dead-man's-switch pattern (Healthchecks.io free
  tier) — the sweep and wiki-pipeline scripts ping a unique URL on success; if the expected
  ping doesn't arrive in the expected window, it alerts. This is the only thing that would
  catch a timer that silently stopped firing, which plain uptime pinging never would.

**Status: not started.** Both are free at current scale.

## Open decisions, not yet made

- **`eoxs-frontend-threads` identity model.** Currently uses one secret per end user (mirrors
  `claude-notes-vault`'s proven pattern). Whether that's the right long-term model depends on
  whether the actual caller on the frontend side is an LLM/agent deciding on its own to save
  (in which case per-user secrets are the right security boundary — an LLM's self-reported
  identity can't be trusted) or the frontend's own deterministic backend code, which already
  knows its real logged-in user (in which case a single shared secret + a trusted username
  parameter would dramatically cut provisioning overhead for large user counts). This was
  raised and left open pending discussion on the frontend team's side — check back on it
  before scaling past a handful of users.
- **MCP connector IP whitelisting.** Investigated; concluded that IP allowlisting doesn't
  restrict claude.ai's own connector traffic (it originates from Anthropic's infrastructure,
  not the end user's IP) — the real lever for "who can use this connector" is per-person URL
  secrets with revocation, which the existing per-identity-secret pattern already extends to
  naturally. Not built.
- **RESOLVED 2026-08-12** (was listed here as an unresolved bug): the raw-ingestion sweep
  used to keep writing new rows to `tickets` after tickets were removed from every MCP tool
  and every historical row deleted — fixed by commit `3fa4c16`, which removed
  `tickets_fetcher.py`'s/`invoice_fetcher.py`'s registration from `run_full_sweep()`'s source
  list. Confirmed live 2026-08-20 by grepping `ingestion/server.py` — no call to either
  fetcher remains in the sweep path, only historical comments. `docs/raw-ingestion.md` §12
  and this section both went stale here after the fix landed elsewhere; see
  `docs/postgres-database.md` §7 for the corroborating row-count note.
