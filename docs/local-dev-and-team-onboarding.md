# Local Dev & Team Onboarding

*How to develop against this project on a laptop instead of editing directly
on the production server, and how to repeat that setup for other teammates
later. Supersedes the one-off shape of `docs/handoff-access-tier-dev.md`
(which predates this repo existing on GitHub) with a general, repeatable
process.*

## The model

1. **GitHub is the single source of truth.** `github.com/eoxssecondbrain/eoxs-wiki-db`,
   `main` branch. Nobody edits the production checkout directly anymore —
   changes land as commits on `main` (reviewed, per §5) and the server pulls
   them.
2. **Local dev works against `eoxs_wiki_staging`, never live `eoxs_wiki`.**
   Enforced at the Postgres ACL level (a role that structurally cannot
   `CONNECT` to the live database), not by trust or convention.
3. **Deploying is a deliberate, separate, admin-only step.** Nobody's local
   credentials can push a change to production by themselves — merging a PR
   and deploying it are two different actions taken by two different people
   (or the same person, deliberately, twice).
4. **Nobody's laptop holds a production secret it doesn't need.** Scope every
   credential to the task. See the access matrix below.

## Access matrix — what each kind of task actually needs

| Task | Repo | Staging DB | Live DB | Server SSH | Other secrets |
|---|---|---|---|---|---|
| Ingestion / fetcher work | Write (branch + PR) | Read-write | None | None | Own sandbox API key for the one source they're touching (Gmail/Zoho/Fireflies/etc.) — never the production credential |
| Auditing / data QA | Read | Read-only | None | None | Own Anthropic key, only if testing the redaction classifier locally |
| Linear board review | None needed, or Read | None | None | None | Their own Linear login invited onto the "EDB" team — **not** a shared `LINEAR_EDB_API_KEY` |
| Server health checks | Read | None | None | Restricted (status/logs only, see §6) | None |
| Admin (you) | Write + merge | Read-write | Read-write | Full | Everything |

Nobody except admin gets `.env`, the live `PGPASSWORD`, any `MCP_*_URL_SECRET`,
or unrestricted server SSH. `.env.example` in the repo root documents every
variable that exists — copy it to `.env` locally and fill in only the rows
your task needs.

---

## Part 1 — Your own local setup (Windows)

You're on Windows — use **WSL2 with Ubuntu**, not native Windows Python. This
project is bash/Linux-native top to bottom (the server itself is Ubuntu
24.04); matching that locally avoids a long tail of path- and line-ending
bugs, and it's the same environment every future teammate will use.

### 1. Install WSL2 + Ubuntu

In an **administrator** PowerShell window:
```powershell
wsl --install -d Ubuntu
```
Reboot if prompted. Launch "Ubuntu" from the Start menu once — it'll ask you
to create a Linux username/password (local to WSL, unrelated to anything on
the server). Everything from here on runs **inside that Ubuntu window**.

### 2. Base packages

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip git postgresql-client
```

### 3. GitHub access

If you don't already have a way to push to GitHub from this machine, the
simplest is a Personal Access Token:
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
token → scope it to the `eoxs-wiki-db` repo, Contents: Read and write.
You'll be prompted for a username + this token as the password the first time
you push.

```bash
git clone https://github.com/eoxssecondbrain/eoxs-wiki-db.git
cd eoxs-wiki-db
```

### 4. Your personal staging Postgres role (run on the **server**, not your laptop)

This needs a Postgres superuser, so run it yourself on the server (you have
sudo there):
```bash
sudo -u postgres psql
```
```sql
-- Close the "everyone can connect" hole on LIVE if it's still open, and make
-- sure the two roles that legitimately use live still can (safe to re-run).
REVOKE CONNECT ON DATABASE eoxs_wiki FROM PUBLIC;
GRANT CONNECT ON DATABASE eoxs_wiki TO eoxs_app;
GRANT CONNECT ON DATABASE eoxs_wiki TO eoxs_readonly;

-- Your personal dev role -- generate a real password, don't ship this one.
-- (openssl rand -base64 24)
CREATE ROLE local_dev LOGIN PASSWORD 'REPLACE_ME';
GRANT CONNECT ON DATABASE eoxs_wiki_staging TO local_dev;

\c eoxs_wiki_staging
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO local_dev;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA wiki_staging TO local_dev;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO local_dev;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA wiki_staging TO local_dev;
GRANT CREATE ON SCHEMA public TO local_dev;
GRANT CREATE ON SCHEMA wiki_staging TO local_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE local_dev IN SCHEMA public GRANT ALL ON TABLES TO local_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE local_dev IN SCHEMA wiki_staging GRANT ALL ON TABLES TO local_dev;
```
`local_dev` cannot connect to `eoxs_wiki` (live) at all — confirmed: Postgres
here only listens on `127.0.0.1`, so there's no network path to it except
through an SSH tunnel anyway (§5), and the role itself has no `CONNECT` grant
on that database even if it did.

### 5. The SSH tunnel

Postgres only binds to localhost on the server, by design — the only way to
reach it from your laptop is through an SSH tunnel using your existing
`deploy` SSH access. Run this in its own WSL terminal window and leave it
running while you work:
```bash
ssh -L 5433:127.0.0.1:5432 deploy@5.223.44.95 -N        # Hetzner (still live)
# DigitalOcean box instead:
# ssh -L 5433:127.0.0.1:5432 deploy@68.183.181.25 -N
```
(`5433` avoids clashing with a local Postgres if you ever install one; `-N`
means "just forward, don't open a shell".)

### 6. Your local `.env`

```bash
cp .env.example .env
```
Edit it:
```
PGHOST=127.0.0.1
PGPORT=5433
PGDATABASE=eoxs_wiki_staging
PGUSER=local_dev
PGPASSWORD=<the password from step 4>
```
Leave everything else blank until you're actually working on something that
needs it (see the access matrix above).

### 7. Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 8. Verify

```bash
python3 -c "
from ingestion.db import get_staging_conn
conn = get_staging_conn()
cur = conn.cursor()
cur.execute('SELECT count(*) FROM call_transcripts')
print('staging connection OK:', cur.fetchone())
"
```
You now have a full local checkout, talking to a live-updating copy of the
real data (staging gets every ingestion cycle the same as live does), with no
path to touching production from your laptop at all.

### Troubleshooting — real mistakes hit doing this setup, in order

Every one of these actually happened working through this guide. Saving the
next person the same back-and-forth.

- **"Which terminal am I actually in?"** — the single most common mistake
  here, by far. Windows PowerShell, WSL's Ubuntu app, and an SSH session on
  the server are three genuinely different environments that can look
  similar at a glance. Before running anything, check the prompt: it should
  show your laptop's own hostname (e.g. `...@LAPTOP-XXXX`) for local WSL
  work, or the server's hostname (`...@ubuntu-4gb-sin-1` on Hetzner,
  `...@Live-droplet` on DigitalOcean) if you're
  intentionally inside an SSH session. Commands meant for one, run in the
  other, fail in confusing ways (see the tunnel note below) rather than with
  an obvious "wrong place" error.
- **`sudo apt install python3.12` fails with "Unable to locate package"** —
  WSL's default Ubuntu (22.04 "jammy") doesn't carry Python 3.12 in its base
  repos. Fix: add the deadsnakes PPA first —
  `sudo apt install -y software-properties-common && sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update`
  — then retry the full install line from step 2. If any package name in a
  multi-package `apt install` can't be found, apt aborts the whole command
  without installing *any* of them — re-run the complete line, not just the
  failed package.
- **`git clone`/`git push` prompts for credentials, or fails outright** —
  this repo is private. Generate a GitHub personal access token scoped to
  it and use that as the password when prompted (username = your GitHub
  username). **Never paste a real token into a chat with an AI assistant or
  anywhere else it'll be logged** — if one ever does end up somewhere it
  shouldn't, rotate/revoke it immediately afterward, even if it worked fine.
- **`pip install -r requirements.txt` fails with "No such file or
  directory"**, or `nano .env` opens a suspiciously empty "[New File]"** —
  you're not inside the cloned repo directory. `cd` into it first
  (`ls` should show `requirements.txt`, `mcp_server/`, `ingestion/`, etc.
  before you proceed) — this is the same "which terminal/directory am I in"
  class of mistake as the first point above, just one level more specific.
- **The verify step fails with `Connection refused` on port 5433** — the SSH
  tunnel (step 5) isn't currently open. It has to run in its own dedicated
  terminal window, stays silent with no prompt while working correctly, and
  does **not** persist once that window is closed — reopen it
  (`ssh -L 5433:127.0.0.1:5432 deploy@<host> -N` — Hetzner `5.223.44.95` or
  DigitalOcean `68.183.181.25`; from your laptop, not
  from inside a server session) any time you come back to this after a break.
- **Pasting the multi-line `GRANT`/`ALTER` block right after `\c
  eoxs_wiki_staging` produces `invalid integer value "..." for connection
  option "port"`** — psql's `\c` command consumes everything up to the next
  newline as connection arguments, and a fast multi-line paste can deliver
  the next SQL line before `\c` finishes parsing, so it tries to parse
  `GRANT ALL PRIVILEGES...` as a username/host/port. Run `\c
  eoxs_wiki_staging` **by itself**, confirm the prompt actually changes to
  `eoxs_wiki_staging=#`, *then* paste the rest as a separate block.
- **A private access token/password only works for read, not push (or vice
  versa), even though it worked a minute ago for something else** —
  fine-grained GitHub tokens are scoped to an explicit list of repositories.
  A token that already works fine against `eoxs-wiki-db` won't automatically
  work against a brand-new repo until that repo is added to the token's own
  "Repository access" list in GitHub settings.

---

## Part 2 — Day-to-day workflow

```bash
git checkout -b your-name/short-description
# make changes, test locally against staging
git add <files>
git commit -m "..."
git push -u origin your-name/short-description
```
Open a PR on GitHub against `main`. Someone reviews (you can point Claude
Code's `code-reviewer` agent at the diff for a first pass) and merges.

**Deploying is separate and admin-only:**
```bash
# on the server
cd /home/deploy/eoxs-wiki-db
git pull
```
- `wiki_ingestion`/raw-`ingestion` scripts run as `Type=oneshot` systemd
  timers — a fresh process spawns per run, so a `git pull` alone is enough;
  the next scheduled run picks up the new code automatically.
- `mcp_server.http_server` (the live MCP connectors) is a **persistent**
  process — it needs an explicit restart to pick up new code:
  `sudo systemctl restart eoxs-mcp.service`. Restarting it drops any
  in-memory-only state, so double check nothing important only lives there
  before restarting (nothing currently does, as of this doc).

---

## Part 3 — Onboarding another teammate later

Repeat Part 1 for them, with two changes:

1. **Their own Postgres role**, same shape as §4 but their own name
   (`GRANT ... TO <their_name>_dev`) — never share `local_dev`'s password.
   Per-person roles mean you can revoke one person without affecting anyone
   else, and every write to staging is attributable.
2. **Their own SSH login**, scoped down from `deploy`. Don't hand out the
   `deploy` account itself — it holds `.env`, can restart production
   services, and has full sudo. Instead:
   ```bash
   sudo useradd -m -s /bin/bash <their_name>
   sudo mkdir -p /home/<their_name>/.ssh
   # they generate their own keypair locally and send you the PUBLIC half
   sudo bash -c 'echo "<their public key>" > /home/<their_name>/.ssh/authorized_keys'
   sudo chown -R <their_name>:<their_name> /home/<their_name>/.ssh
   sudo chmod 700 /home/<their_name>/.ssh
   sudo chmod 600 /home/<their_name>/.ssh/authorized_keys
   sudo passwd -l <their_name>   # key-only, no password login
   ```
   Confirm they're **not** added to the `sudo` group (default `useradd`
   behavior already excludes it). This account is good for nothing but
   opening a tunnel to Postgres — no `.env`, no other service access.
3. **GitHub**: invite them as a collaborator on the repo with **Write**
   access (can push branches and open PRs) — then turn on branch protection
   on `main` (Settings → Branches → require a PR + at least one review before
   merge) so nobody, including you, can push straight to `main` by accident.
   If a teammate's role is closer to "reviewing Linear boards" than writing
   code, they likely don't need repo access at all — see §6.

### For the "server health check" persona specifically

They need to read service status and logs without full sudo. A narrowly
scoped sudoers rule does this without opening anything else up:
```bash
echo '<their_name> ALL=(root) NOPASSWD: /usr/bin/systemctl status eoxs-mcp.service, /usr/bin/systemctl status eoxs-wiki-pipeline.service, /usr/bin/systemctl status eoxs-sweep.service, /usr/bin/journalctl -u eoxs-mcp.service *' | sudo tee /etc/sudoers.d/<their_name>-healthcheck
sudo chmod 440 /etc/sudoers.d/<their_name>-healthcheck
```
Adjust the unit list to whatever they actually need to check. This grants
exactly those read-only commands, nothing else — they still can't restart a
service, read `.env`, or touch the database.

### For the "Linear board review" persona specifically

Don't hand out `LINEAR_EDB_API_KEY` — invite them as a real user onto the
Linear "EDB" team instead (Linear's own UI, Settings → Members). That gives
per-person audit trail and normal Linear permission controls, which a shared
service API key can't.

---

## What's still open

- **MCP connector IP whitelisting** for restricting who can attach a
  connector URL in claude.ai — planned but not built; see the design
  discussion in conversation, not yet written up as a doc. The short version:
  IP allowlisting doesn't restrict claude.ai's own connector traffic (it
  originates from Anthropic's infrastructure, not the end user's IP) — the
  real lever there is per-person URL secrets with revocation, which this
  same access-matrix pattern extends naturally to.
