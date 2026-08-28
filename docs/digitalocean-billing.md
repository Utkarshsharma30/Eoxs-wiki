# DigitalOcean — Resources & Billing

*Every DigitalOcean resource AskCruz runs on and what each one costs. Figures pulled live
from the DigitalOcean billing API on 2026-08-28.*

> **Scope:** DigitalOcean only. Every other vendor — Anthropic, Hetzner, GoDaddy, Vercel,
> Fireflies, Fathom, Linear, Odoo — lives in `docs/tech-stack-and-billing.md`, which is the
> cross-vendor inventory. This file exists because DigitalOcean is the one vendor whose
> numbers can be read programmatically, so it can be kept exact while the rest stay
> placeholders until someone with console access fills them in.
>
> **Refresh it with:**
> ```bash
> doctl balance get
> doctl invoice list                    # then: doctl invoice get <uuid>
> doctl compute droplet list --format Name,PublicIPv4,Memory,VCPUs,Disk,Region,Status
> doctl databases list  --format Name,Engine,Version,Size,Region,Status
> doctl apps list       --format Spec.Name,DefaultIngress
> doctl compute snapshot list
> ```

---

## 1. Month-to-date: $43.97

Invoice preview **$42.21** after a $5.00 Inference Cloud Trial credit, plus 13% HST
(Ontario). Everything is in **SGP1 (Singapore)**.

| Resource | Type | Spec | MTD | Purpose |
|---|---|---|---|---|
| `eoxs-staging-app` | Droplet | 2 vCPU / 4 GB / 80 GB | **$11.61** | staging |
| `eoxs-staging-agent` | Droplet | 2 vCPU / 4 GB / 80 GB | **$11.61** | staging |
| `staging-cruz` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$7.33** | staging database |
| `Live-droplet` | Droplet | 2 vCPU / 4 GB / 80 GB | **$6.47** | **production** |
| `eoxs-services-sgp1` | App Platform | 3 components | **$2.28** | `eoxs-mcp`, `eoxs-ingestion`, `eoxs-sweep` |
| `eoxs-db-live-cluster` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$1.91** | **production database** — partial month, see §3 |
| `cruz-3gm-outlook-db` | Managed PG 16 | 1 GB / 1 vCPU / 10 GB | **$0.68** | 3GM client |
| `askcruz-3gm` | Droplet | 1 vCPU / 512 MB / 10 GB | **$0.18** | 3GM client |
| `eoxs-staging-app-snapshot-14aug` | Snapshot | 4.41 GiB | **$0.13** | |
| `eoxs-staging-agent-snapshot-14aug` | Snapshot | 4.02 GiB | **$0.12** | |
| HST Ontario (13%) | Tax | — | **$4.86** | |
| Inference Cloud Trial | Credit | — | **−$5.00** | one-off |

App Platform breakdown: `eoxs-mcp` $1.09, `eoxs-ingestion` $1.09, `eoxs-sweep` $0.10
(30 job runs).

---

## 2. What each resource actually is

### Production

| Resource | Detail |
|---|---|
| `Live-droplet` · `68.183.181.25` | Ubuntu 24.04.4. Runs nginx, the MCP server, ingestion, both pgweb instances, the frontend-threads service, and the wiki pipeline. Serves `mcp.askcruz.com`. |
| `eoxs-db-live-cluster` | Managed PG 16.15. Holds `eoxs_wiki`, `eoxs_wiki_staging`, `eoxs_frontend_threads`. Private VPC, `sslmode=require`. |
| `eoxs-services-sgp1` | App Platform. `eoxs-mcp` + `eoxs-ingestion` as services, `eoxs-sweep` as a 2-hourly scheduled job. |

### Staging

`eoxs-staging-app` (`68.183.234.165`), `eoxs-staging-agent` (`157.230.42.193`), and the
`staging-cruz` database. Both droplets are 2 vCPU / 4 GB — **the same size as production**.

### Client-specific (3GM)

`askcruz-3gm` (`168.144.247.176`, 512 MB) and `cruz-3gm-outlook-db`. 3GM is the first
acquired customer, so this looks like a per-client deployment pattern. Worth understanding
what it contains and whether it is the intended template before customer two arrives —
neither resource is described anywhere else in this repo.

---

## 3. Cost observations

**Staging is ~70% of the bill.** The two staging droplets ($23.22) plus `staging-cruz`
($7.33) total **$30.55**, against **$8.38** for the production droplet and its database.
Staging is sized identically to production; whether it needs to be is a real question.

**The bill will rise on its own.** `eoxs-db-live-cluster` shows $1.91 because it was created
2026-08-25 — roughly 85 hours of runtime this cycle. A full month at the same tier is about
**$16–17**, which puts September around **$58–60** with no other changes.

**Two snapshots from 14 Aug are still billing** — $0.25/mo combined. Small, but worth
deleting if the staging boxes have moved on from that state.

**Hetzner is not in this total.** The original box (`5.223.44.95`) is still live, still
serving users, and billed separately by Hetzner. Infrastructure is effectively doubled
until cut-over completes.

---

## 4. Notes

- Every figure above is month-to-date for **2026-08**, not a monthly rate. Droplet and
  database costs are hourly, so a resource created mid-month shows less than its full
  monthly price.
- The `Duration` column in `doctl invoice get` is hours of billed runtime — useful for
  spotting exactly this partial-month effect.
- Snapshots bill by stored size and are easy to forget; check
  `doctl compute snapshot list` when reviewing cost.
- Account: `eoxscruz@gmail.com` (team `My Team`).
