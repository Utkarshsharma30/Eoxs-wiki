"""Query-time redaction safety net -- the last line of defense before a
tool response reaches a non-full-clearance caller. Wired into
mcp_server/server.py's call_tool() handler for every identity except
FULL_CLEARANCE.

Why this exists, given every tool already SQL-filters by access_tier:
that filter is only as good as the tier CLASSIFICATION was at write time
(ingestion/inline_tier_classifier.py, wiki_ingestion/promote.py,
wiki_ingestion/tier_classifier.py) -- an LLM classification pass, and it
can be wrong. This is a second, independent check that only ever sees
content that ALREADY passed the first check (i.e. was already tagged
safe for this caller's clearance) -- it exists purely to catch cases
where that earlier tag was a mistake. It does not replace the tier
system; a response that already got the right tier never even reaches
here for the categories it's already excluded from.

Design, confirmed 2026-08:
- Runs on every tool call for every non-FULL-clearance identity -- no
  exceptions carved out for "safe-looking" tools, since a wrong exception
  is exactly the kind of mistake this exists to catch.
- Uses Sonnet, not Haiku -- deliberately a different, stronger model than
  whatever produced the original classification, so this isn't just
  re-running the same judgment that may have already been wrong.
- Redacts by having the model return the EXACT verbatim spans to remove,
  which are then stripped via plain string replacement in Python --
  never by having the model regenerate/rewrite the response, which risks
  silently altering text outside what it flagged. Everything not
  explicitly flagged is guaranteed byte-for-byte unchanged.
- Fails CLOSED: retries a few times on transient errors, and if it still
  can't get a verdict, blocks the response rather than passing through
  unverified content. Matches this codebase's existing convention for
  every other confidentiality-relevant check (inline_tier_classifier.py
  defaults to the MOST restrictive tier on error, not the least). A
  blocked response reads as an ordinary "no data" result, the same way a
  not-found and an access-denied result are already made indistinguishable
  elsewhere in this system -- a transient API hiccup should never LOOK
  like something is being hidden.
- Every time this actually redacts something, it's logged to
  mcp_redaction_log (schema/023_mcp_redaction_log.sql) so the underlying
  page/row's tier can be corrected at the source.

2026-08-10 addition: `check_and_redact` now also accepts `extra_categories`
-- restricted categories that apply regardless of clearance/access_tier,
for identities that need a content-based restriction with no matching DB
tier at all (the intern MCP: same GENERAL_CLEARANCE row-level access as
the general-employee MCP, deliberately not a new access_tier value, but
with monetary amounts always redacted on top). Wired in via
build_server()'s `extra_redact_categories` param -- see
_EXTRA_CATEGORY_DEFINITIONS below for the category text.

Same day, second use of the mechanism: the HR MCP gets
`non_payroll_monetary_amounts` -- HR's clearance (tier2_confidential +
tier2) legitimately needs to see payroll/salary/incentive figures, but has
no role-based reason to see client billing/pricing amounts, which happen
to sit in the same tier2_confidential bucket. Unlike `monetary_amounts`
(blanket, no exceptions -- intern), this category explicitly carves out
payroll amounts as NOT restricted, so the LLM pass has to make a real
distinction rather than a blanket strip -- see _strip_monetary_fields'
docstring for why it deliberately does NOT run for this category.

2026-08-10, same day, unrelated addition: employee activity/performance/
productivity monitoring data (e.g. Cattr) added to the tier2_confidential
DEFINITION itself (not a new extra_category -- this one maps exactly onto
the existing tier2_confidential/tier2 boundary, so no carve-out mechanism
is needed the way payroll-vs-billing needed one). Mirrored into
ingestion/tier_classifier.py and ingestion/inline_tier_classifier.py so
new content gets tagged correctly going forward; updating it here too
means this safety net starts catching it immediately for general/intern
callers even on rows that were already tagged tier2 before this change,
without needing a full historical re-classification pass.
"""
import json
import logging
import os

import anthropic

from ingestion.db import get_live_conn

logger = logging.getLogger("mcp_server.redaction")

_MODEL = "claude-sonnet-5"
_MAX_CHARS = 20000  # hard cap against pathologically large responses (e.g. get_client_profile)
_MAX_RETRIES = 3

_BLOCKED_RESULT = {"error": "This information is temporarily unavailable. Please try again."}

_TIER_DEFINITIONS = {
    "tier1": (
        'Rajat "Raj" Jain\'s own PERSONAL data -- his personal financial information '
        "(bank statements, personal investments, personal taxes), divorce/family/other "
        "personal-life matters involving Raj. NOT company business, even if Raj is a "
        "participant -- that belongs under tier2_confidential below, not here."
    ),
    "tier2_confidential": (
        "EOXS company-confidential business data -- salary/payroll/compensation/incentive/"
        "bonus figures for ANY employee, investor relations and fundraising, company "
        "financial statements or bank/accounting data, vendor payment terms or sensitive "
        "contract pricing, legal or compliance matters (that are not Raj's personal legal "
        "matters), employee activity/performance/productivity monitoring data (e.g. Cattr or "
        "similar tracking-tool output, individual performance metrics/scores, productivity "
        "reviews)."
    ),
}

# Not access_tier values -- these are content-based restrictions applied on
# top of a clearance's normal tier filtering, not in place of it. Passed in
# via check_and_redact's extra_categories, never derived from clearance.
_EXTRA_CATEGORY_DEFINITIONS = {
    "monetary_amounts": (
        "Any monetary or currency amount, in any currency or unit -- dollar/other-currency "
        "figures, prices, invoice or line-item totals, deal or contract sizes, discounts or "
        "markups, salary/payroll numbers, or any other numeric value that represents an "
        "amount of money. Applies regardless of which tier the surrounding content belongs "
        "to -- flag a dollar figure in ordinary tier2 business content the same as anywhere "
        "else. Does not apply to non-monetary numbers (dates, quantities/counts of items, "
        "percentages that aren't themselves a price, phone numbers, ids)."
    ),
    # HR: the one identity that legitimately needs payroll amounts (that's the
    # job) but has no business reason to see client billing/pricing -- e.g. a
    # get_client_profile response surfacing a client's monthly charge and
    # implementation cost (confirmed live, 2026-08-10: HR asked "how much are
    # we charging Brannon" and got a real dollar figure back -- correct per
    # today's tier rules, since client pricing is tier2_confidential and HR
    # clears tier2_confidential, but not something HR's role actually needs).
    "non_payroll_monetary_amounts": (
        "Any monetary or currency amount that is NOT an employee payroll, salary, "
        "compensation, incentive, or bonus figure -- in any currency or unit. Flag client "
        "billing/subscription/licensing charges, implementation or onboarding costs, invoice "
        "or line-item totals, deal or contract sizes, discounts or markups, vendor payments, "
        "investor/fundraising amounts, and any other numeric value that represents an amount "
        "of money. Do NOT flag an amount that is clearly an employee's payroll, salary, "
        "compensation, incentive, or bonus figure -- those must stay visible; this category "
        "exists specifically to keep payroll information visible while restricting every "
        "other kind of monetary amount. Does not apply to non-monetary numbers (dates, "
        "quantities/counts of items, percentages that aren't themselves a price, phone "
        "numbers, ids)."
    ),
}

_ALL_CATEGORY_DEFINITIONS = {**_TIER_DEFINITIONS, **_EXTRA_CATEGORY_DEFINITIONS}

# Checked directly against the live schema (2026-08-10): the only numeric
# currency-amount fields reachable by any tier2-visible tool, across every
# table GENERAL_CLEARANCE can query (sales_orders/order_lines/invoices/
# invoice_lines via get_invoice/search_invoices -- tickets/
# implementation_tasks/clients/call_transcripts/email_threads have none).
# discount_pct is deliberately excluded -- a percentage, not a currency
# amount (matches monetary_amounts' own definition above).
_MONETARY_FIELD_NAMES = {
    "amount_total", "unit_price", "subtotal", "amount_untaxed",
    "amount_tax", "amount_paid", "amount_due",
}

_PROMPT = """You are a security safety-net for Cruz, EOXS's internal knowledge base. A database
query already ran and was filtered for a specific access level -- your job is ONLY to double
-check that filtering caught everything, not to re-decide it from scratch.

The caller's access level does NOT allow seeing content in these categories:
{restricted_definitions}

Below is the exact tool response this caller is about to receive. Find any verbatim spans of
text that reveal information matching the restricted categories above. Be precise: quote each
span EXACTLY as it appears in the text below (character-for-character, so it can be found and
removed programmatically) -- do not paraphrase, summarize, or correct it. Only flag genuine
matches to the categories above; ordinary business content is NOT restricted just because Raj
or an employee is mentioned in it -- it has to actually match one of the categories.

Tool response:
{content}

Respond with ONLY a JSON array of exact substrings to redact, e.g. ["exact text one", "exact text two"].
If nothing needs redaction, respond with exactly: []"""


def _restricted_tiers(clearance):
    return [t for t in ("tier1", "tier2_confidential") if t not in clearance]


def _walk_strings(obj):
    """Yields every string leaf value in a JSON-like structure (arbitrarily
    nested dicts/lists). Used instead of comparing against a json.dumps()
    blob -- see the 2026-08 incident note in check_and_redact for why."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def _strip_monetary_fields(obj):
    """Deterministically blanks dict values under known currency-amount
    field names (_MONETARY_FIELD_NAMES) -- covers structured numeric
    fields (Decimal/int/float) that _walk_strings/the LLM text pass below
    would never see, since those only ever look at string leaves. A raw
    Decimal amount_total is never a string until json.dumps() serializes
    the final response, well after this runs, so without this the LLM
    pass silently has nothing to redact and the real figure ships intact
    -- confirmed live against a real search_invoices result before this
    was added (amount_total passed through completely unredacted).
    Runs unconditionally before the LLM pass whenever monetary_amounts is
    requested, independent of whether there's any string content to check
    at all -- the LLM pass remains the backstop for amounts mentioned in
    free text (email/call bodies, wiki prose), which this can't catch
    since it only knows fixed field names, not arbitrary prose.

    Deliberately NOT triggered by non_payroll_monetary_amounts (only the
    literal "monetary_amounts" check below does that) -- a field name
    alone can't tell payroll apart from client billing, and blanket-
    stripping every _MONETARY_FIELD_NAMES field regardless would strip
    payroll amounts too, exactly what that category exists to keep
    visible. The LLM pass is the only mechanism that can make that call,
    which is fine here since HR's tools don't expose the structured
    Odoo invoice fields this targets anyway (get_invoice/search_invoices
    were removed from every non-full identity in 2026-08)."""
    if isinstance(obj, dict):
        return {
            k: ("[restricted: amount]" if k in _MONETARY_FIELD_NAMES and v is not None else _strip_monetary_fields(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_strip_monetary_fields(v) for v in obj]
    return obj


def _replace_in_structure(obj, replacements):
    """Returns a new structure with every occurrence of each key in
    `replacements` (exact substring -> replacement text) replaced within
    every string leaf, recursively. Non-string leaves pass through
    unchanged. Operates directly on the real Python structure -- never
    round-trips through a serialized/re-parsed form, so there's no way
    for this step itself to produce something structurally broken."""
    if isinstance(obj, dict):
        return {k: _replace_in_structure(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_in_structure(v, replacements) for v in obj]
    if isinstance(obj, str):
        new = obj
        for span, repl in replacements.items():
            new = new.replace(span, repl)
        return new
    return obj


def _log_redaction(clearance_name, tool_name, snippets):
    try:
        conn = get_live_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mcp_redaction_log (clearance_name, tool_name, redacted_snippets) VALUES (%s, %s, %s)",
                    (clearance_name, tool_name, snippets),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("failed to log redaction event (redaction itself still applied): %s", e)


def _extract_text(resp):
    """resp.content isn't always [TextBlock] -- Sonnet can prepend a
    ThinkingBlock (extended thinking) before the actual text block, and
    resp.content[0] would then be the wrong block entirely (no .text
    attribute -> crash, caught live during testing). Find the real text
    block wherever it is instead of assuming position 0."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(f"no text block found in response content: {[getattr(b, 'type', type(b)) for b in resp.content]}")


def _parse_spans(raw_text):
    raw = raw_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    spans = json.loads(raw)
    if not isinstance(spans, list):
        raise ValueError(f"expected a JSON list, got {type(spans).__name__}")
    return spans


async def check_and_redact(result, clearance, tool_name, clearance_name="unknown", extra_categories=()):
    """extra_categories: additional restricted-category keys (see
    _EXTRA_CATEGORY_DEFINITIONS) to check regardless of clearance -- for a
    content-based restriction with no corresponding access_tier value.

    Returns the (possibly modified) result. Never raises -- on
    repeated failure returns a blocked/"unavailable" placeholder instead
    of the original (fail closed) rather than passing through anything
    unverified.

    2026-08 incident note: the first version of this function compared
    against json.dumps(result) -- a real, reproducible bug, caught live
    against actual production traffic, not in testing: JSON-escapes
    characters like an embedded double-quote (") into \\" in that
    serialized text, but the model naturally reproduces the real
    unescaped character when quoting a span back (as does a non-ASCII
    character like ₹, escaped to \\u20b9 in json.dumps output by
    default -- a related, earlier instance of the same class of bug).
    Either way the substring match silently found nothing and a real
    confidential incentive figure reached a general-clearance user
    untouched. Comparing against actual string field VALUES (via
    _walk_strings/_replace_in_structure) instead of a JSON serialization
    sidesteps the entire escaping-mismatch class of bug structurally,
    not just the two specific instances found so far."""
    if "monetary_amounts" in extra_categories:
        result = _strip_monetary_fields(result)

    restricted = _restricted_tiers(clearance) + list(extra_categories)
    if not restricted:
        return result  # this clearance already sees everything -- nothing to check

    strings = [s for s in _walk_strings(result) if s]
    if not strings:
        return result

    content_for_model = "\n---\n".join(strings)
    definitions = "\n".join(f"- {t}: {_ALL_CATEGORY_DEFINITIONS[t]}" for t in restricted)
    prompt = _PROMPT.format(restricted_definitions=definitions, content=content_for_model[:_MAX_CHARS])

    client = anthropic.AsyncAnthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    spans = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.messages.create(
                model=_MODEL, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            spans = _parse_spans(_extract_text(resp))
            break
        except Exception as e:
            logger.warning("redaction check attempt %d/%d failed for tool=%s: %s", attempt, _MAX_RETRIES, tool_name, e)

    if spans is None:
        logger.error("redaction check exhausted retries for tool=%s -- failing closed, blocking response", tool_name)
        return _BLOCKED_RESULT

    # Validate each flagged span actually occurs somewhere in the real
    # content (not hallucinated) before trusting it.
    spans = [s for s in spans if isinstance(s, str) and s and any(s in field for field in strings)]
    if not spans:
        return result

    replacements = {s: "[restricted]" for s in spans}
    redacted_result = _replace_in_structure(result, replacements)

    _log_redaction(clearance_name, tool_name, spans)
    return redacted_result
