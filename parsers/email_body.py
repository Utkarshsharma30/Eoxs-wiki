"""Splits an email thread body into per-message blocks.

Body format (after the '# <subject>' line):
    ## Message N - YYYY-MM-DD HH:MM <TZ>

    **From:** Name <email>

    <message text, possibly with '**Attachments:**' bullet list>

<TZ> was assumed to always be UTC (the regex hardcoded the literal string)
until this was found broken live: 224 of 30,302 loaded email_threads had
message_count>0 but zero actual email_messages rows, because their message
headers say IST (India Standard Time, generated_at in those files' own
frontmatter confirms it -- some upstream process ran in IST for a subset
of files), and the old regex only ever matched "UTC" -- any other label
made every message in that file invisible to the parser, not just
mis-timestamped. Now the label is captured and returned, and the caller
(loaders/load_emails.py) is responsible for the actual UTC conversion.
"""
import re

MESSAGE_HEADER_RE = re.compile(
    r"^## Message (\d+) - (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ([A-Z]{2,5})\s*$", re.MULTILINE
)
FROM_LINE_RE = re.compile(r"^\*\*From:\*\*\s*(.+)$", re.MULTILINE)


def split_messages(body):
    """Returns a list of dicts: {message_index, message_date_str, message_tz, from_addr, body}."""
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
            "message_tz": m.group(3),
            "from_addr": from_addr,
            "body": block.strip(),
        })
    return messages
