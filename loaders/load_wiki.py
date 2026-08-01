"""Loads wiki/**/*.md into wiki_pages, then resolves the [[wikilink]] graph
into wiki_links, frontmatter 'sources' into wiki_citations (unresolved —
raw source tables aren't loaded into this schema pass), and callouts into
wiki_flags.

Idempotent: upserts on source_file_path. Link/citation/flag resolution
always fully re-derives from current wiki_pages content (cheap at this
scale), so it's safe to rerun standalone after only some pages changed.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders._db import get_conn, VAULT_ROOT
from parsers.frontmatter import parse_frontmatter
from parsers.wikilinks import extract_wikilinks, extract_flags

SOURCE_TYPE = "wiki_page"
VALID_TYPES = {"entity", "concept", "source", "analysis", "overview", "prospect"}


def to_str_list(value):
    """YAML parses bare numeric-looking list items (e.g. a year tag '2021')
    as ints, not strings — coerce everything to str so it fits a text[] column."""
    if not value:
        return []
    return [str(v) for v in value]


import re
LEADING_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def to_date(value):
    """Some pages' 'updated:' is a rich annotation, not a bare date
    (e.g. '2026-07-30 (catch-up sync — ...)'). Extract the leading date;
    the full original string is preserved separately in updated_raw."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    m = LEADING_DATE_RE.match(str(value))
    return date.fromisoformat(m.group(1)) if m else None


def load_pages(conn):
    root = VAULT_ROOT / "wiki"
    counts = {"loaded": 0, "skipped_bad_type": 0, "error": 0}

    for path in root.rglob("*.md"):
        rel_path = str(path.relative_to(VAULT_ROOT))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        text = path.read_text(encoding="utf-8")
        try:
            meta, body = parse_frontmatter(text)
        except Exception as e:
            print(f"skip (invalid YAML frontmatter): {rel_path}: {e}")
            counts["error"] += 1
            continue

        page_type = meta.get("type")
        if page_type not in VALID_TYPES:
            print(f"skip (bad/missing type={page_type!r}): {rel_path}")
            counts["skipped_bad_type"] += 1
            continue

        title = meta.get("title") or path.stem

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wiki_pages (
                        title, page_type, entity_class, tags, sources_raw,
                        created_date, updated_date, updated_raw, generated_hash, body,
                        source_file_path, source_file_mtime
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_file_path) DO UPDATE SET
                        title = EXCLUDED.title,
                        page_type = EXCLUDED.page_type,
                        entity_class = EXCLUDED.entity_class,
                        tags = EXCLUDED.tags,
                        sources_raw = EXCLUDED.sources_raw,
                        created_date = EXCLUDED.created_date,
                        updated_date = EXCLUDED.updated_date,
                        updated_raw = EXCLUDED.updated_raw,
                        generated_hash = EXCLUDED.generated_hash,
                        body = EXCLUDED.body,
                        source_file_mtime = EXCLUDED.source_file_mtime,
                        updated_at = now()
                    """,
                    (
                        title, page_type, meta.get("entity_class"),
                        to_str_list(meta.get("tags")), to_str_list(meta.get("sources")),
                        to_date(meta.get("created")), to_date(meta.get("updated")),
                        (str(meta.get("updated")) if meta.get("updated") is not None else None),
                        meta.get("generated_hash"), body, rel_path, mtime,
                    ),
                )
            conn.commit()
            counts["loaded"] += 1
        except Exception as e:
            conn.rollback()
            print(f"FAIL {rel_path}: {e}")
            counts["error"] += 1

    return counts


def resolve_links_and_flags(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, body FROM wiki_pages")
        pages = cur.fetchall()

    title_to_id = {title: pid for pid, title, _ in pages}

    with conn.cursor() as cur:
        cur.execute("DELETE FROM wiki_links")
        cur.execute("DELETE FROM wiki_flags")

        link_count, unresolved_count, flag_count = 0, 0, 0
        for page_id, _, body in pages:
            for link in extract_wikilinks(body):
                to_id = title_to_id.get(link["to_title_raw"])
                if to_id is None:
                    unresolved_count += 1
                cur.execute(
                    """
                    INSERT INTO wiki_links (from_page_id, to_page_id, to_title_raw, display_text, context_snippet)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (page_id, to_id, link["to_title_raw"], link["display_text"], link["context_snippet"]),
                )
                link_count += 1

            for flag in extract_flags(body):
                cur.execute(
                    "INSERT INTO wiki_flags (wiki_page_id, flag_type, text) VALUES (%s,%s,%s)",
                    (page_id, flag["flag_type"], flag["text"]),
                )
                flag_count += 1

    conn.commit()
    return link_count, unresolved_count, flag_count


def resolve_citations(conn):
    """Citations are logged with source_type='unknown', source_id=NULL for now
    since raw source tables (tickets/invoices/calls/etc.) aren't in this
    schema pass yet — only email_thread is loaded so far. Re-run this after
    each new raw source loader lands to backfill resolution."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, sources_raw FROM wiki_pages")
        pages = cur.fetchall()

        cur.execute("DELETE FROM wiki_citations")
        count = 0
        for page_id, sources_raw in pages:
            for ref in (sources_raw or []):
                cur.execute(
                    """
                    INSERT INTO wiki_citations (wiki_page_id, source_type, source_id, source_ref_raw)
                    VALUES (%s, 'unresolved', NULL, %s)
                    """,
                    (page_id, ref),
                )
                count += 1
    conn.commit()
    return count


def main():
    conn = get_conn()
    counts = load_pages(conn)
    print("page load:", counts)

    link_count, unresolved_count, flag_count = resolve_links_and_flags(conn)
    print(f"links: {link_count} total, {unresolved_count} unresolved (target page not found)")
    print(f"flags: {flag_count}")

    citation_count = resolve_citations(conn)
    print(f"citations: {citation_count} (all unresolved — raw source tables beyond emails not loaded yet)")

    conn.close()


if __name__ == "__main__":
    main()
