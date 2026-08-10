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
        "matters)."
    ),
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


async def check_and_redact(result, clearance, tool_name, clearance_name="unknown"):
    """Returns the (possibly modified) result. Never raises -- on
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
    restricted = _restricted_tiers(clearance)
    if not restricted:
        return result  # this clearance already sees everything -- nothing to check

    strings = [s for s in _walk_strings(result) if s]
    if not strings:
        return result

    content_for_model = "\n---\n".join(strings)
    definitions = "\n".join(f"- {t}: {_TIER_DEFINITIONS[t]}" for t in restricted)
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
