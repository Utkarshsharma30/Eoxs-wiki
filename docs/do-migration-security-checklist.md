# DigitalOcean Migration Security Checklist

*For Talal — organized by the phases in `docs/infrastructure-roadmap.md`, cross-checked
against the box's actual live state on 2026-08-20. This is a working checklist, not a
standalone spec — read `docs/infrastructure-roadmap.md` first for the full reasoning behind
the phased plan; this document only adds the security lens on top of it.*

## Two things that changed since the roadmap doc was written

**Resolved, not carried forward**: the roadmap doc flags the raw-ingestion sweep writing
stray rows to `tickets` as an open, unresolved bug. It isn't — confirmed live by grepping
`ingestion/server.py`: commit `3fa4c16` removed `tickets_fetcher.py`'s registration from the
sweep on 2026-08-12. Nothing to fix during this migration window. (`docs/infrastructure-roadmap.md`
and `docs/raw-ingestion.md` §12 have both been corrected to match.)

**A real gap, now closed**: none of Talal's 2026-08-15 security hardening on the live Hetzner
box was ever committed to this repo until 2026-08-20 — `server_tokens off`, TLS locked to
1.2/1.3, security headers + HSTS, MCP-secret masking in the nginx access log, and the
fail2ban sshd jail were all hand-applied directly on the box. A fresh Droplet built from
`deploy/setup.sh` alone would **not** reproduce any of it. Now captured in
[`deploy/HARDENING.md`](../deploy/HARDENING.md) + [`deploy/nginx.conf`](../deploy/nginx.conf) +
[`deploy/fail2ban-jail.local`](../deploy/fail2ban-jail.local) — treat that file as the source
of truth for Phase 1 below, not tribal memory.

---

## Phase 0 — Before anything is provisioned

- [ ] **Decide** the secrets split: what goes in the Droplet's own `.env` vs. App Platform's
  secret manager — and check App Platform build logs don't echo secrets during deploy.
- [ ] **Decide** Droplet region relative to the Managed Database (same region — don't let DB
  traffic cross regions unencrypted-by-default over the public internet).

## Phase 1 — Droplet

- [ ] **Re-apply everything already hardened on Hetzner** — none of it carries over on a fresh
  box automatically: `server_tokens off`, TLS locked to 1.2/1.3, security headers + HSTS,
  MCP-secret masking in the access log, `.htpasswd` permissions, fail2ban + sshd jail,
  key-only admin (no password auth), deploy account scoped down. Work directly from
  [`deploy/HARDENING.md`](../deploy/HARDENING.md), then diff the new box's config against it
  — don't reconstruct from memory.
- [ ] **Confirm before replicating**: the live box's `sudo` group is `deploy`, `ayan`,
  `apexadmin` — not just `deploy` as `docs/backend-server.md` §1 currently documents. Confirm
  with Ayan/Talal whether all three should carry over 1:1 to the new Droplet.
- [ ] DO's own Cloud Firewall configured at the platform level, not just fail2ban at the host
  level — two independent layers, same principle as Cruz's own DB-level + AI-response-level
  access check.
- [ ] Fresh SSH keys for the new box — don't reuse Hetzner keys.

## Phase 2 — Managed Database

- [ ] Confirm the DB cluster has no public network access — trusted-sources-only (Droplet +
  App Platform services explicitly allowlisted, nothing else).
- [ ] Verify roles/permissions after `pg_dumpall`/`pg_dump` migration match the original —
  row-by-row, not just table-count — a lift-and-shift can accidentally grant broader access
  than intended if role grants aren't checked individually. Live roles today: `eoxs_app`
  (full DML, owns all tables) and `eoxs_readonly` (SELECT-only, used by pgweb) — confirm both
  land with identical scope, not wider.
- [ ] Confirm DO's automatic backup/PITR settings actually meet what's wanted (retention
  window, encryption at rest) — don't assume the default is sufficient. No backup exists for
  either database today, so this migration is also the first real backup this system will
  ever have.

## Phase 3 — App Platform

- [ ] Same secret-masking discipline in whatever logging App Platform provides as exists in
  the nginx access log today (the `masked` log format redacts the MCP URL secret before it's
  ever written — see `deploy/nginx.conf`).
- [ ] Revisit the `eoxs-frontend-threads` identity-model decision (per-user secret vs. shared
  secret + trusted param) before scaling — explicitly flagged as unresolved in the roadmap,
  and it's a real security-boundary decision, not just an efficiency one.

## Phase 4 — Domain + transport

- [ ] New domain's TLS setup replicates the same TLS 1.2/1.3-only + HSTS posture — not just
  "the cert renews automatically now so security is handled."
- [ ] **Highest bypass risk in this phase**: when Streamable-HTTP is added alongside SSE in
  `mcp_server/http_server.py`, the new transport path gets the exact same per-identity access
  control as SSE — a new code path is exactly where a bypass gets missed.

## Phase 5 — Parallel testing (the real security regression test)

- [ ] **Highest-value test in the whole migration**: explicitly re-verify all **5** MCP
  identity tiers — `full` (Raj), `hr`, `general`, `intern`, and `staging_qa` — enforce the
  same redaction/sensitivity rules on the new stack, not just that the app "works." The
  roadmap doc says 4 identities; `staging_qa` was added 2026-08-13 (a sandbox routed to the
  staging DB for the employee/asset write tools, see `docs/backend-server.md` §5.3) and
  postdates that doc — don't let it get skipped in the re-verification pass just because it's
  newer. A functional bug is annoying; an access-control regression is a leak.

## Phases 6–7 — Cutover & decommission

- [ ] Rotate every Hetzner-only credential after decommission — DB passwords, pgweb
  basic-auth, any API keys scoped to that box. Don't let old credentials linger as valid
  after the box they protected is gone.
- [ ] Confirm nothing in the org still points at the raw IP once DNS cutover happens.

---

## One functional item worth fixing during this window

~~The tickets table bug~~ — already fixed (see above). Nothing currently open here; if a new
functional gap turns up during the migration, it belongs in this section, not silently fixed
and forgotten.

---

## Daily workflow with Claude across the three DO surfaces

They're not symmetric — only one of the three gives you a shell.

**Droplet** — same model as Hetzner today: SSH / VS Code Remote-SSH, `cd` into the repo, run
`claude` fresh per task, exit when done (don't leave it parked — same 4GB-box discipline
applies, maybe more important if the new box is sized similarly). This is where
`eoxs-wiki-pipeline` and `eoxs-wiki-mcp` live, so this is also where you'd debug the
`claude -p` subprocess chain the roadmap flags as the highest-risk piece.

**App Platform** — no SSH, no shell, ever. It's git-push-to-deploy. Daily work here means:
- Local repo commits/pushes trigger the deploy — the actual "editing" happens in the normal
  git workflow, Claude Code included, same as any other codebase.
- Logs and status come from `doctl` (DO's CLI) instead of `journalctl`:
  `doctl apps logs <app-id> --type=run` (runtime logs) or `--type=build` (deploy logs),
  `doctl apps list-deployments <app-id>`.
- A Claude Code session (on the Droplet or your own machine, with `doctl` authenticated) can
  run these commands the same way it runs `journalctl` today.

**Managed Database** — also no shell. Two ways in: `psql`/pgweb over the connection string DO
gives you (same commands as above, just a different host), or DO's own web console for
backups, PITR, and performance insights. Give Claude Code the connection string via env var
(never hardcoded in a prompt or committed) and it can run `pg_stat_activity` / `SHOW` queries
against it directly.

Net effect: the Droplet workflow barely changes from today. The App Platform and Managed
Database workflow shifts from "SSH in and look" to "query the platform's API/CLI" — a good
trade, since `doctl` and the DO console give a documented, discoverable surface instead of
having to already know which file lives where in `/etc`.
