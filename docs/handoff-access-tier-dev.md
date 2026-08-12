# Handoff Packet — Chunk-Level Access Tier Feature

> **SUPERSEDED.** Written for a specific one-off feature handoff before this repo was on
> GitHub — the §3 "no GitHub" premise is no longer true, and the general pattern here
> (staging-only Postgres role + restricted SSH login) has been generalized into
> `docs/local-dev-and-team-onboarding.md`, which is what a new developer should actually
> follow today. This file is kept for the feature-specific context, not as a setup guide.

*For: whoever is picking up the paragraph/section-level access-tier redesign. Prepared so they can get fully independent, working, staging-only access with zero prior context on this project.*

## The principle this is built on

He gets everything he needs to build and fully test the feature **against the staging database only** — not "please don't touch live," but **structurally cannot touch live**, enforced at the database ACL level, not by trust. He does not get: production server access, the live `.env` (Gmail/Zoho/Fireflies/Fathom/Odoo/Anthropic/Linear credentials — none of that is relevant to this feature), or any systemd/deploy access. When his work is done and reviewed, promoting it to live is a separate, deliberate step you (or whoever owns deploys) take — not something his own credentials are capable of doing by themselves.

## What he gets, and why each piece is scoped the way it is

| # | What | Scope |
|---|---|---|
| 1 | A new Postgres role | Read-write on `eoxs_wiki_staging` only. Cannot connect to `eoxs_wiki` (live) at all — see §1 below for why this needs a real ACL fix, not just a revoke. |
| 2 | A new SSH login on the VPS | Key-auth only, **not** in the `sudo` group, used only to open a tunnel to reach Postgres (which only listens on `127.0.0.1` — there's no other way in). No access to `.env`, no access to any other service. |
| 3 | The code repository | Full read access (he needs to understand `wiki_ingestion/`, `mcp_server/`, `schema/`, `docs/` to build this correctly) via his own clone, on his own branch. |
| 4 | The existing docs | `ARCHITECTURE.md`, `docs/postgres-database.md`, `docs/wiki-ingestion.md` especially — already written, hand them over directly, saves him days of reverse-engineering. |
| 5 | The open design questions | The unresolved ambiguities from the brainstorming session (granularity, provenance-vs-content classification, redaction UX, scope) — he should either get your answers to these first, or be told explicitly he owns deciding them. |

---

## §1 — The database role (needs a superuser, i.e. you or Ayan running this, not me)

**Why a simple `REVOKE` isn't enough**: right now, Postgres's built-in `PUBLIC` pseudo-role already has `CONNECT` on the live `eoxs_wiki` database (confirmed live — this predates this handoff, it's just how the database was set up). Postgres privileges are additive across every applicable grant — a role has a privilege if *any* grant path gives it to them (direct grant, or via `PUBLIC`, or via role membership). Revoking `CONNECT` from one specific new role does **not** remove access it still has through the `PUBLIC` grant. The only reliable fix is to revoke `CONNECT` from `PUBLIC` itself on the live database, then explicitly re-grant it back to the two roles that are actually supposed to use it.

Run this as a superuser (`sudo -u postgres psql`, or connect as the `postgres` role):

```sql
-- 1. Close the implicit "everyone can connect" hole on the LIVE database.
--    This is a real hardening fix, not just handoff scaffolding — worth
--    doing regardless. Confirmed today: PUBLIC currently has CONNECT on
--    eoxs_wiki; PUBLIC has zero TABLE-level grants anywhere, so this has
--    never allowed actual data access, but it should still be closed.
REVOKE CONNECT ON DATABASE eoxs_wiki FROM PUBLIC;

-- 2. Restore connect for the two roles that legitimately use live.
GRANT CONNECT ON DATABASE eoxs_wiki TO eoxs_app;
GRANT CONNECT ON DATABASE eoxs_wiki TO eoxs_readonly;

-- 3. Create the new developer's role. Replace the password with a real
--    generated one (e.g. `openssl rand -base64 24`) -- don't ship this
--    placeholder.
CREATE ROLE eoxs_staging_dev LOGIN PASSWORD 'REPLACE_ME_WITH_A_REAL_PASSWORD';

-- 4. Scope it to staging only.
GRANT CONNECT ON DATABASE eoxs_wiki_staging TO eoxs_staging_dev;

-- 5. Give it real working privileges ON STAGING (create tables, alter
--    schema, insert/update/delete -- he needs full DDL+DML there since
--    he'll be prototyping a new table + migration).
\c eoxs_wiki_staging
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO eoxs_staging_dev;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA wiki_staging TO eoxs_staging_dev;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO eoxs_staging_dev;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA wiki_staging TO eoxs_staging_dev;
GRANT CREATE ON SCHEMA public TO eoxs_staging_dev;
GRANT CREATE ON SCHEMA wiki_staging TO eoxs_staging_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE eoxs_staging_dev IN SCHEMA public GRANT ALL ON TABLES TO eoxs_staging_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE eoxs_staging_dev IN SCHEMA wiki_staging GRANT ALL ON TABLES TO eoxs_staging_dev;
```

**Verify it actually worked** (run as the new role, from anywhere with network access to the tunnel):
```
psql "host=127.0.0.1 port=<tunnel-port> dbname=eoxs_wiki dbname=eoxs_wiki_staging user=eoxs_staging_dev"
```
The `eoxs_wiki` (live) connection attempt should fail with a permission error. The `eoxs_wiki_staging` one should succeed.

## §2 — The SSH login (needs sudo, i.e. you or Ayan running this)

Postgres only listens on `127.0.0.1` on the VPS — there is no direct network path to it from outside. The only way in is an SSH tunnel, so he needs an SSH account, but it should not be `deploy` (that account has sudo and touches every production service).

```bash
sudo useradd -m -s /bin/bash wikidev
sudo mkdir -p /home/wikidev/.ssh
# Have him generate a keypair on HIS OWN machine (ssh-keygen -t ed25519) and
# send you the PUBLIC key (id_ed25519.pub) -- never his private key.
sudo bash -c 'echo "<HIS_PUBLIC_KEY_HERE>" > /home/wikidev/.ssh/authorized_keys'
sudo chown -R wikidev:wikidev /home/wikidev/.ssh
sudo chmod 700 /home/wikidev/.ssh
sudo chmod 600 /home/wikidev/.ssh/authorized_keys
sudo passwd -l wikidev   # disable password login entirely, key-only
```
Confirm he is **not** added to the `sudo` group (default `useradd` behavior already excludes it — just don't run `usermod -aG sudo wikidev`).

## §3 — The repository

You don't currently have this repo on GitHub (it's local-only on the VPS). Two options — pick one:

**Option A (recommended): a bare repo on the VPS**, reachable over the same SSH login he already has for the DB tunnel — no new infrastructure.
```bash
sudo -u wikidev git init --bare /home/wikidev/eoxs-wiki-db.git
cd /home/deploy/eoxs-wiki-db
git remote add wikidev-handoff wikidev@<VPS_IP>:/home/wikidev/eoxs-wiki-db.git
git push wikidev-handoff main
```
He then clones with `git clone wikidev@<VPS_IP>:/home/wikidev/eoxs-wiki-db.git`. When he's ready to share progress, he pushes to a branch on that same remote and you pull it down on your side to review.

**Option B: a private GitHub repo.** Nicer PR/review workflow, but is a new piece of infrastructure and means the code leaves the VPS entirely. Only do this if you already want this project on GitHub anyway, independent of this handoff.

Either way — **do not** give him access to the live-deploying checkout at `/home/deploy/eoxs-wiki-db` itself, or to the `deploy` account. He works from his own clone, on his own machine, entirely.

## §4 — What to physically send him

1. VPS IP, his SSH username (`wikidev`), confirmation his public key is installed.
2. The Postgres credentials: `eoxs_staging_dev` / the real password you generated.
3. The repo URL from §3.
4. This file, plus `ARCHITECTURE.md`, `docs/postgres-database.md`, `docs/wiki-ingestion.md` (already written — just hand them over).
5. Your answers to the open design questions from the brainstorm (granularity, provenance-vs-content classification, how a redacted chunk should render, whether links/citations need their own tier, scope) — or explicit sign-off that he owns deciding them himself.
6. **No `.env` file, no server credentials, no other API keys.** If his implementation ends up needing an LLM call to classify chunk sensitivity, he uses his **own** Anthropic API key during development (his own account, at his own/your discretion on cost) — not the production `CLASSIFIER_ANTHROPIC_API_KEY`.

## §5 — His local machine setup, from zero

```bash
# 1. Prerequisites
#    - Python 3.12 (match the server's version)
#    - git
#    - psql or a GUI client (pgAdmin/DBeaver) if he wants one, optional

# 2. Clone the repo (Option A path)
git clone wikidev@<VPS_IP>:/home/wikidev/eoxs-wiki-db.git
cd eoxs-wiki-db

# 3. Python environment
python3 -m venv .venv
source .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -r requirements.txt

# 4. Open the SSH tunnel to reach Postgres (run this in its own terminal,
#    leave it running while he works)
ssh -L 5433:127.0.0.1:5432 wikidev@<VPS_IP>
#    (5433 chosen to avoid clashing with a local Postgres if he has one;
#    any free local port works)

# 5. His own local .env (create this file himself -- do NOT copy the real one)
cat > .env << 'EOF'
PGHOST=127.0.0.1
PGPORT=5433
PGDATABASE=eoxs_wiki_staging
PGUSER=eoxs_staging_dev
PGPASSWORD=<the password from §1>
EOF

# 6. Sanity check
python3 -c "
from ingestion.db import get_staging_conn
conn = get_staging_conn()
print('connected OK:', conn)
"
```

He now has a fully working local environment against a complete, realistic copy of the real data, with zero path to touching live.

## §6 — How this eventually reaches live

Not his decision to make unilaterally, and not something to build into his own access. Once his schema change and code are working and reviewed against staging:
1. His schema change becomes a new numbered migration file (`schema/023_...sql` or whatever's next), reviewed the same way every other migration in this repo has been.
2. The migration gets applied to live `eoxs_wiki` by whoever runs deploys today (you/Ayan), the same way every prior migration was.
3. His application code gets merged into whatever the production checkout actually runs, and deployed the normal way (not something his `wikidev` account or `eoxs_staging_dev` role can do themselves — no write access to the live database or the production server exists in his credentials at all).
