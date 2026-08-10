"""Writes a fetched call transcript (Fireflies or Fathom) into
call_transcripts / call_segments. Upsert key for API-fetched rows (no
filesystem path) is (source, external_id) via the partial unique index
added in migration 013 -- see that migration's comment for why the
original 3-column (source, external_id, source_file_path) constraint
can't be used with a NULL path. Follows write_email.py's exact pattern:
takes conn first, full DELETE+reinsert of children, commits internally,
returns the parent row id. Called via ingestion.db.dual_write() so it
runs once against live and once against staging.
"""


def write_call(conn, *, source, external_id, meeting_title, call_date, duration_seconds,
                duration_human, host_email, participants, recording_url, fireflies_summary,
                key_topics, action_items, tags, generated_at, client_id, transcript_body, segments,
                access_tier="tier1"):
    """segments: list of dicts {segment_order, speaker, text}.
    access_tier: caller's responsibility to classify (ingestion.inline_tier_classifier).
    Only applied on the INITIAL insert (excluded from DO UPDATE SET below) -- in practice
    existing_call() dedup means calls are never re-upserted anyway (immutable once
    transcribed), but kept consistent with every other tiered table's convention. Defaults
    to 'tier1' (fail closed).
    Returns the call's DB id (from the live connection's perspective when
    called via dual_write; staging's return value is discarded by the caller)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_transcripts (
                source, external_id, meeting_title, call_date, duration_seconds,
                duration_human, host_email, participants, recording_url,
                fireflies_summary, key_topics, action_items, tags, generated_at,
                client_id, transcript_body, access_tier, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,now())
            ON CONFLICT (source, external_id) WHERE source_file_path IS NULL DO UPDATE SET
                meeting_title = EXCLUDED.meeting_title,
                call_date = EXCLUDED.call_date,
                duration_seconds = EXCLUDED.duration_seconds,
                duration_human = EXCLUDED.duration_human,
                host_email = EXCLUDED.host_email,
                participants = EXCLUDED.participants,
                recording_url = EXCLUDED.recording_url,
                fireflies_summary = EXCLUDED.fireflies_summary,
                key_topics = EXCLUDED.key_topics,
                action_items = EXCLUDED.action_items,
                tags = EXCLUDED.tags,
                generated_at = EXCLUDED.generated_at,
                client_id = EXCLUDED.client_id,
                transcript_body = EXCLUDED.transcript_body,
                updated_at = now()
            RETURNING id
            """,
            (
                source, external_id, meeting_title, call_date, duration_seconds,
                duration_human, host_email, participants, recording_url,
                fireflies_summary, key_topics, action_items, tags, generated_at,
                client_id, transcript_body, access_tier,
            ),
        )
        call_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM call_segments WHERE call_id = %s", (call_id,))
        for seg in segments:
            cur.execute(
                """
                INSERT INTO call_segments (call_id, segment_order, speaker, text)
                VALUES (%s,%s,%s,%s)
                """,
                (call_id, seg["segment_order"], seg.get("speaker"), seg["text"]),
            )

    conn.commit()
    return call_id


def existing_call(conn, source, external_id):
    """Calls are immutable once transcribed (unlike email threads, which
    can grow new messages), so a simple existence check is enough dedup --
    no message-count-style comparison needed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM call_transcripts WHERE source = %s AND external_id = %s AND source_file_path IS NULL",
            (source, external_id),
        )
        return cur.fetchone() is not None
