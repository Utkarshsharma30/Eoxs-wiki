# Tier 2 — Role-Specific Scripts

Only relevant to the intern's assigned task — everyone still watches all of
Tier 1 first, since these assume that vocabulary without re-explaining it.
Each follows the same format as Tier 1: bullet talking points, `[SHOW: ...]`
cues, "before you record" checklist. Fill these in once a real person is
assigned to the role — the outline below is the starting shape for each,
not a finished script.

---

## Ingestion handling

**Goal:** confidently add/fix a data source fetcher and know how to test it
safely.

Draft outline once assigned:
- Walk `ingestion/` structure: one file per source, shared helpers
  (`db.py`, `state.py`, `retry.py`).
- `[SHOW]` reading an existing fetcher end to end as the template for a new
  one.
- `[SHOW]` running `--dry-run` before ever writing for real.
- The local dev setup from `docs/local-dev-and-team-onboarding.md` — this is
  where they'll actually work, never on the live server.
- How a fetcher's fix reaches production (PR → merge → the oneshot service
  just picks it up next scheduled run, no manual restart needed).

## Auditing / data QA

**Goal:** confidently check whether what the pipeline produced is actually
correct, and know how to report a real problem.

Draft outline once assigned:
- `[SHOW]` spot-checking a wiki page against its raw source for factual
  accuracy — the actual skill, not just "does it look reasonable."
- Where to look when something's wrong: is it a synthesis problem (the AI
  drafted it wrong) or an ingestion problem (the raw data itself is wrong or
  missing)?
- How tiering/redaction QA works — testing the same query across identities,
  as shown in Tier 1 Video 5, but as a deliberate checking habit rather than
  a one-off demo.
- How to file what they find (Linear "EDB" team, or however this gets
  decided once someone's actually doing this).

## Linear board review

**Goal:** read the EDB board and understand what a healthy vs unhealthy
pipeline cycle looks like.

Draft outline once assigned:
- `[SHOW]` the persistent board issue and what a normal cycle's report looks
  like (chunks processed, pages drafted/promoted, errors).
- What a bad cycle looks like, and who to flag it to.
- They likely need their own Linear login on the EDB team, not database or
  server access at all — see the access matrix in
  `docs/local-dev-and-team-onboarding.md`.

## Server health checks

**Goal:** confidently check whether the live system is healthy without full
server access.

Draft outline once assigned:
- `[SHOW]` the restricted status/log commands they're actually granted (see
  `docs/local-dev-and-team-onboarding.md` §6) — `systemctl status` on the
  three services, reading logs.
- What healthy output looks like for each of the three services
  (`eoxs-mcp.service` persistent, `eoxs-wiki-pipeline.service` and
  `eoxs-sweep.service` oneshot/scheduled) vs what a failure looks like.
- What they can/can't do about a problem themselves (nothing that needs
  `sudo` beyond the read-only commands they're granted) and who to escalate
  to.
