"""Resolves wiki_citations.source_ref_raw against loaded tables where a
reliable match exists. Frontmatter 'sources:' entries are free-text slugs,
not stable foreign keys, so this only resolves what matches unambiguously:

  1. wiki_pages.title (slugified)      -> source_type='wiki_page'
  2. tickets.ticket_number (T#####)    -> source_type='ticket'
  3. call_transcripts.external_id      -> source_type='call_transcript'
     or source_file_path stem
  4. email_threads.source_file_path stem -> source_type='email_thread'

Anything left is genuinely ambiguous free text (e.g. 'ppc-metals-recovered-batch-1')
and stays source_type='unresolved' rather than risk a wrong match.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loaders._db import get_conn


def slugify(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


TICKET_RE = re.compile(r"^t(\d{5})", re.IGNORECASE)


def main():
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT id, title FROM wiki_pages")
        title_by_slug = {}
        for pid, title in cur.fetchall():
            title_by_slug.setdefault(slugify(title), []).append(pid)

        cur.execute("SELECT id, ticket_number FROM tickets")
        ticket_by_number = {num.lower(): tid for tid, num in cur.fetchall()}

        cur.execute("SELECT id, source_file_path FROM call_transcripts")
        call_by_stem = {Path(p).stem.lower(): cid for cid, p in cur.fetchall()}

        cur.execute("SELECT id, source_file_path FROM email_threads")
        email_by_stem = {Path(p).stem.lower(): eid for eid, p in cur.fetchall()}

        cur.execute("SELECT id, wiki_page_id, source_ref_raw FROM wiki_citations")
        citations = cur.fetchall()

    counts = {"wiki_page": 0, "ticket": 0, "call_transcript": 0, "email_thread": 0, "unresolved": 0}

    with conn.cursor() as cur:
        for cit_id, wiki_page_id, ref_raw in citations:
            slug = slugify(ref_raw)
            source_type, source_id = "unresolved", None

            # 1. wiki page title match (skip self-citation and ambiguous multi-matches)
            candidates = title_by_slug.get(slug, [])
            candidates = [c for c in candidates if c != wiki_page_id]
            if len(candidates) == 1:
                source_type, source_id = "wiki_page", candidates[0]

            # 2. ticket number
            if source_type == "unresolved":
                m = TICKET_RE.match(ref_raw)
                if m:
                    key = f"t{m.group(1)}"
                    if key in ticket_by_number:
                        source_type, source_id = "ticket", ticket_by_number[key]

            # 3. call transcript filename stem
            if source_type == "unresolved" and slug in call_by_stem:
                source_type, source_id = "call_transcript", call_by_stem[slug]

            # 4. email thread filename stem
            if source_type == "unresolved" and slug in email_by_stem:
                source_type, source_id = "email_thread", email_by_stem[slug]

            counts[source_type] += 1
            cur.execute(
                "UPDATE wiki_citations SET source_type = %s, source_id = %s WHERE id = %s",
                (source_type, source_id, cit_id),
            )

    conn.commit()
    conn.close()

    print(f"total citations: {len(citations)}")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
