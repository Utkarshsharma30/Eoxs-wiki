"""Client routing for call ingestion -- classifies a call's participant
email addresses against the clients/contacts tables to decide whether the
call belongs to a specific client (sets call_transcripts.client_id) or is
general/unmatched.

Deliberately simpler than the old pipeline's tools/pipeline_common/
contacts.py: that module also matched internal/external/prospect buckets
by scanning raw/prospects/**/*.md in the vault repo, but the current
schema only has a client_id FK (no prospects table), and this system
avoids depending on the vault repo at fetch time -- clients/contacts here
are DB rows (seeded once via loaders/load_clients.py), not a live YAML/
markdown read. Matches the "client vs. general/unmatched" behavior
HANDOFF.md describes for the DB-native fetcher.
"""
from ingestion.db import get_live_conn


def load_client_index():
    """{'emails': {email_lower: client_id}, 'domains': {domain_lower: client_id}},
    built once per fetcher run from LIVE (routing data doesn't diverge
    live-vs-staging, and cursor/routing decisions are always live-driven)."""
    emails, domains, slugs = {}, {}, {}
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug, domains FROM clients")
            for row in cur.fetchall():
                slugs[row["id"]] = row["slug"]
                for d in row["domains"] or []:
                    domains[d.lower()] = row["id"]

            cur.execute(
                "SELECT client_id, email FROM contacts WHERE email IS NOT NULL AND client_id IS NOT NULL"
            )
            for row in cur.fetchall():
                emails[row["email"].lower()] = row["client_id"]
    finally:
        conn.close()
    return {"emails": emails, "domains": domains, "slugs": slugs}


def classify_client(index, participants):
    """Returns the matching client_id, or None if no participant matches a
    known client contact email or domain. Exact contact-email matches beat
    domain fallback matches, mirroring the old pipeline's tier ordering."""
    cleaned = [(p or "").lower().strip() for p in participants]

    for email in cleaned:
        if email in index["emails"]:
            return index["emails"][email]

    for email in cleaned:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain and domain in index["domains"]:
            return index["domains"][domain]

    return None
