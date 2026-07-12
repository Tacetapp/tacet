# Compile artifact — agent guidance

This prompt is read by the in-app chat agent (and by external agents browsing the vault) when the user asks to compile a project artifact. It is not consumed by Python code via PromptLoader — it is meant to be `fs_read` by the agent at compile time.

When the user says any of:

- "compile a brief on <project>"
- "draft an architecture doc for <project>"
- "give me a cost estimate for <project>"
- "write a doc on <project>" / "summarize <project>"

…follow this procedure:

## 1. Resolve the project

Read the `projects/` directory of the knowledge vault (default `~/meetings-kb/projects/`; absolute path is supplied in the system prompt) and find the project whose `title`, `slug`, or `aliases` field matches the user's reference. If no match is obvious, ask the user to disambiguate — do not guess.

## 2. Gather context

Read the matched project page (`projects/<slug>.md`) and every source listed in its `sources` frontmatter array (`sources/<id>.md`). For deeper detail, follow the `meeting_path` in each source's frontmatter to the raw meeting (default `~/meetings-kb/meetings-raw/<id>-*.md`). Stay grounded — every claim in the artifact must trace back to one of those files.

## 3. Pick the artifact type

Type is one of:

- **`brief`** — exec summary, status, key decisions, open risks, asks. Half a page to a page. Lead with a 2-sentence headline.
- **`architecture`** — components, data flows, dependencies, integration points. Pull every technical claim from the sources and cite the source page via `[[2026-05-18-153412]]` style wikilinks. If a critical detail is missing from the sources, list it under `## Open Questions` rather than inventing.
- **`cost`** — itemized estimates with assumptions called out. Tag any unsourced number with `(estimated)`. Group by category (compute, storage, network, licensing, labor) when the project has any infra/cloud component.
- **`freeform`** — follow the user's verbatim instruction. Their phrasing wins over the structures above.

## 4. Write the artifact

Use `fs_write` to create:

```
<vault>/outputs/<slug>-<type>-<YYYY-MM-DD>.md
```

The vault root path is supplied in the system prompt (default `~/meetings-kb/`).

The file is plain markdown — no YAML frontmatter is required (artifacts are read by humans and agents directly).

Recommended skeleton (apply only what fits the type):

```markdown
# <Project title> — <Artifact type>
_Compiled <YYYY-MM-DD> from [[<slug>]]._

## Headline
<1–2 sentences>

## <Section appropriate to type>

…

## Sources
- [[<session-id>]] — <one-line context>
- [[<session-id>]] — <one-line context>
```

## 5. Log + reply

Append one line to the vault `log.md`:

```
- <ISO timestamp> — compile <type> <slug> -> outputs/<filename>
```

Reply to the user with the file path so they can open it. Don't paste the artifact body into the chat — they want a file.

## Rules

- **Be terse.** A useful brief is 300–600 words. An architecture doc is 800–1500. A cost estimate is mostly a table.
- **Cite everything material.** Every claim that came from a meeting needs a `[[<session-id>]]` reference.
- **Surface uncertainty.** If sources contradict, say so under `## Open Questions`. Don't smooth over disagreements.
- **Don't refuse for missing context.** If the project page is sparse, write what you can and tell the user what additional meetings would help.
