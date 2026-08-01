"""Splits a call transcript body into per-speaker turns.
Format: '**Speaker Name:** text...' one turn per paragraph-ish block."""
import re

SPEAKER_LINE_RE = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$")


def extract_transcript(body):
    """body: everything after '## Transcript'. Returns the raw transcript text."""
    idx = body.find("## Transcript")
    if idx == -1:
        return body.strip()
    return body[idx + len("## Transcript"):].strip()


def split_segments(transcript_text):
    """Returns a list of dicts: {segment_order, speaker, text}."""
    segments = []
    current_speaker = None
    current_lines = []
    order = 0

    def flush():
        nonlocal order
        if current_speaker is not None and current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                segments.append({"segment_order": order, "speaker": current_speaker, "text": text})
                order += 1

    for line in transcript_text.split("\n"):
        m = SPEAKER_LINE_RE.match(line.strip())
        if m:
            flush()
            current_speaker = m.group(1).strip()
            current_lines = [m.group(2)] if m.group(2) else []
        else:
            current_lines.append(line)
    flush()

    return segments
