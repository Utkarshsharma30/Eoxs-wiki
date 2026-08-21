# Backend Server — DigitalOcean

*Deep technical reference for the DigitalOcean host that `eoxs-wiki-db` was migrated to on 2026-08-20.*

> **Read this first.** This document describes the **new** DigitalOcean box. The older
> `docs/backend-server.md` describes the **Hetzner** box (`5.223.44.95`), which is
> **still live and still the system of record** until cut-over completes. Both documents
> are currently accurate about their own machine. Do not delete `backend-server.md`
> until the Hetzner box is retired; until then, treat *that* file as authoritative for
> anything a user is actually hitting today.
>
> Everything below was verified directly against the running host on 2026-08-21, not
> copied forward from the Hetzner doc. Where the two boxes differ, the difference is
> called out explicitly rather than silently overwritten.

---

## 1. Where this runs

One DigitalOcean droplet: hostname `Live-droplet`, public IP **`68.183.181.25`**, also
reachable at **`68-183-181-25.nip.io`** (a DNS wildcard service that resolves the
hostname straight back to that IP — it is *not* an EOXS-owned domain, see §10).
Ubuntu **24.04.4 LTS**, kernel 6.8. **2 vCPU, 3.8 GB RAM, 77 GB disk, 2 GB swapfile.**

Repo lives at `/home/deploy/eoxs-wiki-db`. Every application service runs as Linux user
`deploy` (**uid 1001**).

**Improvement over the Hetzner box, worth knowing:** on Hetzner, `deploy` was uid 1000
*and a member of the `sudo` group* — flagged in `backend-server.md` §1 as a real
hardening gap, since a compromised service would have inherited sudo-capable access.
On this box that is fixed: `id deploy` returns `groups=1001(deploy)` and nothing else.
The services now run as a genuinely unprivileged account. Administrative users are
separate: `live-talal` (uid 1000), `nidhi` (1002), `jaskeerat` (1003).

## 2. What's running here, service by service

Same unit set as Hetzner. Every unit sets `User=deploy`, `Group=deploy`,
`WorkingDirectory=/home/deploy/eoxs-wiki-db`,
`EnvironmentFile=/home/deploy/eoxs-wiki-db/.env`, and logs to journald with a distinct
`SyslogIdentifier` — there are no separate log files, `journalctl -u <unit>` is the only
place to look.

| Unit | Port | Type | What it runs | State (2026-08-21) |
|---|---|---|---|---|
| `eoxs-ingestion.service` | 8090 | simple, `Restart=always` | `python3 -m ingestion.server` | active |
| `eoxs-mcp.service` | 8091 | simple, `Restart=always` | `python3 -m mcp_server.http_server` | active |
| `pgweb.service` | 8092 | simple, `Restart=always` | `bin/pgweb … --readonly --lock-session` on `eoxs_wiki` | active |
| `eoxs-wiki-mcp.service` | 8093 | simple, `Restart=always` | `python3 -m wiki_ingestion.mcp_http_server` | active |
| `eoxs-frontend-threads.service` | 8094 | simple, `Restart=always` | `python3 -m mcp_server` (sibling repo) | active |
| `pgweb-staging.service` | 8095 | simple, `Restart=always` | same as pgweb, on `eoxs_wiki_staging` | active |
| `eoxs-sweep.service` | — | oneshot | `python3 -m ingestion.server --sweep` | **timer disabled — see §8** |
| `eoxs-wiki-pipeline.service` | — | oneshot, `TimeoutStartSec=infinity` | `python3 -m wiki_ingestion.run_pipeline` | **timer disabled + blocked, see §7/§8** |
| `nginx.service` | 80/443 | system-provided | reverse proxy (§4) | active |

Binding: **8090 binds `0.0.0.0`**; every other app port binds `127.0.0.1` only and is
reachable solely through nginx. Postgres listens on `127.0.0.1:5432` (and `[::1]`), not
publicly.

Timer cadence (unchanged from Hetzner): `eoxs-sweep.timer` is
`OnCalendar=00/2:00:00`, `eoxs-wiki-pipeline.timer` is `OnCalendar=00/6:00:00`, both with
`RandomizedDelaySec=120` and `Persistent=true`. **Both are currently disabled** — §8.

The sibling `eoxs-frontend-threads` repo (own clone at `/home/deploy/eoxs-frontend-threads`,
own venv, own service, own `eoxs_frontend_threads` database on this same Postgres) runs
here too but is **not** part of this repo's codebase — same deliberate split as on Hetzner.

**Carried-over secret-hygiene issue (still present, verified):** `pgweb.service` and
`pgweb-staging.service` interpolate `${PGWEB_DB_PASSWORD}` into `ExecStart=`, so the
Postgres password for `eoxs_readonly` is visible in plaintext to **any local user** via
`ps aux`. Confirmed live on this box. The Hetzner doc flagged this and the migration
carried it across unchanged. Fix is the same as proposed there: wrap the invocation in a
small shell script that reads the env var internally instead of interpolating it into the
unit file.

## 3. The Ingestion Server

Unchanged from Hetzner in every respect except the host it answers on — see
`docs/backend-server.md` §3 for the full route table, the `_sweep_lock` behaviour, and
the per-source webhook/no-webhook breakdown. Port is still `INGESTION_SERVER_PORT`
(default **8090**), still binds `0.0.0.0`, still fronted by nginx.

The production sweep entrypoint remains `python -m ingestion.server --sweep`, run by the
timer — not the HTTP `/trigger/manual` route.

## 4. Reverse Proxy & TLS (nginx)

Live config: `/etc/nginx/sites-enabled/eoxs-ingestion`. **Rebuilt for this host** — it is
*not* byte-identical to the repo's `deploy/nginx-https.conf`, which still carries the
Hetzner IP. Regenerate or update that tracked file before treating it as deployable here.

- **Port 80** (`default_server`): serves `/.well-known/acme-challenge/` from
  `/var/www/certbot`, 301-redirects everything else to HTTPS.
- **Port 443** (`ssl default_server`): `server_name 68.183.181.25 68-183-181-25.nip.io` —
  **both the bare IP and the nip.io hostname serve every path**, so a connector URL
  written either way works. Cert at
  `/etc/letsencrypt/live/68-183-181-25.nip.io/fullchain.pem`.

Location blocks (identical shape to Hetzner):

| Path | Proxies to | Notes |
|---|---|---|
| `/mcp/` | `127.0.0.1:8091` | no trailing slash, deliberately — the SSE app bakes `/mcp` into self-referential URLs. `proxy_buffering off`, `proxy_read_timeout 3600s`, HTTP/1.1 |
| `/threads/` | `127.0.0.1:8094` | sibling frontend-threads MCP |
| `/dbadmin/` | `127.0.0.1:8092/` | trailing slash strips prefix. `auth_basic` against `/etc/nginx/.htpasswd_dbadmin` |
| `/dbadmin-staging/` | `127.0.0.1:8095/` | same auth file, browses `eoxs_wiki_staging` |
| `/` | `127.0.0.1:8090` | ingestion server, catch-all |

**TLS is materially better here than on Hetzner.** The Hetzner box used a short-lived
IP-address certificate on a roughly 6–7 day renewal cycle. This box has a normal
**90-day Let's Encrypt certificate** for the nip.io hostname (issued 2026-08-20, expires
2026-11-18), via the `webroot` authenticator. Renewal is the distro `certbot` package
with `certbot.timer` enabled, and `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh`
runs `systemctl reload nginx` after each success. A full `certbot renew --dry-run` was
run on 2026-08-21 and passed end to end.

## 5. MCP Server

The serving layer, the five HTTP identities, the clearance model, the redaction safety
net, the write paths, `staging_qa`, and the `_environment` field are **all unchanged by
the migration** — this is application logic, not host configuration. See
`docs/backend-server.md` §5 through §5.4 for the authoritative detail; duplicating it
here would just create two copies to drift apart.

What is worth restating, because it is the thing people ask about during a host move:

**The app is host-agnostic by design.** Connector endpoints are built from the request
path, and the host is never hard-coded — the only literal old-host strings left in
`mcp_server/http_server.py` are in a docstring (line 83), not in any code path. That is
why every existing connector secret works on this box with no code change; only the host
portion of the URL a user has saved needs updating:

```
https://5-223-44-95.nip.io/mcp/<secret>/sse   →   https://68-183-181-25.nip.io/mcp/<secret>/sse
                                                  (the /mcp/<secret>/sse part is unchanged;
                                                   the secret itself does not rotate)
```

Verified live on this box: `/mcp/<MCP_URL_SECRET>/sse` and `/mcp/<MCP_GENERAL_URL_SECRET>/sse`
both return **200** and hold the SSE stream open.

**Known gap, carried over unchanged:** the MCP server still connects as `eoxs_app`
(read-write), not the correctly-scoped `eoxs_readonly` role. The "read-only" guarantee
remains a code-discipline convention, not a database-enforced one. Same hardening
opportunity as before.

## 6. Database

PostgreSQL **16.15** (Ubuntu build), local-only. All three databases migrated with their
data intact, and all six roles with their existing passwords — so every `.env` credential
kept working without edits.

| Database | Size (2026-08-21) | Purpose |
|---|---|---|
| `eoxs_wiki` | 503 MB | live |
| `eoxs_wiki_staging` | 317 MB | staging / QA sandbox (§5.3 of the Hetzner doc) |
| `eoxs_frontend_threads` | 8 MB | sibling frontend-threads system |

Roles: `eoxs_app`, `eoxs_readonly`, `local_dev`, `aditya_dev`, `jagriti_dev`, `postgres`.
None are superusers except `postgres`.

Performance tuning matched to the old box: `shared_buffers = 512MB`,
`effective_cache_size = 1536MB`, `work_mem = 8MB`, `maintenance_work_mem = 128MB`,
`max_connections = 100`.

Spot-check of live row counts: `call_segments` 183,529 · `email_messages` 66,388 ·
`email_threads` 34,539 · `wiki_pages` 1,800.

## 7. The `claude` CLI dependency — the one thing the migration missed

`eoxs-wiki-pipeline` shells out to the **`claude` CLI** for every sub-agent it spawns
(`wiki_ingestion/headless_agent.py`, `_invoke_once`). The migration copied repos, `.env`
files and venvs, but **not** the CLI or its credential — so from 2026-08-20 the pipeline
failed on every run with:

```
FileNotFoundError: [Errno 2] No such file or directory: 'claude'
```

Raw ingestion (`eoxs-sweep`) was unaffected throughout — it is pure Python. The visible
symptom was that new source data kept arriving while `wiki_pages` stayed frozen.

**Two separate requirements, easy to conflate:**

1. **The binary must be on *systemd's* PATH.** Installed 2026-08-21 (native installer,
   v2.1.238) at `/home/deploy/.local/bin/claude`. The installer appends a PATH line to
   `~/.bashrc`, but **systemd units never read `.bashrc`** — so a
   `/usr/local/bin/claude` symlink is what actually makes it resolvable. That symlink is
   load-bearing; do not remove it.
2. **The `deploy` user must be logged in.** There is no `ANTHROPIC_API_KEY` in `.env` for
   this purpose and none in the units — the CLI uses an interactive login stored in
   `deploy`'s `~/.claude`, which was never migrated.

**As of 2026-08-21 requirement 2 is still outstanding** — the CLI answers
`Not logged in · Please run /login`, so the pipeline remains blocked. Completing it needs
an interactive session:

```bash
sudo -u deploy -i     # must be deploy — systemd runs the pipeline as deploy
claude                # launches the TUI
# then, inside the TUI: /login
```

Test the way systemd will actually invoke it — a login shell can succeed where systemd
fails:

```bash
sudo -u deploy env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/home/deploy claude -p 'hi' --output-format text
```

Note that `ANTHROPIC_API_KEY` *does* exist in `.env` and is used by the Python code for
classification and synthesis — that is a different thing from the CLI's credential, and
having it set does not make the CLI authenticated.

## 8. Cut-over state — why both timers are disabled

**Both boxes were live and ingesting simultaneously.** This box had already written 128
new `wiki_ingest_seen` rows and was pulling email current to the day, while Hetzner kept
serving every user. That breaks the stated cut-over plan ("one fresh dump + filestore
delta sync"), which assumed this box sat idle — a straight dump-and-restore from Hetzner
would silently overwrite whatever this box had independently written.

On 2026-08-21 both timers were therefore disabled here, making Hetzner the single writer:

```bash
systemctl disable --now eoxs-sweep.timer eoxs-wiki-pipeline.timer
```

The six long-running services were deliberately left up, so the box still serves and can
be tested. **Re-enable both timers at cut-over** — otherwise this box silently stops
ingesting the moment Hetzner is retired:

```bash
systemctl enable --now eoxs-sweep.timer eoxs-wiki-pipeline.timer
```

Remaining cut-over checklist:

1. Finish the `claude` CLI login (§7) — otherwise the wiki pipeline is dead on arrival.
2. Add the OAuth redirect URIs (§9) in **both** consoles.
3. Each user repoints their connector host (§5). The secret does not change.
4. Fresh database dump + filestore delta sync from Hetzner.
5. Re-enable both timers here.
6. Retire Hetzner, then supersede `docs/backend-server.md` with this file.

## 9. OAuth redirect URIs — a per-host registration, not per-user

`OAUTH_REDIRECT_BASE_URL` in `.env` is already updated to `https://68-183-181-25.nip.io`.
Both self-serve connect flows build their callback from it at request time —
`ingestion/oauth_gmail.py:56` and `ingestion/oauth_zoho.py:50`:

```
https://68-183-181-25.nip.io/oauth/gmail/callback
https://68-183-181-25.nip.io/oauth/zoho/callback
```

Both must be registered in the provider consoles — Google Cloud **and** Zoho. The Zoho
one is easy to miss: the original migration notes listed only Gmail, but Zoho builds its
redirect from the same variable and needs the same treatment.

**This cannot be made on-demand, and that is a security property rather than a
limitation.** The app already generates the URI dynamically; the constraint is on the
provider side. Google and Zoho exact-match the redirect against a pre-registered
allowlist before redirecting — that check is what stops an attacker pointing your
`client_id` at a server they control and harvesting users' auth codes. So it is one
console entry per host, once, after which every future invite link works with no further
console work.

**What this does *not* block:** existing ingestion. All 7 rows in `oauth_accounts`
(4 Gmail, 3 Zoho) migrated `active` with valid refresh tokens, and sweeps show zero
`invalid_grant` / `redirect_uri_mismatch`. The redirect URI matters only when *connecting
or re-authorizing* an account.

## 10. The nip.io hostname — a real constraint to plan around

`68-183-181-25.nip.io` is a public wildcard-DNS convenience service: it simply resolves
the dashed IP in the name back to that IP. EOXS does not own it and cannot control it.
Two consequences:

- **The hostname is derived from the droplet IP.** If the IP ever changes, the TLS cert,
  the connector URLs every user has saved, and both OAuth console registrations all break
  together.
- Putting a real EOXS-owned domain in front (e.g. `wiki.eoxs.com`) would make the
  certificate, the connector URLs and the console entries survive any future IP change or
  migration. Worth doing before these URLs are handed to more users — the cost of the
  change grows with every connector configured against the current host.

## 11. Security posture

Matched to the Hetzner box, verified live:

- **Firewall (ufw)**: default deny incoming; only 22/tcp, 80/tcp, 443/tcp open (v4 and v6).
- **SSH**: key-only. `permitrootlogin no`, `passwordauthentication no`,
  `kbdinteractiveauthentication no`.
- **fail2ban**: active, `sshd` jail (11 bans to date — ordinary background noise).
- **Postgres**: listens on loopback only, not reachable from the internet.
- **`/dbadmin/` and `/dbadmin-staging/`**: nginx `auth_basic`, on top of pgweb's own
  `--readonly`/`--lock-session` and the `eoxs_readonly` role's SELECT-only grants.
- **`unattended-upgrades`**: enabled.
- **Improved vs Hetzner**: `deploy` is no longer in the `sudo` group (§1).
- **Still open**: the pgweb password is visible via `ps` (§2), and the MCP server still
  connects as `eoxs_app` rather than `eoxs_readonly` (§5).

Verified external responses: `/` → 404 (no root route, expected) · `/dbadmin/` → 401 ·
`/oauth/gmail/callback` → 400 (route live, missing params) · `/mcp/<secret>/sse` → 200.

## 12. Dependencies and code size

Python **3.12.3** on both boxes — which is why the virtualenvs moved across without a
rebuild. **82 packages** resolve inside `.venv`. See `docs/backend-server.md` §6 for the
direct-dependency table; `requirements.txt` is unchanged by the migration.

Current code size on this box (`wc -l`, excluding `__pycache__`):

| Directory | Lines | Files |
|---|---|---|
| `ingestion/` | 5,613 | 32 |
| `wiki_ingestion/` | 3,697 | 21 |
| `mcp_server/` | 1,908 | 7 |
| `loaders/` | 1,564 | 11 |
| `parsers/` | 365 | 9 |
| **Total (Python)** | **13,147** | **80** |
| `schema/` (SQL) | 1,078 | 33 migrations |

## 13. Data on disk

About 14 GB under `/home/deploy`, all on the single 77 GB root filesystem (29% used —
no separate volume, despite what a non-root `du` may suggest):

| Path | Size |
|---|---|
| `raj-wiki-vault/` | 13 GB (47,451 files) |
| `eoxs-wiki-db/` | 293 MB |
| `db_exports/` | 254 MB |
| `raj-wiki-vault-main/` | 218 MB |
| `eoxs-frontend-threads/` | 69 MB |
| `claude-notes-vault/` | 3.6 MB |

## 14. Known issues found during migration verification

| Issue | Status | Detail |
|---|---|---|
| `claude` CLI missing → pipeline dead | **binary fixed, login outstanding** | §7 |
| Both boxes writing in parallel | **mitigated** — timers disabled | §8 |
| Zoho OAuth redirect URI not registered | **outstanding** — needs console access | §9 |
| Zoho paging returns 404 past its search window | **fixed 2026-08-21** | see below |
| pgweb password visible in `ps` | open, carried over from Hetzner | §2 |
| MCP connects as `eoxs_app`, not `eoxs_readonly` | open, carried over from Hetzner | §5 |
| `deploy/nginx-https.conf` still holds the Hetzner IP | open — tracked file is stale for this host | §4 |

**The Zoho paging bug is pre-existing, not migration damage.** `ZohoClient.list_all_messages`
walks `start` upward in `PAGE_SIZE` steps, and Zoho answers an offset past the end of its
searchable window with **404 rather than an empty result set**. Since 404 is correctly
classified non-retryable by `_is_retryable`, that aborted the entire account's sweep —
observed on `isha_zoho` at `start=3401` after seven clean runs. The same code on Hetzner
hits the same wall whenever an account's offset crosses that boundary.

Fixed in `ingestion/zoho_fetcher.py` by treating a 404 **from that paging loop only** as
end-of-results; 404s from every other call site still surface as real errors. Verified
live: `isha_zoho` went from aborting to `written: 3, error: 0`, and `support_zoho`
regression-tested clean at `written: 2, error: 0`. **This fix should also be ported to
the Hetzner box**, since that is the one still serving users.

## 15. Where this fits

Same as on Hetzner: this is the one physical machine everything else lives on — the
database (`docs/postgres-database.md`), the raw-ingestion fetchers and their schedule
(`docs/raw-ingestion.md`), the wiki-synthesis pipeline (`docs/wiki-ingestion.md`), and the
Linear reporting (`docs/linear-integration.md`). It also hosts the sibling
`eoxs-frontend-threads` service and a Claude Code CLI environment used for admin and
development work directly on the box.

No separate worker fleet, no managed cloud database, no serverless functions. One droplet,
seven `eoxs-wiki-db` units plus one sibling-repo service, one Postgres instance.

---

*Verified against the live host on 2026-08-21. The Hetzner box remains the system of
record until the §8 checklist is complete.*
