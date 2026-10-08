# Read-only vault stewardship

`obsidian_mcp.stewardship.Snapshot(root).refresh()` reconciles eligible notes and
assets. It prunes the link auditor's configured exclusions, rejects symlinks,
reads through the existing descriptor-relative safe reader, diagnoses concurrent
edits/read errors, and caches parsed notes by content revision. The snapshot is
not a filesystem transaction. No authored files or sidecars are written.

`properties(snapshot, schemas)` validates frontmatter. A schema has a unique
`name`, optional vault-relative `glob` (default `*.md`) and exact frontmatter
`type` selector, and a `fields` object. Fields declare `type` (string, number,
integer, boolean, list, date), optional `required`, `values`, and list `item_type`.
Dates must be real ISO `YYYY-MM-DD` dates. YAML timestamp inference is disabled,
so malformed dates are field defects rather than silently converted values.
JSON-form frontmatter is supported. Duplicate top-level keys, malformed mappings,
and YAML merge keys are frontmatter defects; such notes are not field-validated.

Optional `aliases` maps legacy names to declared properties, producing review
suggestions. It does not silently rename or supply required fields. Cross-field
`rules` declare `left`, `op` (`eq`, `le`, `requires`), and `right`. Invalid or
conflicting schemas fail separately from note defects. Schemas are re-applied
even when file bytes do not change. No schema is guessed from prose.

Example:

```json
[{"name":"project","glob":"Projects/*.md","fields":{
  "status":{"type":"string","required":true,"values":["active","paused"]},
  "due":{"type":"date"},"tags":{"type":"list","item_type":"string"}
}}]
```

Reports contain source/line/revision/evidence, rule/version, severity, suggested
action, and counts. `complete` reports scan diagnostics, not semantic coverage.
IDs derive from path/rule/entity; renames change IDs until the lifecycle milestone.
No inference has been implemented or invoked by the property validator. The
initial API is a Python backend; CLI/MCP integration is a later shared milestone.

## Attachment inventory (WP03.1)

`attachments(snapshot, large_bytes=..., reference_properties=(...))` returns asset
sizes, SHA-256 revisions, exact duplicate groups, potential duplicate bytes counted
once per group, and known owners. Assets hash in 1 MiB chunks with one reader;
there is no full-asset buffering or hashing concurrency. No-change reconciliation
rehashes assets for correctness; a persistent trusted hash cache is not yet built.

Ownership uses WP02 body links, explicitly declared frontmatter reference fields,
and Canvas `type:file` nodes. Canvas files over 16 MiB or malformed nodes are
diagnosed. Unrecognized plugin sources and excluded notes remain a coverage gap.
Unreferenced assets have **unknown ownership**, never deletion eligibility. Reports
never delete, quarantine, merge, or update references. Potential duplicate bytes
are opportunities, not reclaimed space. Vision/similarity inference remains off.

## Task ledger (WP04.1)

`tasks(snapshot, as_of="2026-10-08", timezone="Europe/Berlin")` requires a real
ISO date and valid IANA timezone. `as_of` is the explicitly chosen local calendar
date; the machine clock is never consulted. Supported list checkboxes are space
(open), x/X (completed), - (canceled), / (in progress). Due dates use
`[due:YYYY-MM-DD]` or `📅 YYYY-MM-DD`. Undated tasks are not overdue. Examples in
code, comments and quoted lines are excluded. Invalid dates/statuses are reported.

Mirrors require `[task:ID]` and an explicit frontmatter `project`. Different
projects never share an identity. `task_record: historical` retains an occurrence
but excludes it from overdue/conflict checks. Conflicting active status/deadline
mirrors cite all sources. Newest text is not chosen as truth; prose completion is
not inferred. `[repeat:...]` is retained and queued for review, with no invented
future recurrence. Reopening is represented by an open checkbox, not guessed from
prose. IDs without explicit task markers are occurrence-local.

## Structured tables (WP09.1)

`steward_tables.tables(snapshot, configs)` parses Markdown-it pipe tables with
declared column names, literal unit/currency suffixes/prefixes, and dot/comma
decimal separators. Thousands separators are not guessed. Number/date/string
columns distinguish required blanks, zero, invalid, and explicitly declared `na`
strings. Malformed widths and unmatched tables are coverage diagnostics; they do
not yield fabricated arithmetic. Pipes in cells must be escaped, including inside
code spans. Formula strings and plugin code remain inert data.

Schema keys: `name`, optional `glob`, `columns`, optional `unique_by`, `totals`,
`formulas`. A total specifies `label_column`, `label`, numerical `column`, and
nonnegative decimal `tolerance`; exactly one total row is required. Unresolved
input cells prevent computing a total. Formulas explicitly name numeric `left`,
`right`, `result`, and `op` (add/multiply), with optional tolerance. All arithmetic
uses Decimal. Reports retain raw input rows/lines and revisions, never overwrite
cells. No exchange-rate lookup, arbitrary expression evaluator, locale inference,
or prose extraction is provided.

## Project review (WP06.1)

`project_reviews(snapshot, as_of=..., timezone=..., limit=20)` recognizes explicit
`type: project`, nonempty `project` ID, and `status: active/paused/completed`.
Active projects are flagged for missing string `next_action` or an explicit
`review_due` on/before the chosen date. Paused/completed projects are not labeled
stale. `type: decision` notes with the same project are cited with their declared
`decision_id`, `decision_status`, and `date`; proposed/reopened entries are counted
unresolved. Conflicting records are retained rather than resolved by recency.

Task/decision evidence is bounded (1..100 per section, default 20), with original
counts and truncation visible. Prose decision extraction and recent-change history
are explicit evidence gaps. This is a metadata checklist, not a generated factual
summary. Reports never choose next actions or modify statuses.
