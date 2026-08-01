"""Email relevance classifier -- ports is_eoxs_relevant() from the Render
pipeline's tools/email_to_obsidian/email_to_obsidian.py verbatim in
behavior: Haiku, max_tokens=5, discards only spam/newsletters/cold-outreach/
automated receipts, keeps everything else including personal correspondence,
defaults to keep on ambiguity or API error. Gmail/Zoho only -- never applied
to Fireflies/Fathom/Odoo, matching the original pipeline.
"""
import logging
import os

import anthropic

logger = logging.getLogger("ingestion.spam_filter")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_BODY_CHARS = 4000

_PROMPT = """You are filtering an email archive. Decide if this email should be KEPT or DISCARDED.

DISCARD only if it is clearly: marketing/promotional email, a newsletter, cold sales outreach, \
a social media notification, an automated receipt/alert with no substantive content, or phishing/spam.

KEEP everything else, including personal correspondence and any professional email with real content. \
When uncertain, KEEP.

Subject: {subject}
Body (truncated):
{body}

Answer with exactly one word: KEEP or DISCARD."""


def is_eoxs_relevant(subject, body, client=None):
    """Returns True (keep) or False (discard). Never raises -- any error
    defaults to True, matching the original pipeline's fail-open behavior."""
    try:
        client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(subject=subject or "", body=(body or "")[:_MAX_BODY_CHARS]),
            }],
        )
        answer = resp.content[0].text.strip().upper()
        return "DISCARD" not in answer
    except Exception as e:
        logger.warning("spam classifier failed, defaulting to keep: %s", e)
        return True
