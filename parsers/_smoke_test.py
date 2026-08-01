"""Quick manual check of the parsers against real vault files. Not a pytest suite."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from parsers.frontmatter import parse_frontmatter
from parsers.wikilinks import extract_wikilinks, extract_flags
from parsers.email_body import split_messages

VAULT_ROOT = Path(os.environ["VAULT_ROOT"])

email_file = VAULT_ROOT / "raw/emails/raj_gmail/2020-08/2020-08-10-this-may-resonate.md"
text = email_file.read_text(encoding="utf-8")
meta, body = parse_frontmatter(text)
print("=== EMAIL FRONTMATTER ===")
print({k: meta[k] for k in ("gmail_thread_id", "subject", "message_count", "source_account")})
msgs = split_messages(body)
print(f"parsed {len(msgs)} messages, expected {meta['message_count']}")
assert len(msgs) == meta["message_count"], "message count mismatch!"
print("first message:", msgs[0]["message_index"], msgs[0]["message_date_str"], msgs[0]["from_addr"])

wiki_file = VAULT_ROOT / "wiki/concepts/AI Innovation Team Wind-Down.md"
text2 = wiki_file.read_text(encoding="utf-8")
meta2, body2 = parse_frontmatter(text2)
print("\n=== WIKI FRONTMATTER ===")
print(meta2)
links = extract_wikilinks(body2)
print(f"\nfound {len(links)} wikilinks:")
for l in links:
    print(" -", l["to_title_raw"], "| display:", l["display_text"])
flags = extract_flags(body2)
print(f"\nfound {len(flags)} flags:", flags)

print("\nOK - smoke test passed")
