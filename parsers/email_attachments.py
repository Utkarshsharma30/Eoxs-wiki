"""Extracts attachment bullet lines from an email message block.

Format: "- filename (528 B) (inline tracking image, not stored)"
        "- report.pdf (1.2 MB)"
"""
import re

ATTACHMENT_LINE_RE = re.compile(
    r"^-\s+(?P<filename>.+?)\s+\((?P<size>[\d.]+\s*[A-Za-z]+)\)(?:\s*\((?P<note>[^()]*)\))?\s*$",
    re.MULTILINE,
)

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def parse_size(size_str):
    if not size_str:
        return None
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", size_str)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).upper()
    multiplier = SIZE_UNITS.get(unit)
    return int(value * multiplier) if multiplier else None


def extract_attachments(message_block):
    """Returns a list of dicts: {filename, size_bytes, note} found after an
    '**Attachments:**' marker in this message block."""
    if "**Attachments:**" not in message_block:
        return []
    _, _, after = message_block.partition("**Attachments:**")
    results = []
    for m in ATTACHMENT_LINE_RE.finditer(after):
        results.append({
            "filename": m.group("filename").strip(),
            "size_bytes": parse_size(m.group("size")),
            "note": m.group("note").strip() if m.group("note") else None,
        })
    return results
