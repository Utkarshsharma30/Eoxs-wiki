# Cruz — High-Level Architecture

*A plain-language guide to how Cruz works, for anyone at EOXS — no engineering background required.*

---

## 1. What is Cruz, in one paragraph?

Cruz is EOXS's internal "second brain." It automatically collects everything that happens across the company — every email, every sales call, every support ticket, every client implementation update, every invoice — and turns it into a searchable knowledge base that anyone can ask questions of, in plain English, through a chat interface. Different people see different amounts of detail depending on who they are: general staff see general information, HR sees HR-relevant information, and Raj sees everything, including anything personal to him. Everything described in this document is real, built, and running today — not a proposal or a roadmap.

## 2. Why does it exist?

Institutional knowledge at a growing company tends to live in people's heads, in scattered email threads, in call recordings nobody re-listens to, and in support tickets nobody re-reads after they're closed. Cruz's job is to make sure that knowledge doesn't evaporate — every conversation, every deal, every client relationship detail gets captured once, automatically, and becomes something anyone can search and ask about later, instead of depending on someone remembering it or digging through their inbox.

## 3. The five-stage journey of a piece of information

Think of any single fact — say, a client mentions a pricing concern on a sales call — and follow it through the system:

**Stage 1 — It gets collected automatically.**
Cruz is connected to nine live sources: three individual Gmail inboxes, one shared support inbox (Zoho), two call-recording tools (Fireflies and Fathom), EOXS's own internal support-ticket system, EOXS's own sales-order and invoicing records, and the implementation/onboarding tracking boards of eight different steel-industry clients. Every one of these is checked automatically — some the moment something new happens (real-time), and all of them again on a fixed 2-hour safety-net schedule, so nothing is ever missed for long.

**Stage 2 — It gets filtered and labeled for sensitivity.**
Before anything is stored for real, a first AI pass throws out pure noise — marketing emails, spam, empty or irrelevant call recordings — while keeping every piece of real business content. A second AI pass then reads the actual content and decides how sensitive it is, sorting it into one of three levels: general company information, company-confidential information (like salary or financial details), or Raj's own personal information. This system is deliberately cautious: if it's ever unsure, it defaults to the *more* restrictive label, not the less restrictive one — better to over-protect than to accidentally expose something sensitive.

**Stage 3 — It's stored in one central database.**
Everything lands in a single, well-organized database, tagged with which client it relates to and who's allowed to see it. This is the permanent, structured record of everything Cruz has collected — nothing here is thrown away, and it's what all later stages are built on top of.

**Stage 4 — It gets turned into readable knowledge.**
Every six hours, an AI process reads through everything new since the last pass and writes it up into short, readable "wiki" pages — one page per topic, person, company, or theme, cross-linked to related pages, each one citing exactly which original emails/calls/tickets it came from. A second, independent AI double-checks every new page's citations against the real source material before it's allowed to move forward. Nothing gets published automatically, though — a human still has to give the final go-ahead before a page goes live, so there's always a person in the loop before anything reaches end users.

**Stage 5 — It's ready to be asked about.**
Once live, that knowledge is exactly what Cruz's chat interface searches through to answer a question. Ask "what's the status of the Discount Pipe & Steel rollout?" or "summarize my last call with a client" and Cruz pulls from these AI-written pages (and can dig into the original raw records too) to answer — always respecting the same sensitivity labels from Stage 2, so a general-access user and Raj can ask the identical question and get appropriately different answers.

Alongside all of this, a separate tracking board (in Linear, a project-management tool) automatically gets a running log of what the automation is doing every step of the way — so progress can be checked at a glance without anyone needing direct database access.

## 4. The three levels of access, explained simply

Cruz doesn't have separate copies of its knowledge for different people — it has **one** knowledge base with a sensitivity label on every single piece of it, checked automatically every time anyone asks a question:

- **General** — the default level. Ordinary business information: client updates, ticket statuses, general correspondence. Most people at EOXS operate at this level.
- **Company-Confidential** — anything sensitive to the business as a whole: salaries, financials, legal matters, vendor pricing, investor conversations. Reserved for the people who need it (e.g., HR).
- **Raj-Personal** — anything personal to Raj specifically, never shown to anyone else, including under the Company-Confidential level.

Access is tied to *how* someone connects to Cruz, not something they can adjust themselves — there's no toggle or setting anyone can flip to see more than they're supposed to. The restriction is enforced centrally, once, inside the database layer itself, so it can't be bypassed by asking a clever question or approaching it from a different angle.

## 5. Who owns what

- **Cruz's chat interface (the frontend)** was built and is owned by **Jaskeerat**.
- **The server, database, and all automated pipelines** described in this document are owned and maintained by the team currently doing this build-out.
- **Credentials and access** for the server and database are held by **Ayan**; the Linear tracking board's credentials are held by **Ayan and Nidhi**.

## 6. What's automated vs. what still needs a human

| Fully automated, no human involved | A human is deliberately kept in the loop |
|---|---|
| Collecting data from all 9 sources | Approving an AI-written page before it goes live ("promotion") |
| Sensitivity/access-level labeling | Rotating/managing credentials |
| Turning raw data into draft knowledge pages | Reviewing anything flagged as a contradiction or unverified claim |
| Double-checking a draft page's citations | |
| Reporting progress to the Linear tracking board | |

This is a deliberate design choice: the system is trusted to do all the repetitive, mechanical work by itself, but nothing reaches an end user's screen without having passed through an automatic verification step *and* a human sign-off.

## 7. Is this secure?

- No credentials, passwords, or secret keys are ever visible to Cruz's end users, and none appear anywhere in this document or its companion technical docs.
- The database itself is not reachable from the public internet — only from the server it runs on.
- Every connection to Cruz's knowledge base — whether through the chat interface or a direct database browser — is protected by its own separate layer of access control.
- The sensitivity-labeling system is designed to fail *safe*: when the AI is uncertain how sensitive something is, it defaults to treating it as more restricted, not less.

## 8. What this system is not (yet)

- It does not let anyone control or approve anything directly from the Linear tracking board yet — today Linear is a one-way status report, not a remote control.
- It does not yet have a repeatable, one-command way to set up a brand-new client from scratch (this is tracked as future work, not something this document promises).
- It is not a static snapshot — new sources, clients, and capabilities get added over time, so treat this document as a living description of "how it works today," not a permanent spec.

## 9. Where to go for more technical detail

This document deliberately stays non-technical. Five companion documents in `docs/` go deep on each part of the system for engineers being onboarded to specific work:

- `docs/backend-server.md` — the server itself: what runs on it, how, and where.
- `docs/postgres-database.md` — the database: every table, every relationship, real current data.
- `docs/raw-ingestion.md` — how data gets fetched from all 9 sources and automated.
- `docs/wiki-ingestion.md` — how raw data becomes AI-written knowledge pages.
- `docs/linear-integration.md` — how progress reporting to Linear works, end to end.
