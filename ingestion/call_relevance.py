"""Call-transcript noise filter -- ports is_call_relevant() from the Render
pipeline's tools/fireflies_to_obsidian/fireflies_to_obsidian.py verbatim in
behavior: Haiku, judges actual transcript content (not keywords), discards
only empty/near-empty recordings or clearly personal non-work activity
(e.g. a yoga class) with no substantive conversation. Keeps every genuine
call regardless of topic or company mentioned. Defaults to keep on
ambiguity or any API error (fail-open).

Distinct from spam_filter.is_eoxs_relevant (which is Gmail/Zoho-only and
detects marketing/newsletter content, not applicable to calls). The
existing HANDOFF.md next-steps note says Fireflies/Fathom get "no spam
filtering" -- true for is_eoxs_relevant, but the real reference pipeline
DOES run this separate noise filter before writing a call, and the DB
schema's current historical call_transcripts rows were all produced with
it applied. Ported to keep behavioral parity; exposed via the same
--no-classify convention as the other fetchers so it can be disabled.
"""
import logging
import os

import anthropic

logger = logging.getLogger("ingestion.call_relevance")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_EXCERPT_CHARS = 1500

_PROMPT = """You are filtering a call-recording archive to remove empty or trivial noise, \
keeping every genuine call — personal or professional, any topic, any company.

DISCARD only if: the transcript has no substantive conversation at all (e.g. a call \
that never really started, someone joining and immediately leaving, a stray link/test \
recording), or it is clearly a personal non-work activity with no business content \
(e.g. a yoga class, a workout session, background noise with no discussion).

KEEP everything else, including: any real conversation between people discussing any \
topic, business or personal, professional or informal. Do NOT judge relevance by whether \
a specific company or project is mentioned — a genuine client call, vendor call, or \
internal discussion must be kept even if it never says a company name.

When uncertain, KEEP it — this filter should only remove clear noise, not judge importance.

{content}

Answer with exactly one word: keep or discard"""


def is_call_relevant(meeting_title, duration_human, overview, key_topics, action_items,
                      transcript_excerpt, client=None):
    """Returns True (keep) or False (discard). Never raises -- any error
    defaults to True, matching the original pipeline's fail-open behavior.
    An entirely empty call (no overview/topics/actions/excerpt) is treated
    as noise without spending an API call."""
    if not overview and not key_topics and not action_items and not transcript_excerpt:
        return False

    parts = [f"Meeting title: {meeting_title or 'untitled'}", f"Duration: {duration_human or ''}"]
    if overview:
        parts.append(f"Overview: {overview}")
    if key_topics:
        parts.append("Key topics: " + ", ".join(str(k) for k in key_topics[:10]))
    if action_items:
        parts.append("Action items: " + "; ".join(str(a) for a in action_items[:5]))
    if transcript_excerpt:
        parts.append("Transcript excerpt: " + transcript_excerpt[:_MAX_EXCERPT_CHARS])

    try:
        client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": _PROMPT.format(content="\n".join(parts))}],
        )
        return resp.content[0].text.strip().lower().startswith("k")
    except Exception as e:
        logger.warning("call relevance classifier failed, defaulting to keep: %s", e)
        return True
