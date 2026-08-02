-- Phase 5 prep: promoting a wiki_staging draft to public.wiki_pages has no
-- real file backing it (DB-native from day one, per schema/017's comment on
-- wiki_staging.wiki_pages) -- but source_file_path/source_file_mtime were
-- NOT NULL, a leftover from loaders/load_wiki.py's one-time git-vault
-- migration pass (source_file_path was that pass's natural key). public.
-- wiki_pages has 0 rows at the time of this migration (that migration was
-- never actually run against this DB), so nothing existing depends on the
-- NOT NULL constraint -- this simply makes room for DB-native promoted rows
-- to leave both columns NULL. The UNIQUE index on source_file_path is left
-- in place: Postgres treats NULLs as distinct for uniqueness, so any number
-- of file-less rows can coexist with it.
ALTER TABLE wiki_pages ALTER COLUMN source_file_path DROP NOT NULL;
ALTER TABLE wiki_pages ALTER COLUMN source_file_mtime DROP NOT NULL;

-- Phase 5 review sweep: records why a draft was marked 'reviewed' or
-- 'rejected' (status transitions defined in schema/017's comment on this
-- table) -- surfaced to whoever approves promotion, since for now that's a
-- human decision, not an automatic one.
ALTER TABLE wiki_staging.wiki_pages ADD COLUMN review_notes TEXT;
