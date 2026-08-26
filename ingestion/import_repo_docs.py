"""One-time (re-runnable) backfill: imports this repository's own docs,
ARCHITECTURE.md, and a synthesized codebase-overview document into the new
`repo_docs` table (schema/034_repo_docs.sql), so they're queryable through
MCP the same way emails/calls/assets are. Added 2026-08-26.

Every row is hardcoded access_tier='tier1' (Raj-only) -- not classified
per-document the way assets are, since this whole category (internal
engineering/ops detail: credentials, infra topology, schema internals) has
no business being visible to any identity but `full`.

Not an ongoing fetcher -- these are files already in this repo, imported by
hand. Re-run to pick up doc edits (safe: ON CONFLICT upsert), but nothing
calls this automatically.

Usage: python -m ingestion.import_repo_docs [--dry-run]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import get_live_conn, get_staging_conn

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every docs/*.md file is imported except docs/training/ (video scripts, not
# engineering docs) and the two files deleted in this same change (see
# CLAUDE.md's "Read next" list) -- handoff-access-tier-dev.md (explicitly
# superseded by docs/local-dev-and-team-onboarding.md) and
# infrastructure-roadmap.md (superseded by docs/migration-status.md).
DOC_FILES = sorted(
    p for p in (REPO_ROOT / "docs").glob("*.md")
)

ARCHITECTURE_FILES = [REPO_ROOT / "ARCHITECTURE.md"]


def _slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _title_from_body(body, fallback):
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line.lstrip("#").strip() if line.startswith("#") else line
    return fallback


def _codebase_overview_body():
    """Synthesizes one overview document describing the repo's directory
    structure and the purpose of each major module -- there is no single
    existing file that plays this role (ARCHITECTURE.md is plain-language/
    non-engineering by design, per CLAUDE.md), so this is generated from
    the actual current top-level layout rather than copied from a source
    file. Kept intentionally short and structural (what lives where and
    why), not a restatement of ARCHITECTURE.md or any docs/ file."""
    sections = []
    top_level_dirs = {
        "ingestion/": "Raw-data fetchers (Gmail, Zoho, Fireflies, Fathom, Odoo) and the recurring sweep server.",
        "wiki_ingestion/": "The pipeline that turns raw rows into synthesized wiki pages via `claude -p` subprocesses.",
        "mcp_server/": "The MCP server: tool implementations, per-identity access-tier/redaction enforcement, employee and asset write paths.",
        "schema/": "Numbered SQL migration files (source of truth for every table) plus the migration runner.",
        "loaders/": "One-off/maintenance scripts (e.g. staging data reset) that aren't part of any recurring pipeline.",
        "parsers/": "Format-specific extraction helpers (PDF/DOCX/XLSX/CSV text extraction for attachments).",
        "frontend_threads/": "Schema and support code for the separate eoxs-frontend-threads service's database.",
        "deploy/": "systemd units, nginx configs, and hardening notes for the live server(s).",
        "docs/": "Living reference documentation for every subsystem -- see CLAUDE.md's reading order.",
    }
    sections.append("# Codebase Overview (synthesized)\n")
    sections.append(
        "Generated reference describing this repository's directory structure and the "
        "purpose of each major module, for querying via get_repo_doc/search_repo_docs. "
        "Not a substitute for ARCHITECTURE.md (plain-language, no engineering background "
        "needed) or the per-subsystem docs/ files (deep detail) -- this is the structural "
        "map connecting the two.\n"
    )
    sections.append("## Top-level layout\n")
    for d, desc in top_level_dirs.items():
        sections.append(f"- **`{d}`** -- {desc}")
    sections.append(
        "\n## Root files\n"
        "- **`ARCHITECTURE.md`** -- plain-language system overview.\n"
        "- **`CLAUDE.md`** -- orientation for a fresh session; the canonical reading order "
        "and current-state summary.\n"
        "- **`HANDOFF.md`** -- superseded historical context; CLAUDE.md supersedes it.\n"
        "- **`sync.py`** -- entry point tying ingestion/wiki-ingestion together for manual runs.\n"
        "- **`requirements.txt`** -- Python dependencies (single venv for the whole repo).\n"
    )
    sections.append(
        "\n## Where to look for X\n"
        "- A new raw data source: `ingestion/<source>_fetcher.py`, registered in "
        "`ingestion/server.py`'s sweep source list.\n"
        "- A new MCP tool: `mcp_server/server.py` (TOOLS dict, tool defs, TIER_FILTERED_TOOLS "
        "if it reads tiered data), or its own module (e.g. `mcp_server/employees.py`) for a "
        "large, self-contained tool set.\n"
        "- A schema change: a new numbered file in `schema/`, applied via "
        "`schema/run_migrations.py`.\n"
        "- Access-tier/redaction logic: `mcp_server/redaction.py` and the `access_tier` "
        "column/enum (`schema/019_access_tier.sql`, `schema/020_tier2_confidential.sql`).\n"
    )
    return "\n".join(sections)


def _upsert(conn, slug, title, body, doc_type, source_file_path):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repo_docs (slug, title, body, doc_type, source_file_path, access_tier)
            VALUES (%s, %s, %s, %s, %s, 'tier1')
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                doc_type = EXCLUDED.doc_type,
                source_file_path = EXCLUDED.source_file_path,
                updated_at = now()
            RETURNING id
            """,
            (slug, title, body, doc_type, source_file_path),
        )
        doc_id = cur.fetchone()["id"]
    conn.commit()
    return doc_id


def _rows_to_import():
    rows = []
    for path in DOC_FILES:
        body = path.read_text(encoding="utf-8")
        slug = _slugify(path.stem)
        title = _title_from_body(body, fallback=path.stem)
        rows.append((slug, title, body, "doc", f"docs/{path.name}"))
    for path in ARCHITECTURE_FILES:
        body = path.read_text(encoding="utf-8")
        slug = _slugify(path.stem)
        title = _title_from_body(body, fallback=path.stem)
        rows.append((slug, title, body, "architecture", path.name))
    rows.append(("codebase-overview", "Codebase Overview", _codebase_overview_body(), "codebase", None))
    return rows


def import_all(dry_run=False):
    results = {}
    for slug, title, body, doc_type, source_file_path in _rows_to_import():
        if dry_run:
            print(f"[dry-run] would import {slug!r} ({title!r}, {doc_type}, {len(body)} chars)")
            results[slug] = {"title": title, "doc_type": doc_type, "chars": len(body)}
            continue

        live_conn = get_live_conn()
        try:
            doc_id = _upsert(live_conn, slug, title, body, doc_type, source_file_path)
        finally:
            live_conn.close()

        try:
            staging_conn = get_staging_conn()
            try:
                _upsert(staging_conn, slug, title, body, doc_type, source_file_path)
            finally:
                staging_conn.close()
        except Exception as e:
            print(f"staging write failed for {slug!r} (live succeeded, continuing): {e}")

        print(f"imported {slug!r} -> repo_docs.id={doc_id} ({doc_type})")
        results[slug] = {"id": doc_id, "title": title, "doc_type": doc_type}
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    import_all(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
