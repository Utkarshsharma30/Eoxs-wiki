"""Splits an email thread body into per-message blocks.

Body format (after the '# <subject>' line):
    ## Message N - YYYY-MM-DD HH:MM UTC

    **From:** Name <email>

    <message text, possibly with '**Attachments:**' bullet list>
"""
import re

MESSAGE_HEADER_RE = re.compile(
    r"^## Message (\d+) - (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\s*$", re.MULTILINE
)
FROM_LINE_RE = re.compile(r"^\*\*From:\*\*\s*(.+)$", re.MULTILINE)


def split_messages(body):
    """Returns a list of dicts: {message_index, message_date_str, from_addr, body}."""
    headers = list(MESSAGE_HEADER_RE.finditer(body))
    messages = []
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        block = body[start:end].strip("\n")

        from_match = FROM_LINE_RE.search(block)
        from_addr = from_match.group(1).strip() if from_match else None

        messages.append({
            "message_index": int(m.group(1)),
            "message_date_str": m.group(2),
            "from_addr": from_addr,
            "body": block.strip(),
        })
    return messages
