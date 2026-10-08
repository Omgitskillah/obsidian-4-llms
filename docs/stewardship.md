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
