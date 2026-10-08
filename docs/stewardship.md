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
JSON-form frontmatter is supported. Duplicate keys at any mapping depth, malformed mappings,
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
Stateless IDs derive from path/rule/entity; the optional lifecycle store described
below supplies persistent identity across supported renames.
No inference has been implemented or invoked by the property validator. The
APIs are also exposed through the configured CLI/MCP runner described below.

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

Numerical cells are limited to 80 significant digits and arithmetic uses a local
200-digit Decimal context; larger input cells are diagnosed instead of silently
rounding to Python's default 28-digit context.

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

## Inbox preview (WP05.1)

`inbox_triage(snapshot, config)` accepts `inboxes` (path globs), `destinations`
(folder -> `{scope: literal}`), and `rules` (`field`, exact `value`, `destination`).
Every capture must declare frontmatter `scope`, matching the destination's scope.
Unknown/mismatching scope, multiple routes, missing/excluded/symlink destinations,
or no explicit route cause `leave_in_inbox`. Only an existing safe destination
can produce `preview_move`. Note prose cannot override these configured rules.

Suggestions carry source revision and always require revalidation before any
future move. No move, new folder, new topic, metadata edit, or split occurs. The
first lane supports explicit metadata only; implicit associations and acceptable
semantic destination sets remain unvalidated optional work.

## Duplicate opportunities (WP07.1)

`steward_duplicates.duplicates(snapshot, max_pairs=500, per_note=10, threshold=.7)`
groups identical full-file SHA-256 bytes separately from whitespace-normalized
full-text fingerprints. Frontmatter is retained in both fingerprints. Normalized
groups are not asserted semantically interchangeable.

Lexical candidates use five-word shingles, cap 512 features/note, 100 paths per
posting, and explicit pair bounds. They require matching declared string `scope`.
Reports expose metadata and numeric/negation token differences, bounded excerpts,
revisions, and opportunities/uncertainty. They never label a lexical pair safe to
merge and never transitively cluster candidate pairs. Missing candidates remain
a measured retrieval limitation, not evidence that the vault has no duplicates.
Embeddings, semantic classification, merge drafting, and consolidation are absent.

## Structured contradictions (WP08.1)

`steward_claims.contradictions(snapshot, fields, max_pairs=500)` consumes only
frontmatter `claims` arrays. Every claim requires string `entity`, `field`, `unit`,
`scope`, ISO `date`, `kind` (measurement/estimate/specification), and numerical
`value`. Optional `qualifier` is a literal condition and `status` is
current/superseded. Fields declare units as positive Decimal `factor` and optional
`offset`, plus nonnegative tolerance. No guessed unit conversions.

Pairs must share entity/field; scope/date/kind/condition mismatches and explicit
supersession are separate classifications. Only incompatible values with all
qualifiers matching become conflict findings, citing both revisions. Pair limits
and truncation are visible. The latest note is never selected as truth. Broad
prose contradictions, quotations, and inferred corrections require an optional
model lane and are not evaluated by this deterministic stage.

## Knowledge map (WP10.1)

`steward_graph.knowledge_map(snapshot, scopes=None, target=None, limit=100)` builds
existing resolved note edges from WP02 and explicit folder/type/frontmatter-tag
navigation. It shows isolated notes as a graph condition, not a defect. Components
use undirected existing edges only within identical scopes; existing cross-scope
edges are shown separately and never become suggested links. A declared scope
filter limits eligible nodes. `target` returns incoming/outgoing note paths and
edge evidence; edge rows are bounded with total counts/truncation.

No semantic connections, generated note writes, automatic indexes, or link
insertion is implemented. Existing links remain evidence even if inappropriate;
scope filters are analysis configuration, not a user-authentication mechanism.

## Search-index maintenance (WP11.1)

The existing `semantic.Config` now tracks optional `OBSIDIAN_EMBED_DIGEST`,
`OBSIDIAN_INDEX_PARSER_VERSION` (default 1), and JSON string-list
`OBSIDIAN_INDEX_EXCLUSIONS`. Incompatible identities refuse refresh/search until
an explicit rebuild. A configured digest is checked against Ollama `/api/tags`
before each real embedding batch. Without a configured digest, status explicitly
reports `digest_pinned: false`; model tags can still drift. Pinning is recommended
for reproducible evaluations. Legacy indexes cannot adopt a new pinned digest or
nondefault parser/exclusion configuration without rebuilding.

`OBSIDIAN_EMBED_BATCH_CHUNKS` (default 16, range 1..256) bounds each embedding
request. Results reject nonfinite vectors and mixed dimensions. Per-note updates
remain transactional. Persisted pending paths, queue age, last error, indexed
revisions and last reconciliation are exposed by `maintenance_status(conn, cfg)`.
The queue is reconstructed from vault/index state after restart; cancellation
leaves uncommitted notes pending. No independent competing vector index is added.

`refresh(..., force_hash=True)` catches changed bytes with unchanged mtimes. Nuru's
existing worker now uses this reconciliation on every pass and exposes `status`,
`last_success`, and a `paused` Event that yields between notes. This increases
no-change file reads but performs zero no-change embeddings. Lexical tools remain
independent of semantic availability. Lock/corruption errors are retried by the
existing worker; no automatic destructive database reset is introduced. Real
retrieval quality and interactive contention measurements remain NOT RUN.

## Configured runner, CLI and findings lifecycle

`obsidian-mcp-steward VAULT --config CONFIG.json [--state STATE.sqlite3]` explicitly
selects `packages`: properties, links, attachments, tasks, tables, reviews, triage,
duplicates, claims, graph, index. Configuration objects use the backend contracts
above: `schemas`, `attachments`, `tables`, `triage`, `duplicates`, `claims`, `graph`,
plus `as_of`, `timezone`, optional `policy: {exclusions: [...]}`, and `index_env`
(existing semantic environment keys). `index` only reads an existing index status;
it never embeds or builds one. A missing index is not complete.

Finding pages default 50, max 100; compare snapshots across pages. Without
`--details`, feature arrays are summarized by counts and finding evidence has
bounded excerpts/nesting with visible truncation. `--details` requests full
feature-specific inventories. Notes over 16 MiB, aliases/merge keys in YAML, and
read failures are diagnostics; metadata-based checks do not invent clean results.

An optional state database must be outside authored files or excluded by policy.
It records paths/entities, evidence-scoped acknowledgements/suppression, and
resolution. Restart and unambiguous content/inode-preserving renames preserve
entity IDs; ambiguous identical copies get new identities with a warning. There
is no assumption of reliable identity for every rename across filesystems.
Only complete passes retire disappeared findings. Changed evidence reopens review.
Config/evidence changes invalidate suppression even with unchanged source bytes.
No identifiers are written into notes. Existing unrelated SQLite databases and
state paths through symlinks are rejected.

Explicit CLI review adds `--review-id ID --evidence-revision HASH --review-status
acknowledged|suppressed|open` alongside `--state`. It rescans before revision-checked
sidecar review and never edits notes. The `audit_stewardship` MCP tool reads
`OBSIDIAN_STEWARD_CONFIG`; it does not write sidecar state, and is not added to
Nuru's default model tool allowlist. No inferred repairs, moves, merges, or deletes
are performed by any package.

## Optional interpretation boundary

`steward_interpretation.candidates` freezes an explicitly selected request queue
with 1..4 eligible notes/candidate, bounded excerpts, exact revisions, and same
declared scope for cross-note comparisons. `interpret` accepts an injected
provider and rendered-chat token counter. Without a per-run `RunApproval` it
refuses to call the provider. A live approval must be obtained from the human
before constructing it; the record is an application attestation, not an external
authorization service. No built-in provider, automatic inference scheduling,
environment bypass, or live CLI invocation is added.

Each call reserves counted input + capped output against the total run budget,
including failed calls. Live adapters must return `InferenceResponse` with exact
model revision and measured usage. Budget mismatches stop the run. Results record
calls, tokens, latency, unprocessed candidates, and invalid responses. Yield can
pause before a call. Failed requests are not retried silently.

Format, verbatim citations, and unchanged source revisions are validated in code.
Passing those checks does **not** prove semantic correctness: every suggestion
still requires human semantic review; no automatic action is permitted. Feature
question selection, provider integration, exact tokenizer/chat template, and
model-quality grading remain separately configured work. Fake-provider tests do
not demonstrate prompt-injection resistance or model efficacy. All live stages
remain OFF and NOT RUN pending per-run permission.
