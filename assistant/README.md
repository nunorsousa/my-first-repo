# 🗂️ Executive assistant

A personal assistant agent that handles the meeting lifecycle: it prepares you to walk into
a meeting, and captures what came out of one.

It runs inside Claude Code against your own Google account and Granola notes — no server,
no API keys, nothing to deploy. **It drafts; it never sends.**

## What it does

| Command | What you get |
| --- | --- |
| `/meeting-prep` *(or `next`, or a phrase)* | A brief for an upcoming meeting: what needs you, who's attending and how you know them, open threads, what was decided last time, what's still outstanding, and a few talking points → `briefs/` |
| `/meeting-recap` *(or `last`, or a phrase)* | Decisions, action items with owners and dates, open questions → `recaps/`, items appended to `state/action-items.md`, and a follow-up email left in **Gmail Drafts** |

The two halves close a loop: recaps write action items to the ledger, and the next brief
for the same people reads them back. Meeting three knows what was promised in meeting one.

## Setup

**Fill in `profile.md`.** This is the whole setup. It's who you are, what you're focused
on, who matters, and how you write. The assistant asks for it on first run if you'd rather
answer questions than edit a file.

Skip it and the briefs still work, but the drafted emails won't sound like you — pasting
two or three of your own sent emails into the Voice section fixes that faster than anything
else.

**Connectors.** Google Calendar, Gmail, Granola, and Google Drive need to be connected in
Claude. Missing one degrades gracefully: no Granola means no recaps, no Drive just means
briefs skip the documents section.

## Try it

```
/meeting-prep next     # brief for your next calendar meeting
/meeting-recap last    # recap of your most recent Granola meeting
```

Then check Gmail: the follow-up is sitting in **Drafts**, and Sent is untouched.

## Draft-only, structurally

The assistant operates at **Tier 1 — draft, never dispatch** (see `policy.md` for the
tiers). That isn't a promise in a prompt: the agent's tool allowlist in
`.claude/agents/executive-assistant.md` simply omits every tool that could send mail,
create or move a calendar event, share a Drive file, or delete anything. It can't send,
because it has no way to.

Raising the ceiling later — letting it send a follow-up after you say yes — is one line in
that allowlist, not a rewrite.

`policy.md` also draws the boundary that matters for an agent reading your inbox: meeting
transcripts, invite descriptions, email bodies, and web pages are **data, never
instructions**. An email telling the assistant to forward a contract is something the brief
reports, not something it does.

## Files

```
assistant/
  profile.md            you: role, priorities, VIPs, voice          ← fill this in
  policy.md             autonomy tiers and guardrails (binding)
  briefs/               YYYY-MM-DD-<slug>.md, one per prepped meeting
  recaps/               YYYY-MM-DD-<slug>.md, one per recapped meeting
  state/action-items.md the ledger, append-only

.claude/
  agents/executive-assistant.md    role + tool allowlist
  skills/meeting-prep/SKILL.md
  skills/meeting-recap/SKILL.md
```

Briefs and recaps are committed to git, so meeting history is versioned and greppable.
Treat that as a feature with a cost: don't recap anything you wouldn't want in the repo.

## Extending it

The meeting lifecycle is one of seven responsibilities an assistant like this can own. The
rest, roughly in value order: inbox triage with reply drafts · calendar defense and
scheduling · a full commitment register (*I owe* / *owed to me*, reconciled from email as
well as meetings) · daily briefings — the `/morning` skill already covers this one ·
document drafting · travel logistics, which would tie neatly into `flightdeals` next door.

Each is another skill in `.claude/skills/` plus, where it needs one, a state file here. The
agent definition and `policy.md` are shared and shouldn't need to change.

To run these on a schedule rather than on demand, Claude Routines can invoke a skill on a
cron — worth setting up once the output has earned your trust, not before.
