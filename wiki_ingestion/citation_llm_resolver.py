"""Second pass at the wiki_citations rows still stuck at
source_type='unresolved' after citation_resolver.py's mechanical pass
(607 remaining as of that run). Where the mechanical pass required an
exact parseable {source}-{date}-{slug} shape, this pass searches real
content (full-text where available, ILIKE fallback) across every
candidate table -- email_threads, call_transcripts, tickets,
implementation_tasks, and wiki_pages -- and asks an LLM to judge whether
any candidate is GENUINELY the record being cited, not just topically
similar.

Explicit design constraint from the user: a wrong citation is worse than
an honest "unresolved" one. Two-stage judgment, not one: stage 1 proposes
a match (only HIGH confidence is even considered, MEDIUM/LOW/no-match
leave the citation untouched); stage 2 is a SEPARATE, independent LLM
call whose only job is to try to REFUTE the proposed match, defaulting to
refuted when uncertain. A citation is only written if it survives both
stages. This was added after a first version (single-stage, propose-only)
was smoke-tested on 5 real citations and got 2 wrong (40% false-positive
rate on its own "HIGH confidence" matches -- e.g. matched a citation as
generic as "remya-gmail-archive", which carries no specific identifying
information at all, to one arbitrary specific email thread). One-stage
proposal alone is not trustworthy enough for this; adversarial
verification is required.

Same CLASSIFIER_ANTHROPIC_API_KEY, same incremental-write-per-item
pattern as every other classifier in this system (a mid-run kill loses
at most the current batch, not everything).
"""
import asyncio
import logging
import os
import re
import time

import anthropic

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wiki_ingestion.citation_llm_resolver")

_MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 10
MAX_RETRIES = 5
CANDIDATES_PER_TABLE = 4
SNIPPET_CHARS = 220

_PREFIX_RE = re.compile(r"^(raj_gmail|ron_gmail|remya_gmail|support_zoho|fireflies|fathom|tickets|calls)-")
_DATE_RE = re.compile(r"^\d{4}-\d{1,2}(-\d{1,2})?-")


def _clean_query(source_ref_raw):
    s = _PREFIX_RE.sub("", source_ref_raw)
    s = _DATE_RE.sub("", s)
    return s.replace("-", " ").replace("_", " ").strip() or source_ref_raw


def _search_email_threads(conn, query_text, limit=CANDIDATES_PER_TABLE):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.subject AS title,
                   left(coalesce(m.body, ''), %s) AS snippet
            FROM email_threads t
            LEFT JOIN LATERAL (
                SELECT body FROM email_messages WHERE thread_id = t.id
                ORDER BY message_index LIMIT 1
            ) m ON true
            WHERE t.subject ILIKE %s
               OR EXISTS (SELECT 1 FROM email_messages em WHERE em.thread_id = t.id
                          AND em.body_tsv @@ plainto_tsquery('english', %s))
            LIMIT %s
            """,
            (SNIPPET_CHARS, f"%{query_text}%", query_text, limit),
        )
        return [{"source_type": "email_thread", "id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in cur.fetchall()]


def _search_call_transcripts(conn, query_text, limit=CANDIDATES_PER_TABLE):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, meeting_title AS title, left(coalesce(fireflies_summary, transcript_body, ''), %s) AS snippet
            FROM call_transcripts
            WHERE meeting_title ILIKE %s OR transcript_tsv @@ plainto_tsquery('english', %s)
            LIMIT %s
            """,
            (SNIPPET_CHARS, f"%{query_text}%", query_text, limit),
        )
        return [{"source_type": "call_transcript", "id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in cur.fetchall()]


def _search_tickets(conn, query_text, limit=CANDIDATES_PER_TABLE):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject AS title, left(coalesce(description, ''), %s) AS snippet
            FROM tickets WHERE subject ILIKE %s OR description ILIKE %s
            LIMIT %s
            """,
            (SNIPPET_CHARS, f"%{query_text}%", f"%{query_text}%", limit),
        )
        return [{"source_type": "ticket", "id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in cur.fetchall()]


def _search_implementation_tasks(conn, query_text, limit=CANDIDATES_PER_TABLE):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT odoo_task_id, task_name AS title, left(coalesce(description, ''), %s) AS snippet
            FROM implementation_tasks WHERE task_name ILIKE %s OR description ILIKE %s
            LIMIT %s
            """,
            (SNIPPET_CHARS, f"%{query_text}%", f"%{query_text}%", limit),
        )
        # source_id for implementation_task citations is odoo_task_id, not the internal id -- see agent_mcp_server.py's tool description.
        return [{"source_type": "implementation_task", "id": r["odoo_task_id"], "title": r["title"], "snippet": r["snippet"]} for r in cur.fetchall()]


def _search_wiki_pages(conn, query_text, exclude_page_id, limit=CANDIDATES_PER_TABLE):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, left(body, %s) AS snippet
            FROM wiki_pages WHERE id != %s AND (title ILIKE %s OR body_tsv @@ plainto_tsquery('english', %s))
            LIMIT %s
            """,
            (SNIPPET_CHARS, exclude_page_id, f"%{query_text}%", query_text, limit),
        )
        return [{"source_type": "wiki_page", "id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in cur.fetchall()]


def _gather_candidates(conn, query_text, citing_page_id):
    candidates = []
    candidates += _search_email_threads(conn, query_text)
    candidates += _search_call_transcripts(conn, query_text)
    candidates += _search_tickets(conn, query_text)
    candidates += _search_implementation_tasks(conn, query_text)
    candidates += _search_wiki_pages(conn, query_text, citing_page_id)
    return candidates


def _format_candidates(candidates):
    lines = []
    for i, c in enumerate(candidates, start=1):
        snippet = (c["snippet"] or "").replace("\n", " ").strip()
        lines.append(f"{i}. [{c['source_type']} id={c['id']}] \"{c['title']}\" — {snippet}")
    return "\n".join(lines) if lines else "(no candidates found)"


_PROMPT = """You are verifying an old, broken citation in Cruz, EOXS's internal knowledge base.

A wiki page titled "{page_title}" has a citation whose original reference text is: "{source_ref_raw}"
(this is a free-text label from an old import that was never matched to a real record).

Below are candidate real records found by searching the database for related content. Decide if
EXACTLY ONE of them is clearly, specifically the record this citation refers to -- not just on the
same general topic, but the actual same event/deal/person/thread, with matching specific details
(names, dates, numbers, context). Do NOT match on topic similarity alone.

Candidates:
{candidates_block}

If you are confident one candidate is the genuine match, answer on one line:
MATCH <candidate_number> HIGH
or, if plausible but you have real uncertainty:
MATCH <candidate_number> MEDIUM

If no candidate is clearly, specifically correct -- including if you're just guessing from general
topic overlap -- answer exactly:
NO_MATCH

Answer with exactly one line, nothing else."""


_REFUTE_PROMPT = """You are skeptically reviewing a PROPOSED citation match in Cruz, EOXS's internal
knowledge base -- someone else already proposed this match; your only job is to try to find a reason
it is WRONG.

Wiki page: "{page_title}"
Citation text being resolved: "{source_ref_raw}"

Proposed match: [{source_type} id={source_id}] "{title}" — {snippet}

Look specifically for: the citation text not containing enough specific detail to justify picking
this exact record over any other on the same general topic; the record's specific details (names,
dates, deal specifics, people involved) not actually aligning with what the citation text implies;
or this being a topic-similarity match rather than a genuine same-record match.

If you cannot find a real, specific problem and the match holds up under scrutiny, answer: CONFIRMED
If you have ANY real doubt, or the citation text is too generic to justify picking this specific
record, answer: REFUTED

Default to REFUTED when uncertain -- an honestly unresolved citation is better than a wrong one.

Answer with exactly one word: CONFIRMED or REFUTED."""


async def _propose(client, sem, page_title, source_ref_raw, candidates):
    if not candidates:
        return None
    prompt = _PROMPT.format(
        page_title=page_title, source_ref_raw=source_ref_raw, candidates_block=_format_candidates(candidates)
    )
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=20, messages=[{"role": "user", "content": prompt}],
                )
                answer = resp.content[0].text.strip().upper()
                m = re.match(r"MATCH\s+(\d+)\s+(HIGH|MEDIUM)", answer)
                if m and answer.split()[-1] == "HIGH":
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(candidates):
                        return candidates[idx]
                return None
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 2, 60)
                logger.warning("rate limited, retrying in %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("LLM citation match failed, leaving unresolved: %s", e)
                return None
        logger.warning("exhausted retries, leaving unresolved")
        return None


async def _survives_refutation(client, sem, page_title, source_ref_raw, match):
    prompt = _REFUTE_PROMPT.format(
        page_title=page_title, source_ref_raw=source_ref_raw,
        source_type=match["source_type"], source_id=match["id"], title=match["title"],
        snippet=(match["snippet"] or "").replace("\n", " ").strip(),
    )
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=10, messages=[{"role": "user", "content": prompt}],
                )
                answer = resp.content[0].text.strip().upper()
                return "CONFIRMED" in answer and "REFUTED" not in answer
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 2, 60)
                logger.warning("rate limited, retrying in %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("refutation check failed, treating as refuted (fail closed): %s", e)
                return False
        logger.warning("exhausted retries on refutation check, treating as refuted (fail closed)")
        return False


async def _judge_one(client, sem, page_title, source_ref_raw, candidates):
    """Two-stage: propose, then an independent adversarial check that must
    also pass. Returns the candidate dict only if BOTH stages agree."""
    proposed = await _propose(client, sem, page_title, source_ref_raw, candidates)
    if not proposed:
        return None
    survived = await _survives_refutation(client, sem, page_title, source_ref_raw, proposed)
    if not survived:
        logger.info("proposed match for %r rejected by adversarial check: %s id=%s",
                    source_ref_raw, proposed["source_type"], proposed["id"])
        return None
    return proposed


def _fetch_unresolved(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id AS citation_id, c.wiki_page_id, c.source_ref_raw, p.title AS page_title
            FROM wiki_citations c JOIN wiki_pages p ON p.id = c.wiki_page_id
            WHERE c.source_type = 'unresolved'
            """
        )
        return cur.fetchall()


async def _run(conn, rows):
    client = anthropic.AsyncAnthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    matched = 0
    start = time.monotonic()

    async def worker(row):
        nonlocal completed, matched
        query_text = _clean_query(row["source_ref_raw"])
        candidates = _gather_candidates(conn, query_text, row["wiki_page_id"])
        match = await _judge_one(client, sem, row["page_title"], row["source_ref_raw"], candidates)
        if match:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wiki_citations SET source_type = %s, source_id = %s, updated_at = now() WHERE id = %s",
                    (match["source_type"], match["id"], row["citation_id"]),
                )
            conn.commit()
            matched += 1
            logger.info("resolved citation %d: %r -> %s id=%s", row["citation_id"], row["source_ref_raw"],
                        match["source_type"], match["id"])
        completed += 1
        if completed % 50 == 0 or completed == len(rows):
            elapsed = time.monotonic() - start
            rate = completed / elapsed if elapsed else 0
            eta = (len(rows) - completed) / rate if rate else 0
            logger.info("progress: %d/%d (%.1f/s, ~%.0fs remaining, %d matched so far)",
                        completed, len(rows), rate, eta, matched)

    await asyncio.gather(*(worker(row) for row in rows))
    return matched


def main():
    conn = get_live_conn()
    try:
        rows = _fetch_unresolved(conn)
        logger.info("citations still unresolved: %d", len(rows))
        if not rows:
            logger.info("nothing to do")
            return
        matched = asyncio.run(_run(conn, rows))
        logger.info("LLM-assisted pass: %d/%d resolved, %d remain genuinely unresolved", matched, len(rows), len(rows) - matched)

        with conn.cursor() as cur:
            cur.execute("SELECT source_type, count(*) FROM wiki_citations GROUP BY source_type ORDER BY source_type")
            for row in cur.fetchall():
                logger.info("final: %s = %d", row["source_type"], row["count"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
