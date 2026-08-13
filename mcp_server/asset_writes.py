"""Write path for the `assets` table (schema/031) -- curated internal
reference documents (SOPs, company overview, ICP, salary register,
product-feature specs, technical references). The second and, per explicit
instruction, deliberately LAST write-capable corner of this MCP server
alongside employees.py -- every other table stays exactly as read-only as
it was before either of these existed.

Why writing here is enough to keep the wiki fresh, with no extra plumbing:
wiki_ingestion/detect.py's candidates_assets() already picks up any row
whose `updated_at` has advanced past its stored cursor and content-hashes
it against wiki_ingest_seen -- built 2026-08-12 for the one-time backfill
importer, but it doesn't know or care whether an update came from a script
or a live tool call. A create_asset/update_asset call today is simply a new
way to advance `updated_at` on a row that pipeline was already watching --
the next 6-hour wiki-ingestion cycle re-drafts the corresponding wiki page
automatically, exactly like every other source in this system.

Two functions, two very different access shapes, both enforced entirely at
server-construction time (see server.py's build_server(), never from a
tool's own arguments):
  - create_asset: full (Raj) only. Not exposed to hr at all -- Isha's
    access is scoped to updating one existing document, not adding new ones.
  - update_asset: full (any slug) + hr, but hr is bound to a fixed
    `_allowed_slugs` set (today: just {'eoxs-salary-details'}) -- calling it
    for any other slug returns a plain permission error, not a partial
    success or a silent no-op.

`changed_by` and `_allowed_slugs` are both bound via functools.partial in
build_server(), the same construction-time-binding pattern `clearance` and
employees.py's `changed_by` already use -- neither is part of either tool's
inputSchema, so nothing a caller sends can widen its own scope or spoof who
made a change.
"""
import json
import sys

from mcp.types import Tool

from mcp_server.db import query, query_one, execute
from ingestion.inline_tier_classifier import classify_tier

ASSET_SUMMARY_COLUMNS = "id, slug, title, access_tier, source_file_path, imported_at, updated_at"


def _log_change(asset_id, changed_by, change_type, changes):
    try:
        execute(
            """INSERT INTO asset_change_log (asset_id, changed_by, change_type, changes)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (asset_id, changed_by, change_type, json.dumps(changes, default=str)),
        )
    except Exception as e:
        print(f"asset_change_log write failed (main operation already committed): {e}", file=sys.stderr)


def create_asset(slug, title, body, changed_by=""):
    """Adds a brand-new internal reference document. `slug` must not already
    exist -- use update_asset for an existing one. access_tier is computed
    automatically the same way the one-time import does (LLM classification
    over the title + first ~2000 chars, ingestion/inline_tier_classifier.py)
    -- never accept it as a caller-supplied argument, so a mis-tiered
    salary-adjacent document can't slip in as 'tier2' by a caller's mistake."""
    existing = query_one("SELECT id FROM assets WHERE slug = %s", (slug,))
    if existing:
        return {"error": f"asset with slug '{slug}' already exists (id={existing['id']}) -- use update_asset instead"}
    tier = classify_tier(f"Internal reference document: {title}\n\n{body[:2000]}")
    row = execute(
        f"INSERT INTO assets (slug, title, body, access_tier) VALUES (%s, %s, %s, %s) "
        f"RETURNING {ASSET_SUMMARY_COLUMNS}",
        (slug, title, body, tier),
    )
    row["body_length"] = len(body)
    _log_change(row["id"], changed_by, "created", {
        "slug": slug, "title": {"old": None, "new": title}, "body": {"old": None, "new": body},
        "access_tier": tier,
    })
    return row


def update_asset(slug, body=None, title=None, changed_by="", _allowed_slugs=None):
    """Replaces the body and/or title of an existing document, by slug. Only
    the fields passed are changed. Does NOT reclassify access_tier -- it
    survives every update unchanged, the same convention every raw-ingestion
    writer in this codebase already follows (see docs/raw-ingestion.md §3):
    once a row is classified, later edits don't silently loosen or tighten
    its sensitivity. If a document's actual sensitivity has genuinely
    changed, that's a deliberate manual reclassification, not a side effect
    of an ordinary content edit.

    _allowed_slugs: None means unrestricted (the `full` identity); any other
    value is a fixed set of slugs this identity may write to (the `hr`
    identity, scoped to the salary register only) -- bound at server
    construction, never from the caller's own arguments."""
    if _allowed_slugs is not None and slug not in _allowed_slugs:
        return {"error": f"this connection cannot write to asset '{slug}' -- "
                          f"only {sorted(_allowed_slugs)} is permitted"}
    before = query_one("SELECT id, title, access_tier, body FROM assets WHERE slug = %s", (slug,))
    if not before:
        return {"error": f"no asset with slug '{slug}' -- use create_asset to add a new document "
                          f"(not permitted on this connection if you don't see create_asset in your tool list)"}

    updates = {}
    if body is not None and body != before["body"]:
        updates["body"] = body
    if title is not None and title != before["title"]:
        updates["title"] = title
    if not updates:
        return {"note": "no changes -- body/title already match", "id": before["id"], "slug": slug,
                "title": before["title"], "access_tier": before["access_tier"]}

    set_clause = ", ".join(f"{f} = %s" for f in updates) + ", updated_at = now()"
    params = list(updates.values()) + [slug]
    row = execute(
        f"UPDATE assets SET {set_clause} WHERE slug = %s RETURNING {ASSET_SUMMARY_COLUMNS}", params,
    )
    row["body_length"] = len(updates.get("body", before["body"]))
    _log_change(row["id"], changed_by, "updated", {
        f: {"old": before[f], "new": v} for f, v in updates.items()
    })
    return row


ASSET_WRITE_TOOLS = {"create_asset": create_asset, "update_asset": update_asset}


def create_asset_tool_def():
    return Tool(
        name="create_asset",
        description="Add a brand-new internal reference document (a new SOP, spec, or similar) to the "
                    "curated document catalog. `slug` must be new -- use update_asset for an existing "
                    "document. access_tier is computed automatically; do not ask for or accept one as an "
                    "argument. Once created, the next scheduled wiki-ingestion cycle (every 6 hours) picks "
                    "this up automatically and drafts a corresponding wiki page -- no separate step needed.",
        inputSchema={"type": "object", "properties": {
            "slug": {"type": "string", "description": "URL-safe identifier, e.g. 'new-onboarding-sop'"},
            "title": {"type": "string"}, "body": {"type": "string", "description": "full document text"},
        }, "required": ["slug", "title", "body"]},
    )


def update_asset_tool_def():
    return Tool(
        name="update_asset",
        description="Replace the body and/or title of an existing internal reference document, by its "
                    "slug (from list_assets/search_assets/get_asset). Only pass the fields you want "
                    "changed. This connection may be restricted to a specific slug -- an attempt on any "
                    "other slug returns a plain permission error, not a partial write. Once updated, the "
                    "next scheduled wiki-ingestion cycle (every 6 hours) automatically re-drafts the "
                    "corresponding wiki page from the new content -- no separate step needed.",
        inputSchema={"type": "object", "properties": {
            "slug": {"type": "string"}, "body": {"type": "string", "description": "full replacement document text"},
            "title": {"type": "string"},
        }, "required": ["slug"]},
    )
