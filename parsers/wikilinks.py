"""Extracts [[Page Title]] and [[Page Title|display text]] wikilinks from body text."""
import re

# Matches [[Title]] or [[Title|display]] — Obsidian wikilink syntax.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]")

CONTEXT_RADIUS = 80  # chars of surrounding text to capture on each side


def extract_wikilinks(body):
    """Returns a list of dicts: {to_title_raw, display_text, context_snippet}."""
    links = []
    for m in WIKILINK_RE.finditer(body):
        title = m.group(1).strip()
        display = m.group(2).strip() if m.group(2) else None
        start = max(0, m.start() - CONTEXT_RADIUS)
        end = min(len(body), m.end() + CONTEXT_RADIUS)
        snippet = body[start:end].replace("\n", " ").strip()
        links.append({
            "to_title_raw": title,
            "display_text": display,
            "context_snippet": snippet,
        })
    return links


CONTRADICTION_RE = re.compile(r"^\s*>\s*⚠️\s*CONTRADICTION:\s*(.+)$", re.MULTILINE)
UNVERIFIED_RE = re.compile(r"^\s*>\s*🔍\s*UNVERIFIED:\s*(.+)$", re.MULTILINE)


def extract_flags(body):
    """Returns a list of dicts: {flag_type, text}."""
    flags = []
    for m in CONTRADICTION_RE.finditer(body):
        flags.append({"flag_type": "contradiction", "text": m.group(1).strip()})
    for m in UNVERIFIED_RE.finditer(body):
        flags.append({"flag_type": "unverified", "text": m.group(1).strip()})
    return flags
