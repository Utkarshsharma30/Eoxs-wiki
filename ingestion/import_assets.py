"""One-time backfill: imports the curated internal reference documents
(SOPs, company overview, ICP, salary register, etc.) that raj-wiki-vault's
older file-based pipeline kept in raw/assets/*.md into this database's new
`assets` table (schema/031_assets.sql). These were never part of this
database's raw layer even though the WIKI PAGES synthesized from them were
migrated in already (source_file_path on those wiki_pages rows still
literally reads 'wiki/sources/assets/<title>.md') -- found 2026-08-12 while
investigating why 15 wiki_citations rows sat permanently unresolved.

Not an ongoing fetcher -- these are manually-curated documents, not an
external API feed. Re-run to pick up manual edits to the source files (safe:
ON CONFLICT upsert), but nothing calls this automatically.

Usage: python -m ingestion.import_assets [--dry-run]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import get_live_conn, get_staging_conn
from ingestion.inline_tier_classifier import classify_tier

RAJ_WIKI_VAULT_ASSETS = Path("/home/deploy/raj-wiki-vault/raw/assets")

# filename -> slug. Most are a direct slugify of the filename, but 3 aren't
# (icp.md/sop.md/TECHNICAL.md used deliberately more descriptive slugs when
# originally wiki-ingested, verified by matching each file's actual content
# against wiki_pages.sources_raw before building this table -- see the
# investigation this migration came from, not guessable from the filename
# alone).
FILENAME_TO_SLUG = {
    "AI_Joe_Features.md": "ai-joe-features",
    "AI_Joe_Project_Overview.md": "ai-joe-project-overview",
    "Code_Review_and_Technical_QA_SOP.md": "code-review-technical-qa-sop",
    "Development_Standards_and_Code_Structure_SOP.md": "development-standards-code-structure-sop",
    "EOXS_Client_Implementation_and_Go_Live_SOP.md": "eoxs-client-implementation-go-live-sop",
    "EOXS_Company_Overview.md": "eoxs-company-overview",
    "EOXS_News_SOP.md": "eoxs-news-sop",
    "EOXSplore_SOP.md": "eoxsplore-sop",
    "EOXS_Product_Features.md": "eoxs-product-features",
    "EOXS_SALARY_DETAILS.md": "eoxs-salary-details",
    "GitLab_Branching_and_Deployment_SOP.md": "gitlab-branching-deployment-sop",
    "icp.md": "eoxs-icp",
    "Product_Demo_Video_SOP.md": "product-demo-video-sop",
    "sop.md": "eoxs-sop",
    "TECHNICAL.md": "eoxs-technical-sales-coach",
}


def _title_from_body(body, fallback):
    """First non-empty line, minus a leading markdown '# ' if present --
    not every source file uses a top-level heading for its title (one has
    plain text on line 1 with a '#' heading only appearing later, deep in
    a subsection -- searching for the first '# '-prefixed line anywhere in
    the document picked that up by mistake instead)."""
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line[2:].strip() if line.startswith("# ") else line
    return fallback


def _upsert(conn, slug, title, body, source_file_path, access_tier):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO assets (slug, title, body, source_file_path, access_tier)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                source_file_path = EXCLUDED.source_file_path,
                access_tier = EXCLUDED.access_tier,
                updated_at = now()
            RETURNING id
            """,
            (slug, title, body, source_file_path, access_tier),
        )
        asset_id = cur.fetchone()["id"]
    conn.commit()
    return asset_id


def import_all(dry_run=False):
    results = {}
    for filename, slug in FILENAME_TO_SLUG.items():
        path = RAJ_WIKI_VAULT_ASSETS / filename
        body = path.read_text(encoding="utf-8")
        title = _title_from_body(body, fallback=slug)
        tier = classify_tier(f"Internal reference document: {title}\n\n{body[:2000]}")

        if dry_run:
            print(f"[dry-run] would import {slug!r} ({title!r}, {tier}, {len(body)} chars)")
            results[slug] = {"title": title, "tier": tier, "chars": len(body)}
            continue

        live_conn = get_live_conn()
        try:
            asset_id = _upsert(live_conn, slug, title, body, f"raw/assets/{filename}", tier)
        finally:
            live_conn.close()

        try:
            staging_conn = get_staging_conn()
            try:
                _upsert(staging_conn, slug, title, body, f"raw/assets/{filename}", tier)
            finally:
                staging_conn.close()
        except Exception as e:
            print(f"staging write failed for {slug!r} (live succeeded, continuing): {e}")

        print(f"imported {slug!r} -> assets.id={asset_id} ({tier})")
        results[slug] = {"id": asset_id, "title": title, "tier": tier}
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    import_all(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
