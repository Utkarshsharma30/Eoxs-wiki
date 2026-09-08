-- wiki_citations had no timestamp of its own -- every other citation-
-- adjacent table (wiki_pages, email_threads, assets, ...) has one, but a
-- citation row itself carried no signal for "when was this citation last
-- touched/resolved" (promote.py's initial write, or a later re-resolution
-- by citation_resolver.py/citation_llm_resolver.py). Needed so a caller can
-- tell a freshly-resolved citation from one that's sat at 'unresolved'
-- since the row was first written, and as a real per-citation timestamp for
-- anything that wants one (rather than approximating via a join to a raw
-- source table, or wiki_pages.updated_date, which isn't the same thing --
-- see mcp_server/server.py's _CITED_EVENT_DATE_SQL for why those two are
-- deliberately kept separate).
ALTER TABLE wiki_citations
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
