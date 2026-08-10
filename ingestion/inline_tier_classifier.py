"""Synchronous, per-item access-tier classifier for new rows at write
time -- inline counterpart to ingestion/tier_classifier.py's bulk backlog
job, same 3-way prompt/criteria, same CLASSIFIER_ANTHROPIC_API_KEY. Mirrors
spam_filter.py's is_eoxs_relevant() call pattern: one sync API call per
new row, fail-closed (defaults to the most restrictive level, 'tier1') on
any error -- never raises.

Only called for a row's FIRST write (INSERT), not on every subsequent
update to the same row (matching every write_*.py's existing convention
of excluding access_tier from its ON CONFLICT DO UPDATE SET) -- a thread/
call/ticket/task's tier is computed once and preserved, not re-evaluated
as new messages/events get appended. Known limitation, not solved here:
content added after the initial classification (e.g. a salary figure
mentioned in message #5 of an already-tier2 thread) won't retroactively
change the tier -- would need the periodic bulk job
(ingestion/tier_classifier.py) re-run occasionally to catch drift.
"""
import logging
import os

import anthropic

logger = logging.getLogger("ingestion.inline_tier_classifier")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_CONTEXT_CHARS = 2000

_PROMPT = """You are classifying an item in Cruz, EOXS's internal knowledge base, for access control.

Decide exactly one of three levels:

TIER1 -- Rajat "Raj" Jain's own PERSONAL data only:
- Raj's personal financial information (bank statements, personal investments, personal taxes)
- Divorce, family, or other personal/private life matters involving Raj
- Any other content that is personal to Raj rather than EOXS company business
Do NOT use TIER1 for company business, even if Raj is the sender/participant and even if it's
company-sensitive -- that belongs in TIER2_CONFIDENTIAL below.

TIER2_CONFIDENTIAL -- EOXS company-confidential business data:
- Salary, payroll, compensation, incentive, or bonus figures for ANY employee (including Raj's own)
- Investor relations and fundraising
- Company financial statements or bank/accounting data
- Vendor payment terms or contracts with sensitive pricing
- Legal or compliance matters (that are NOT Raj's personal legal matters)
- Employee activity, performance, or productivity monitoring data -- e.g. Cattr or similar
  tracking-tool output, individual performance metrics/scores, productivity reviews

TIER2 -- General, visible company-wide: ordinary business correspondence, client implementation/
support work, product/ops, sales orders, scheduling, recruiting (non-compensation details), and
other everyday professional content -- the default for anything not clearly TIER1 or
TIER2_CONFIDENTIAL.

When genuinely uncertain between two adjacent levels, prefer the more restrictive one (fail closed):
TIER2_CONFIDENTIAL over TIER2, or TIER1 over TIER2_CONFIDENTIAL if it's plausibly Raj's personal
matter rather than company business.

{context}

Answer with exactly one word: TIER1, TIER2_CONFIDENTIAL, or TIER2."""


def classify_tier(context, client=None):
    """context: a short text description of the item (same shape as
    ingestion/tier_classifier.py's per-table context strings). Returns
    'tier1' | 'tier2_confidential' | 'tier2'. Never raises -- any error
    defaults to 'tier1' (fail closed), matching the bulk job's behavior."""
    try:
        client = client or anthropic.Anthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=_MODEL, max_tokens=8,
            messages=[{"role": "user", "content": _PROMPT.format(context=context[:_MAX_CONTEXT_CHARS])}],
        )
        answer = resp.content[0].text.strip().upper()
        if "TIER2_CONFIDENTIAL" in answer:
            return "tier2_confidential"
        if "TIER1" in answer:
            return "tier1"
        return "tier2"
    except Exception as e:
        logger.warning("inline tier classifier failed, defaulting to tier1: %s", e)
        return "tier1"
