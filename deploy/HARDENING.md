# Server hardening — applied live, not by any script

Everything in this file was hand-applied directly on the Hetzner box (`5.223.44.95`),
not via a commit or a setup script — `deploy/setup.sh` predates all of it. Captured
here 2026-08-20 specifically so a DigitalOcean migration doesn't silently drop it;
see the migration security checklist in `docs/infrastructure-roadmap.md`.

## nginx (`deploy/nginx.conf`, `deploy/nginx-https.conf`)

Both tracked files are now kept byte-identical to the live
`/etc/nginx/nginx.conf` and `/etc/nginx/sites-available/eoxs-ingestion` — diff
against live periodically, since nothing enforces that automatically.

- `server_tokens off` — hides the nginx version string from every response.
- `ssl_protocols TLSv1.2 TLSv1.3` — TLS 1.0/1.1 dropped (apex, 2026-08-15).
- Security headers, http-level (apex, 2026-08-15): `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: no-referrer`,
  `Strict-Transport-Security: max-age=63072000`.
- Custom `masked` log format — redacts the MCP secret path segment
  (`/mcp/<secret>/...` → `/mcp/[REDACTED]/...`) before it ever reaches
  `access.log`, via an nginx `map` on `$uri`. Applies to every request logged
  by this server, not just MCP traffic that happens to match.

## fail2ban (`deploy/fail2ban-jail.local`)

Deployed as `/etc/fail2ban/jail.local` on the live box. `sshd` jail enabled,
`bantime=1h findtime=10m maxretry=5`, `nftables` backend (via
`/etc/fail2ban/jail.d/defaults-debian.conf`, Debian's own package default —
not hand-edited). `ignoreip` currently only has localhost + one IP
(`161.97.98.156`) — **TODO in the live config itself**: append EOXS office
IPs once Ayan provides them, so legitimate admin access doesn't risk a
lockout under retry pressure.

## sshd (`/etc/ssh/sshd_config` — not tracked, see below)

Confirmed live, 2026-08-20:
```
PermitRootLogin no
PasswordAuthentication no
```
Key-only auth, no root login. **Not captured as a tracked file** — `sshd_config`
on this box is the stock Ubuntu file with these two lines hand-edited in,
not worth tracking wholesale; just replicate these two directives on the new
Droplet and confirm with `sshd -T | grep -E "permitrootlogin|passwordauthentication"`.

## Access control notes for whoever provisions the new Droplet

- **`sudo` group membership, confirmed live 2026-08-20**: `deploy`, `ayan`,
  `apexadmin` — not just `deploy` as `docs/backend-server.md` §1 currently
  documents. Confirm with whoever owns `ayan`/`apexadmin` whether both should
  carry over to the new box before replicating this 1:1.
- `.htpasswd_dbadmin` (nginx basic auth in front of pgweb, both live and
  staging) is `-rw-r----- root:www-data` — not world-readable. Preserve this
  permission mode on the new box; a `cp` as the wrong user can silently
  loosen it.

## What's NOT yet done

- None of the above was verified against a written restore/rebuild test —
  this file is the first time it's been written down at all. Treat a fresh
  Droplet's hardening as "re-apply from this file, then diff the result
  against this file again" rather than assuming a provisioning script covers
  it, since no such script exists yet.
