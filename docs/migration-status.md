# Migration Status — Where This Actually Stands

*Live status of the Hetzner → DigitalOcean migration and the re-architecture that
follows it. Written 2026-08-21. If you are picking this up cold, read this file first —
it is the "what's on the plate right now" document. The deep references are
`docs/backend-server.md` (Hetzner) and `docs/backend-server-digitalocean.md` (the new box).
`docs/infrastructure-roadmap.md` (the original target-architecture reasoning: why a
DigitalOcean hybrid — Droplet + App Platform + Managed Database — not a pure PaaS move, and
the platform comparison behind picking DigitalOcean) was deleted 2026-08-26 as superseded —
this file is now the sole live status/plan; §4 below carries the remaining execution steps
forward.*

---

## The one-paragraph version

The 2026-08-20 migration by Talal was a **lift-and-shift**: one DigitalOcean droplet
running exactly what Hetzner ran. It was executed well and verified — see §1. But the
roadmap called for a **hybrid split** (managed database + App Platform services + a
droplet for the Claude-CLI work), which the lift-and-shift did not do. So there are now
two tracks: finishing the migration (cut-over from Hetzner), and re-architecting into the
split that was always intended. **Both Hetzner and the new droplet are live and ingesting
right now**, which is the single most important operational fact on this page — see §3.

---

## 1. Migration QA — verified 2026-08-21

Checked against the live host, not the handover notes.

**Sound.** All three databases migrated with data intact (`eoxs_wiki` 503 MB,
`eoxs_wiki_staging` 317 MB, `eoxs_frontend_threads` 8 MB) and all six roles with working
credentials, so no `.env` edits were needed. Six services active on 8090–8095; MCP returns
200 and holds the SSE stream. Firewall 22/80/443 default-deny, SSH key-only, fail2ban
active. Certificate valid to 2026-11-18 with a passing `certbot renew --dry-run`. The
13 GB vault (47,451 files) copied intact.

**Two things are better than Hetzner:** `deploy` is no longer in the `sudo` group (a
hardening gap flagged in the old docs), and TLS moved from a ~6-day IP certificate to a
normal 90-day certificate.

### What was broken, and what was done about it

| # | Finding | Status |
|---|---|---|
| 1 | **Wiki pipeline dead.** `claude` CLI never installed → `FileNotFoundError` every 6h since 2026-08-20. Raw ingestion was unaffected, which masked it: source data kept arriving while `wiki_pages` stayed frozen. | **Fixed.** CLI installed + `/usr/local/bin/claude` symlink (the installer only writes `~/.bashrc`, which systemd never reads). Logged in as `deploy`. Pipeline ran to `Finished`. |
| 2 | **Zoho paging aborts on 404.** Zoho answers an offset past its searchable window with 404, not an empty page; 404 is non-retryable, so the whole account's sweep died. Hit `isha_zoho` at `start=3401` after seven clean runs. | **Fixed** (`4d4bac7`). Verified live: `isha_zoho` 0 → 7 messages, `error: 0`. **Pre-existing, not migration damage** — also applied to Hetzner. |
| 3 | **`gh` CLI missing.** Config and token migrated, binary did not — git pushes failed. | **Fixed.** Installed; the migrated token authenticated with no new credential. |
| 4 | **Zoho OAuth redirect URI** was missing from the handover list (only Gmail was listed), but Zoho builds its callback from the same `OAUTH_REDIRECT_BASE_URL`. | Both added. Google **verified** accepting; Zoho **unverified** — needs a real invite to confirm. Low priority: affects only *new* connects. |
| 5 | **Parallel-write divergence.** Both boxes live and ingesting into separate copies. | **Open, growing.** See §3. |

**The pattern worth remembering from #1 and #3: config migrated, binaries did not.** Assume
anything that shells out to a CLI is suspect on a freshly-migrated box.

### Carried over from Hetzner (pre-existing, not introduced)

- pgweb's Postgres password is visible to any local user via `ps` (interpolated into the
  systemd unit's `ExecStart=`).
- The MCP server connects as `eoxs_app` (read-write), not `eoxs_readonly` — "read-only" is
  a code convention, not database-enforced.
- `deploy/nginx-https.conf` still carries the Hetzner IP in 4 places; the tracked config is
  not deployable on the new box as-is.
- `.gitignore` covers `.env` but **not** `.env.bak*`. A full copy of production credentials
  sits untracked-but-not-ignored, one `git add -A` from GitHub. **Still open.**

---

## 2. Done this session (all pushed to `main`)

| Commit | What |
|---|---|
| `4d4bac7` | Zoho paging 404 fix + `docs/backend-server-digitalocean.md` |
| `f6e01e9` | Container-platform deployability: `0.0.0.0` bind + `PGSSLMODE` |
| `88dbfb9` | `.do/app.yaml` — three-component App Platform spec |
| `18ba77e` | `MCP_AYAN_URL_SECRET` added to the spec (it is **required**, not optional) |

**`f6e01e9` is the one to understand.** Two changes, both no-ops on the droplet, both
required off it:

- `mcp_server/http_server.py` bound uvicorn to `127.0.0.1`. Correct behind nginx, fatal on
  App Platform — the platform's load balancer connects from *outside* the container, a
  loopback bind refuses it, the health check fails, and the container is killed as
  "misbehaving". That was the observed first-deploy failure. Now `0.0.0.0`, overridable via
  `MCP_HTTP_HOST`. Still not publicly reachable: ufw allows only 22/80/443.
- `ingestion/db.py` and `mcp_server/db.py` passed no `sslmode`. **DO Managed Postgres
  refuses plaintext connections.** Now read `PGSSLMODE`, defaulting to `prefer` so
  local/loopback is unchanged. Set `PGSSLMODE=require` in the managed environment.

---

## 3. ⚠️ The live risk: both boxes are writing

Hetzner and the new droplet are **both ingesting into their own copies of `eoxs_wiki`
right now**. The new box holds rows Hetzner does not:

- 2026-08-21 ~13:00 — 128 divergent rows
- 2026-08-21 ~17:00 — **219 divergent rows**

It grows every 2 hours. The handover plan ("fresh dump + filestore delta sync") assumed the
new box sat idle, so **a straight dump-and-restore from Hetzner would silently overwrite
every one of those rows**.

Both timers were disabled on 2026-08-21 to stop this, then **deliberately re-enabled** the
same day. That is a valid choice, but it has a consequence that must be planned for:

> **Cut-over is now a MERGE, not a restore.** Decide explicitly — before any dump — whether
> the new box's divergent rows are kept or discarded.

To pause divergence again:
```bash
sudo systemctl disable --now eoxs-sweep.timer eoxs-wiki-pipeline.timer
```
And to resume at cut-over (**do not forget this** — otherwise the new box silently stops
ingesting once Hetzner retires):
```bash
sudo systemctl enable --now eoxs-sweep.timer eoxs-wiki-pipeline.timer
```

---

## 4. What is left, in execution order

### Immediate — no dependencies

1. **`.gitignore`: add `.env.bak*`.** One line. Prevents a full production-credential leak.
2. **DNS: `mcp.askcruz.com` → `68.183.181.25`** (A record, GoDaddy — DNS is *not* on DO).
   ⚠️ `askcruz.com` is a **live Vercel site** (Jaskeerat's frontend): apex `216.150.1.1`,
   `www` → `vercel-dns`, both HTTP 200. **Add a subdomain only. Do not touch the apex or
   `www`.** Then nginx `server_name` + a certbot cert for the new name.
   *Why this is urgent: every connector URL handed out on `nip.io` must be manually
   reissued later. A real hostname makes the eventual migration a DNS change instead.*
3. **Verify the Zoho redirect URI** with a real invite (`python -m ingestion.oauth_zoho
   invite …`). Google is already verified.

### Re-architecture (needs DigitalOcean console access)

4. **Managed Postgres** — cluster exists (`eoxs-db-live-cluster`, SGP1). Create the three
   logical databases; leave `doadmin`/`defaultdb` for administration only. Services keep
   connecting as `eoxs_app`. Add every droplet + the app to **Trusted Sources**. Confirm
   backups and retention explicitly, then **do one real restore test** — a backup nobody has
   restored is not a backup. *This closes the no-backup gap that has existed on both boxes
   since the beginning; it is the single biggest reliability win available.*
5. **App Platform** — create from `.do/app.yaml`. The web UI **ignores the spec** and
   autodetects a single component with no run command (confirmed twice); use
   `doctl apps create --spec .do/app.yaml` instead. Then fill every `REPLACE_IN_CONSOLE`
   secret in the console, marked Encrypted. `PGHOST` must be the **private** (`private-…`)
   host from the cluster's VPC tab.
6. **Agent droplet** — **reuse the existing `Live-droplet`.** Once MCP/ingestion/sweep move
   to App Platform, it is left running only `eoxs-wiki-pipeline` + `eoxs-wiki-mcp`, which is
   exactly the agent box. It already has the authenticated Claude CLI, the 13 GB vault, `gh`,
   the repo and the hardening — provisioning a fresh droplet means redoing all of it and
   risking the same "config without binaries" trap. After cut-over, stop and disable the four
   services that moved so nothing runs twice.
7. **Reliability** — dead-man's-switch on both scheduled jobs (Healthchecks.io), uptime
   check on `/health` (UptimeRobot), and alerting on sweep *errors*, not just
   non-execution. *The pipeline failed every run for a day and nothing alerted; the
   `isha_zoho` 404 sat inside a successful-looking sweep. Plain uptime pinging catches
   neither.*
8. **Cut-over** — parallel-test all identities, webhooks, one full sweep, one full pipeline
   cycle; reconcile the diverged data per §3; repoint DNS; observe; then decommission.

---

## 5. Notes for whoever does the next step

**Ordering that matters:** database first, then services. Never point new services at the
old database "temporarily" — a half-migrated state means two services writing two
databases, which is how §3 happened in the first place.

**Do not break the webhooks.** `/webhook/gmail`, `/webhook/fireflies`, `/webhook/fathom`
are called by *external* services. If the ingestion server moves, those registrations must
be updated or real-time ingestion silently stops — and the 2-hourly sweep will mask it for
up to two hours.

**Baseline row counts to verify after any restore:** `call_segments` 183,529 ·
`email_messages` 66,388 · `email_threads` 34,539 · `wiki_pages` 1,800.

**Data sizes, for sizing decisions:** the whole `eoxs_wiki` database is ~504 MB, of which
the three email tables are 359 MB (71%). Raj + Ron Gmail together are ~182 MB in Postgres.
The same email data in `raj-wiki-vault` is ~4.2 GB — ~17× larger, because the vault keeps
original binary attachments while Postgres stores only extracted text. Of the vault's
13 GB, **7.2 GB is `.git` history** and 5.2 GB is content. Managed-database sizing is not a
constraint at this scale.

**`LINEAR_API_KEY` / `LINEAR_TEAM_KEY` are dead.** The code reads only the `_EDB_`
variants. Do not carry the stale pair into any new environment.

**Reproduce the hardening.** `deploy/HARDENING.md` exists because hand-applied hardening
was nearly lost once. A fresh box built from `deploy/setup.sh` alone does **not** reproduce
it.

---

*Keep this file current as items close — it is the entry point for anyone new to this work.*
