---
name: prose
description: The writing standard for every word polly's team ships — code comments, commit/PR text, docs, Markdown. Built on the humanizer skill plus a few house rules. The builder writes to it; the reviewer re-checks it. Load whenever an implement or review task will produce or judge prose, and for your own doc authoring.
---

# prose — how we write comments, docs, and PR text

All human-facing text polly and its sub-agents ship goes through this standard.
It rides on the `humanizer` skill and adds a few house rules. Sub-agents run in a
different worktree and can't load polly's skills, so when you delegate you must
**inline these rules into the dispatch `input`** — don't just name the skill.

## The standard
- **Run it through `humanizer`.** Strip AI-writing tells before shipping prose —
  load the `humanizer` skill (`~/.claude/skills/humanizer/`, from
  github.com/blader/humanizer) if it's on the box; otherwise apply its intent from
  "tells to cut" below.
- **Crisp and concise.** Fewest words that still land. Cut filler, hedging, and
  throat-clearing. One good sentence beats three.
- **Don't drown the reader in references.** Cite or link only what a reader
  actually needs — no exhaustive footnotes, no dumping every related file, ticket,
  or line number. Point to the one thing that matters.
- **Open with an ELI5, then build down.** For any summary (PR description, doc,
  design note), lead with a plain-language take a non-expert gets — what changed and
  why it matters, no jargon. Then step down one level at a time, each building on the
  last, connecting the dots from concept to mechanics. A layman should get the gist
  from the first lines; an engineer should reach the specifics by the end. Never open
  at the low level.
- **Comments make code legible, not redundant.** A comment explains the scenario
  or intent the code can't — why this exists, what edge case it guards — never
  restates what the line already says. One or two lines; let clear names and
  structure carry the rest. (Mirrors this repo's CLAUDE.md "Code comments".)

## Tells to cut (the humanizer essence, if the skill isn't loadable)
Inflated significance ("crucial", "vital", "seamless", "robust", "powerful"),
promotional tone, em-dash pile-ups, rule-of-three padding, vague attribution
("studies show", "it's well known"), formulaic transitions ("Moreover,",
"In today's landscape"), tacked-on "-ing" clauses that add nothing, and needless
conjunctive filler.

## Who applies it
- **Builder** (`implement`) — write every comment, commit message, PR description,
  and doc to this standard *as you go*; it's part of "done", not a later pass.
- **Reviewer** (`cross-review`) — independently re-check the diff's prose in case
  something slipped: flag AI-writing tells, verbosity, over-referencing, and
  comments that just echo the code. Usually non-blocking suggestions; blocking only
  when the prose is misleading or unreadable.
- **polly** — apply it to your own doc/Markdown authoring too (you have the host
  `humanizer` skill).
