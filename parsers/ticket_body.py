"""Parses a support ticket's body into (description, events).

Body shape (after frontmatter):
    # T##### — subject
    | Field | Value |   <- recap table, display-only, not parsed here
    ...
    ## Description
    <freetext>
    ## Activity Thread
    <!-- odoo_msg_id:NNNNN -->
    ### [Log Note] Author — YYYY-MM-DD HH:MM
    <body>
    ---
    <!-- odoo_msg_id:NNNNN -->
    **YYYY-MM-DD HH:MM — Author**
    <body, e.g. '**Ownership:** A -> B'>
    ---
"""
import re

DESCRIPTION_RE = re.compile(r"## Description\s*\n(.*?)(?=\n## |\Z)", re.DOTALL)
ACTIVITY_RE = re.compile(r"## Activity Thread\s*\n(.*)\Z", re.DOTALL)

ODOO_MSG_ID_RE = re.compile(r"<!--\s*odoo_msg_id:(\d+)\s*-->")
LOG_NOTE_HEADER_RE = re.compile(r"^### \[Log Note\] (.+?) — (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$")
CHANGE_HEADER_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (.+?)\*\*\s*$")


def extract_description(body):
    m = DESCRIPTION_RE.search(body)
    return m.group(1).strip() if m else None


def extract_events(body):
    """Returns a list of dicts: {odoo_msg_id, event_type, author, event_time_str, body, event_order}."""
    m = ACTIVITY_RE.search(body)
    if not m:
        return []
    activity = m.group(1)

    blocks = [b.strip("\n") for b in activity.split("\n---\n")]
    events = []
    order = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        msg_id = None
        msg_id_match = ODOO_MSG_ID_RE.search(block)
        if msg_id_match:
            msg_id = int(msg_id_match.group(1))
            block = ODOO_MSG_ID_RE.sub("", block, count=1).strip()

        lines = block.split("\n", 1)
        header = lines[0].strip()
        rest = lines[1].strip() if len(lines) > 1 else ""

        log_match = LOG_NOTE_HEADER_RE.match(header)
        change_match = CHANGE_HEADER_RE.match(header)

        if log_match:
            author, time_str = log_match.group(1), log_match.group(2)
            event_type = "log_note"
        elif change_match:
            time_str, author = change_match.group(1), change_match.group(2)
            event_type = "ownership_change" if "Ownership" in rest else "other"
        else:
            # Doesn't match either header shape — keep as an unstructured event
            # rather than silently dropping content.
            author, time_str, event_type = None, None, "other"
            rest = block

        events.append({
            "odoo_msg_id": msg_id,
            "event_type": event_type,
            "author": author,
            "event_time_str": time_str,
            "body": rest,
            "event_order": order,
        })
        order += 1

    return events
