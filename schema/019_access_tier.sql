-- Access-tier system (Cruz): tier1 = CEO-only, tier2 = general (everyone).
-- Confirmed policy: salary/payout/incentive data is tier1 everywhere, regardless
-- of source. Within raj_gmail specifically, personal content (bank statements,
-- divorce, payouts to employees, salary) is tier1; everything else there is
-- tier2. Calls where rajat@eoxs.com participates alongside an unrecognized-
-- domain contact need the same per-item judgment (confirmed live: real personal
-- content -- a life-coaching call, a recurring call with a different personal
-- domain -- already exists in this source, not just raj_gmail). Every other
-- raw source (tickets, implementation_tasks, ron_gmail, remya_gmail,
-- support_zoho, calls with only known business-domain participants) is
-- structurally safe by policy and defaults straight to tier2.
--
-- Column default is deliberately NOT uniform across tables: email_threads and
-- call_transcripts default to tier1 (fail-closed -- these are the sources
-- where personal content has actually been confirmed to appear, so an
-- unclassified row must stay restricted until something actively clears it).
-- tickets/implementation_tasks/sales_orders default straight to tier2 -- not
-- a fail-open exception, but a source already judged structurally safe, the
-- same way raw ingestion itself distinguishes "needs LLM judgment" (spam
-- classification, gated) from "no judgment needed" (Fireflies/Fathom, ungated)
-- per HANDOFF.md's original design split.
CREATE TYPE access_tier AS ENUM ('tier1', 'tier2');

ALTER TABLE email_threads ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier1';
CREATE INDEX idx_email_threads_access_tier ON email_threads(access_tier);

ALTER TABLE call_transcripts ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier1';
CREATE INDEX idx_call_transcripts_access_tier ON call_transcripts(access_tier);

ALTER TABLE tickets ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier2';
CREATE INDEX idx_tickets_access_tier ON tickets(access_tier);

ALTER TABLE implementation_tasks ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier2';
CREATE INDEX idx_implementation_tasks_access_tier ON implementation_tasks(access_tier);

ALTER TABLE sales_orders ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier2';
CREATE INDEX idx_sales_orders_access_tier ON sales_orders(access_tier);

-- Wiki pages: tier is never independently judged -- always the MAX of every
-- cited source's tier (confirmed policy: a synthesized page can never leak a
-- tier1 source by citing it from an otherwise-tier2 page). Default tier1 here
-- is a pure safety net for a row that somehow never gets its tier computed;
-- the promotion path (wiki_ingestion/promote.py) always sets it explicitly.
ALTER TABLE wiki_pages ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier1';
CREATE INDEX idx_wiki_pages_access_tier ON wiki_pages(access_tier);

ALTER TABLE wiki_staging.wiki_pages ADD COLUMN access_tier access_tier NOT NULL DEFAULT 'tier1';
CREATE INDEX idx_wiki_staging_pages_access_tier ON wiki_staging.wiki_pages(access_tier);
