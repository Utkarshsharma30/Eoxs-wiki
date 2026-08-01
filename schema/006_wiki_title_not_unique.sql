-- Corrects idx_wiki_pages_title: the vault has pre-existing duplicate titles
-- at different paths (e.g. two files both titled "Jamie Hansen"), a real
-- ambiguity in Obsidian's own wikilink resolution too — not something to
-- mask with a DB constraint. See schema/004_wiki.sql comment.
DROP INDEX IF EXISTS idx_wiki_pages_title;
CREATE INDEX idx_wiki_pages_title ON wiki_pages(title);
