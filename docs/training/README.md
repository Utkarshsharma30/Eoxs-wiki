# Cruz Training Video Program

Onboarding video series for new interns — zero prior knowledge in, working
understanding of the system out. Two tiers:

- **Tier 1 — Orientation.** Everyone watches these six, in order, regardless
  of role. Building block for everything else. Scripts: `tier1-orientation-scripts.md`.
- **Tier 2 — Role-specific.** Only the videos matching what someone's actually
  assigned to (ingestion / auditing / Linear review / server health checks).
  Template + placeholders: `tier2-role-specific.md`.

## How to use these docs while recording

Each Tier 1 script is a **bullet-point talking outline, not a word-for-word
script** — read the beats, speak them naturally, re-record a section if it
comes out clunky rather than trying to nail one perfect take of the whole
video. Every `[SHOW: ...]` marker tells you exactly what should be on screen
at that moment — a diagram, a terminal command, a specific query, a live
Claude chat. Run the "before you record" checklist at the top of each script
first so you're not switching windows mid-take.

Diagrams referenced in the scripts are Mermaid blocks embedded directly in
`tier1-orientation-scripts.md` (GitHub renders these natively) and are also
available as a full-screen presentation page for recording — ask for the
link if you don't have it, or regenerate it from the same diagram source.

## Why these docs live in the repo

When the system changes, the code diff tells you which video is now stale —
update the script here first, then re-record just that one video. Keeps the
training material from silently drifting out of date the way a one-off
recording always does.

## Target audience

Someone with **zero prior context** on EOXS's internal tools. Don't assume
they know what Odoo, Fireflies, or an MCP connector is — the first time
any term appears, it gets defined in the moment it's shown, not assumed.
