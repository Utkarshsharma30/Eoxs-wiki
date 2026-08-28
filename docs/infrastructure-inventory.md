# AskCruz — Tech Stack, Services, Billing & Dependencies

*A single place to see everything AskCruz runs on, what it costs, and what it depends on.
Infrastructure figures below were pulled live from the DigitalOcean API on 2026-08-28;
external-service costs are marked as unverified because they are not visible from here.*

> **How to refresh the DigitalOcean half of this document:**
> ```bash
> doctl compute droplet list --format Name,PublicIPv4,Memory,VCPUs,Disk,Region,Status
> doctl databases list --format Name,Engine,Version,Size,Region,Status
> doctl apps list --format Spec.Name,DefaultIngress
> doctl balance get
> doctl invoice list                       # then: doctl invoice get <uuid>
> ```

---

## 1. Cost summary — DigitalOcean

**Month-to-date: $43.97** (invoice preview $42.21 after a $5.00 credit; +13% HST Ontario).

| Resource | Type | Spec | MTD | Notes |
|---|---|---|---|---|
| `eoxs-staging-app` | Droplet | 2 vCPU / 4 GB / 80 GB | **$11.61** | staging |
| `eoxs-staging-agent` | Droplet | 2 vCPU / 4 GB / 80 GB | **$11.61** | staging |
| `Live-droplet` | Droplet | 2 vCPU / 4 GB / 80 GB | **$6.47** | **production** |
| `staging-cruz` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$7.33** | staging DB |
| `eoxs-db-live-cluster` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$1.91** | **production DB** (created 2026-08-25, partial month) |
| `cruz-3gm-outlook-db` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$0.68** | 3GM client |
| `eoxs-services-sgp1` | App Platform | 3 components | **$2.28** | mcp + ingestion + sweep job |
| `askcruz-3gm` | Droplet | 1 vCPU / 512 MB / 10 GB | **$0.18** | 3GM client |
| 2× droplet snapshots | Snapshot | 4.02 + 4.41 GiB | **$0.25** | dated 14 Aug |
| Taxes (HST 13%) | — | — | **$4.86** | |
| Inference Cloud Trial credit | — | — | **−$5.00** | one-off |

**Everything is in SGP1 (Singapore).**

### Cost observations

- **Staging costs 2.6× production.** The two staging droplets alone ($23.22) plus
  `staging-cruz` ($7.33) come to **$30.55/mo — about 70% of the entire bill** — versus
  $8.38 for production droplet + database. Both staging droplets are 2 vCPU / 4 GB,
  the same size as production.
- **`eoxs-db-live-cluster`'s $1.91 is not representative.** It was created 2026-08-25, so
  it has only ~85 hours of runtime. At a full month it is roughly **$16–17**, which is
  what next month's bill will show.
- **Two snapshots from 14 Aug are still billing.** Small ($0.25/mo) but worth deleting if
  the staging boxes have moved on.
- **`askcruz-3gm` is a 512 MB droplet** — the smallest tier. Worth confirming what it runs
  and whether 512 MB is sufficient.

---

## 2. Infrastructure

### Production

| Component | Where | Detail |
|---|---|---|
| **Droplet** | `Live-droplet` · `68.183.181.25` | Ubuntu 24.04.4, 2 vCPU / 4 GB / 80 GB, SGP1 |
| **Database** | `eoxs-db-live-cluster` | Managed PG 16.15, private VPC, `sslmode=require` |
| **App Platform** | `eoxs-services-sgp1` | `eoxs-mcp`, `eoxs-ingestion`, `eoxs-sweep` (cron job) |
| **Domain** | `mcp.askcruz.com` | → `68.183.181.25`, Let's Encrypt cert to 2026-11-22 |

Databases inside the cluster: `eoxs_wiki` (~526 MB), `eoxs_wiki_staging` (~337 MB),
`eoxs_frontend_threads` (~9 MB).

### Services on `Live-droplet`

| Unit | Port | Purpose |
|---|---|---|
| `eoxs-ingestion` | 8090 | webhook receiver + `/health` |
| `eoxs-mcp` | 8091 | customer-facing MCP connectors (SSE) |
| `pgweb` | 8092 | read-only DB browser (`/dbadmin/`) |
| `eoxs-wiki-mcp` | 8093 | internal MCP for pipeline sub-agents |
| `eoxs-frontend-threads` | 8094 | chat-transcript MCP (separate repo) |
| `pgweb-staging` | 8095 | staging DB browser |
| `nginx` | 80/443 | reverse proxy + TLS |
| `eoxs-wiki-pipeline.timer` | — | wiki synthesis, every 6h |
| `eoxs-healthcheck.timer` | — | hourly job health check |

`eoxs-sweep.timer` on the droplet is **disabled** — App Platform owns the sweep now.

### Staging

`eoxs-staging-app` (`68.183.234.165`) · `eoxs-staging-agent` (`157.230.42.193`) ·
`staging-cruz` database. Both droplets 2 vCPU / 4 GB.

### Client-specific (3GM)

`askcruz-3gm` droplet (`168.144.247.176`, 512 MB) and `cruz-3gm-outlook-db`. 3GM is the
first acquired customer — this looks like a per-client deployment pattern worth
understanding before it repeats for the next customer.

### Legacy — not on DigitalOcean

**Hetzner `5.223.44.95`** is still live and still serving users. **Billed separately by
Hetzner and not included in the $43.97 above.** Roughly $6–10/mo historically. Retiring
it is the last step of the migration.

---

## 3. External dependencies

Every one of these is a service AskCruz stops working without. Costs are **not visible
from the infrastructure** and need filling in from each provider's console.

### Critical — an outage here breaks user-facing features

| Service | Used for | Cost | Owner |
|---|---|---|---|
| **Anthropic API** | Redaction safety net, tier classification, spam filter, call relevance | **TBD** — ~$600 observed on one key over ~2.5 weeks | TBD |
| **Claude Code CLI** | Wiki pipeline's `claude -p` sub-agents | subscription | TBD |
| **GoDaddy** | `askcruz.com` DNS + registration | TBD | TBD |
| **Vercel** | `askcruz.com` frontend hosting | TBD | Jaskeerat |

> **Both Anthropic keys (`ANTHROPIC_API_KEY`, `CLASSIFIER_ANTHROPIC_API_KEY`) hit an
> account spend cap on 2026-08-26, resetting 2026-09-01.** While capped, four of five MCP
> identities return "temporarily unavailable" (redaction fails closed), and three
> classifiers silently degrade. Separately, Claude Code subscription access was disabled
> org-wide, which stops the wiki pipeline — a *different* failure needing a *different*
> fix. See §5.

### Data sources — ingestion degrades without them

| Service | Used for | Accounts |
|---|---|---|
| **Google / Gmail API** | Email ingestion + OAuth | 4 (`raj`, `ron`, `remya`, `isha`) |
| **Zoho Mail API** | Support inbox + OAuth | 3 (`support`, `isha`, `ayan`) |
| **Fireflies** | Call transcripts | 1 API key |
| **Fathom** | Call recordings | 1 API key (`ron`) |
| **Odoo** (×7 tenants) | Client implementation boards | Greer, ESS, DPS, PPC, 3GM, Sabre, EOXS-tickets |
| **Linear** | Pipeline status reporting | `LINEAR_EDB_*` |

⚠️ `LINEAR_API_KEY` / `LINEAR_TEAM_KEY` exist in `.env` but are **dead** — the code reads
only the `_EDB_` variants. Do not carry them into new environments.

---

## 4. Dependency chain — what breaks what

```
Anthropic API ──┬─► redaction ──► hr/general/intern/ayan connectors
                ├─► tier classifier ──► new rows default to tier1 (invisible to non-full)
                ├─► spam filter ──► noise gets ingested
                └─► call relevance ──► irrelevant calls ingested

Claude Code CLI ──► wiki pipeline ──► no new wiki pages

Managed Postgres ──► everything (MCP, ingestion, sweep, pipeline, pgweb)

Gmail/Zoho/Fireflies/Fathom/Odoo ──► raw ingestion (per-source, isolated failures)

GoDaddy DNS ──► mcp.askcruz.com ──► every connector URL + OAuth callbacks

Vercel ──► askcruz.com frontend (independent of the backend)
```

**Single points of failure worth knowing:** the Anthropic account (one spend cap disables
four of five connectors), the managed database (nothing works without it), and GoDaddy DNS
(one record change breaks every connector URL and both OAuth callbacks).

---

## 5. Gaps to close

- **Anthropic spend is untracked.** No token or dollar cost is logged anywhere in the
  codebase — `wiki_ingest_cycles`/`wiki_ingest_batches` store row counts and status but no
  cost fields, even though Claude Code's CLI emits `total_cost_usd` in its result event and
  `headless_agent.py` currently discards it. Wiring that field into the batch table would
  give real per-cycle cost instead of estimates.
- **No alerting on API-key exhaustion.** The 2026-08-26 outage surfaced as a user
  complaint, not an alert. The hourly health check does not cover it.
- **External-service costs unknown.** Anthropic, Vercel, GoDaddy, Fireflies, Fathom and
  Linear all need their billing owner and monthly cost recorded here.
- **Staging is 70% of the DigitalOcean bill** and sized identically to production.
- **Two 14 Aug snapshots** still billing.
- **Hetzner still running** — double infrastructure until cut-over.

---

*Refresh the DigitalOcean figures with the commands at the top of this file. The external
services need a human with console access to each provider.*
