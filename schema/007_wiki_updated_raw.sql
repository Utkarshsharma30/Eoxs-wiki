-- Some pages' 'updated:' frontmatter is a rich annotation ("2026-07-30
-- (catch-up sync — ...)"), not a bare date. updated_date keeps just the
-- leading date for range queries; updated_raw preserves the full original
-- value so none of that annotation content is lost.
ALTER TABLE wiki_pages ADD COLUMN updated_raw TEXT;
