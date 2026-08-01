"""Splits a markdown file with a YAML frontmatter block into (metadata dict, body str)."""
import yaml

FRONTMATTER_DELIM = "---"


def parse_frontmatter(text):
    """text: full file contents. Returns (dict, body_str).
    Returns ({}, text) if no frontmatter block is present."""
    if not text.startswith(FRONTMATTER_DELIM):
        return {}, text

    lines = text.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    yaml_block = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")

    metadata = yaml.safe_load(yaml_block) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return metadata, body
