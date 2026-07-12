# Knowledge Vault — Agent Contract

This file is the contract any AI agent (Claude Code, Cursor, or any other
compatible agent, plus Tacet itself) reads to understand how to work with
this vault.
The vault holds the synthesized knowledge built from meetings; raw meeting
records live in the sibling directory `./meetings-raw/`.

> **Important:** never modify files under `./meetings-raw/` from this vault.
> Those are immutable raw records owned by the Tacet app.

---

## Layout

```
./                     vault root (default: ~/meetings-kb/)
  AGENTS.md            this file
  index.md             navigation hub
  log.md               audit trail (one line per ingest / compile)
  projects/<slug>.md   project hub — synthesized state
  people/<slug>.md     person page — backlinks + notes
  sources/<id>.md      one per meeting; mirrors ./meetings-raw/<id>-*.md
  outputs/             generated artifacts (briefs, architecture, cost…)
  meetings-raw/<id>.md raw meeting records (immutable, app-owned)
```

Slugs are lowercase kebab-case (`project-foo`, `alice-chen`). Session IDs
are `YYYY-MM-DD-HHMMSS` (e.g. `2026-05-18-153412`).

---

## Frontmatter schemas

### `projects/<slug>.md`

```yaml
title: Project Foo
type: project
status: active # active | dormant | archived
slug: project-foo
created: 2026-05-18
updated: 2026-05-18
aliases: [Foo Migration]
members: [alice-chen, bob-jones]
sources: [2026-05-18-153412] # session_ids
brief: One-line summary; full brief in body.
tags: [migration, q2]
```

Body sections (in order):

```
## Brief
## Status
## Open Questions
## Decisions
## Touchpoints     <!-- chronological log of meetings; append-only -->
```

### `people/<slug>.md`

```yaml
title: Alice Chen
type: person
slug: alice-chen
aliases: [Alice]
created: 2026-05-18
updated: 2026-05-18
projects: [project-foo]
sources: [2026-05-18-153412]
```

Body: `## Notes`, `## Touchpoints`.

### `sources/<session-id>.md`

```yaml
title: Q2 Planning Sync
type: source
subtype: meeting
session_id: 2026-05-18-153412
meeting_path: ../meetings-raw/2026-05-18-153412-q2-planning-sync.md
started_at: 2026-05-18T15:34:12
projects: [project-foo]
people: [alice-chen, bob-jones]
created: 2026-05-18
updated: 2026-05-18
```

Body: `## Summary`, `## Claims`, `## Decisions`, `## Action Items`,
`## Open Questions`. Entity references use `[[wikilinks]]`.

---

## Wikilinks

`[[project-foo]]` resolves to `projects/project-foo.md`.
`[[alice-chen]]` resolves to `people/alice-chen.md`.
`[[2026-05-18-153412]]` resolves to `sources/2026-05-18-153412.md`.

The full meeting record (transcript, chat history) lives at the
`meeting_path` in the source page's frontmatter — read it from there when
you need raw context.

---

## Workflows

### `query <question>`

1. Read `index.md` to get oriented.
2. Identify the relevant project(s) by name/alias from `projects/`.
3. Read the project page and follow its `sources` to the linked meetings.
4. Synthesize an answer with inline `[[wikilink]]` citations.
5. Optionally append a one-line summary to `log.md`.

### `compile <type> <project>`

`type ∈ {brief, architecture, cost, freeform}`

1. Read `projects/<slug>.md` and every page in its `sources` list.
2. Apply the conventions in `../prompts/compile-artifact.md` (or the
   in-app prompt) for the requested type.
3. Write the result to
   `outputs/<slug>-<type>-YYYY-MM-DD.md`. Use ISO date for the suffix.
4. Append a line to `log.md`: `compile <type> <slug> -> <output path>`.
5. Reply with the output path.

### `ingest` (handled by the Tacet app)

Auto-runs after a meeting ends. Parses the latest meeting, decides which
project it belongs to (or creates a new one), writes the matching
`sources/<id>.md` with claims/decisions/actions/questions, appends a
Touchpoint to the project page, and updates relevant `people/` pages.
External agents do not need to run this themselves.

If you need to re-ingest a meeting after editing its raw record (e.g. you
fixed the title or attendees), the app exposes a `reingest_session` action.

---

## Rules

- **Never modify `./meetings-raw/`** — those are raw, immutable records.
- **Reuse existing slugs.** Before creating a new project or person, check
  the existing pages' `aliases` field for a match. People often go by
  multiple names; consolidate aliases instead of duplicating pages.
- **Touchpoints are append-only.** Don't rewrite history; new content
  goes at the end of the section.
- **Brief / Status sections are LLM-rewritten** when material new
  information arrives. Otherwise leave them alone.
- **Every state change appends a line to `log.md`** so the user can audit
  what touched the vault.
- **Use `fs_write` to create artifacts**; reply with the file path. Don't
  paste a long artifact body inline — the user wants a file they can open.

---

## Compile cheat-sheet

When the user says "compile a brief on X" / "draft an architecture doc
for X" / "estimate cost for X" / "give me a doc on X":

1. Resolve `<X>` to a project slug via `projects/` titles + aliases.
2. Read the project page + linked source pages.
3. Write to `outputs/<slug>-<type>-<YYYY-MM-DD>.md`:
   - `brief` — exec summary, status, decisions, risks, asks.
   - `architecture` — components, data flows, dependencies; technical
     claims pulled from sources, with citations.
   - `cost` — itemized estimates; flag any unsourced number with
     `(estimated)` and explain assumptions.
   - `freeform` — follow the user's instruction verbatim.
4. Append to `log.md` and reply with the path.
