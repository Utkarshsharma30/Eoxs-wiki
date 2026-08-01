"""Loads call transcripts into call_transcripts / call_segments:
  raw/calls/<YYYY-MM>/*.md          (fireflies, unmatched to a client)
  raw/fathom/<YYYY-MM>/*.md         (fathom, no client routing)
  raw/clients/<client>/calls/*.md   (fireflies, matched to a client -> client_id set)

Idempotent: upserts on source_file_path; skips unchanged files via db_sync_state.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders._db import get_conn, VAULT_ROOT
from parsers.frontmatter import parse_frontmatter
from parsers.call_body import extract_transcript, split_segments

SOURCE_TYPE = "call_transcript"


def to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def to_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def to_str_list(value):
    if not value:
        return []
    return [str(v) for v in value]


def get_client_id(conn, client_slug):
    if not client_slug:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM clients WHERE slug = %s", (client_slug,))
        row = cur.fetchone()
        return row[0] if row else None


def load_file(conn, path, client_slug=None):
    rel_path = str(path.relative_to(VAULT_ROOT))
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT file_mtime FROM db_sync_state WHERE source_type = %s AND source_file_path = %s",
            (SOURCE_TYPE, rel_path),
        )
        row = cur.fetchone()
        if row and row[0] >= mtime:
            return "skipped"

    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)

    source = meta.get("source")
    if source not in ("fireflies", "fathom"):
        return "no_source"

    external_id = meta.get("fireflies_id") if source == "fireflies" else meta.get("fathom_recording_id")
    if not external_id:
        return "no_external_id"

    transcript = extract_transcript(body)
    segments = split_segments(transcript)
    client_id = get_client_id(conn, client_slug)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO call_transcripts (
                source, external_id, meeting_title, call_date, duration_seconds,
                duration_human, host_email, participants, recording_url,
                fireflies_summary, key_topics, action_items, tags,
                generated_at, generated_hash, transcript_body, client_id,
                source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_file_path) DO UPDATE SET
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
                generated_hash = EXCLUDED.generated_hash,
                transcript_body = EXCLUDED.transcript_body,
                client_id = EXCLUDED.client_id,
                source_file_mtime = EXCLUDED.source_file_mtime,
                updated_at = now()
            RETURNING id
            """,
            (
                source, str(external_id), meta.get("meeting_title"), to_date(meta.get("date")),
                meta.get("duration_seconds"), meta.get("duration"), meta.get("host_email"),
                to_str_list(meta.get("participants")), meta.get("recording_url"),
                meta.get("fireflies_summary"), to_str_list(meta.get("key_topics")),
                to_str_list(meta.get("action_items")), to_str_list(meta.get("tags")),
                to_dt(meta.get("generated_at")), meta.get("generated_hash"),
                transcript, client_id, rel_path, mtime,
            ),
        )
        call_id = cur.fetchone()[0]

        cur.execute("DELETE FROM call_segments WHERE call_id = %s", (call_id,))
        for seg in segments:
            cur.execute(
                "INSERT INTO call_segments (call_id, segment_order, speaker, text) VALUES (%s,%s,%s,%s)",
                (call_id, seg["segment_order"], seg["speaker"], seg["text"]),
            )

        cur.execute(
            """
            INSERT INTO db_sync_state (source_type, source_file_path, file_mtime)
            VALUES (%s,%s,%s)
            ON CONFLICT (source_type, source_file_path) DO UPDATE SET
                file_mtime = EXCLUDED.file_mtime, last_loaded_at = now()
            """,
            (SOURCE_TYPE, rel_path, mtime),
        )
    conn.commit()
    return "loaded"


def main():
    conn = get_conn()
    counts = {"loaded": 0, "skipped": 0, "no_source": 0, "no_external_id": 0, "error": 0}

    roots = [
        (VAULT_ROOT / "raw/calls", None),
        (VAULT_ROOT / "raw/fathom", None),
    ]
    clients_root = VAULT_ROOT / "raw/clients"
    if clients_root.is_dir():
        for client_dir in clients_root.iterdir():
            calls_dir = client_dir / "calls"
            if calls_dir.is_dir():
                roots.append((calls_dir, client_dir.name))

    for root, client_slug in roots:
        if not root.exists():
            continue
        n = 0
        for path in root.rglob("*.md"):
            try:
                result = load_file(conn, path, client_slug)
                counts[result] += 1
            except Exception as e:
                conn.rollback()
                print(f"FAIL {path}: {e}")
                counts["error"] += 1
            n += 1
        print(f"{root.relative_to(VAULT_ROOT)} (client={client_slug}): scanned {n} files")

    conn.close()
    print("totals:", counts)


if __name__ == "__main__":
    main()
