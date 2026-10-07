# Link integrity audit

`obsidian-mcp-links` and the MCP `audit_links` tool inspect local references without
Obsidian, Ollama, embeddings, or generation. They never edit notes or create a
sidecar file. A `LinkIndex` caches parsed notes in memory and reconciles files on
every scan; restarting rebuilds that derived index.

## Commands

Standalone checkout:

```bash
uv run --locked obsidian-mcp-links /absolute/path/to/vault
uv run --locked obsidian-mcp-links /absolute/path/to/vault --all
uv run --locked obsidian-mcp-links /absolute/path/to/vault --target Topics/Target.md
uv run --locked obsidian-mcp-links /absolute/path/to/vault --policy /path/to/policy.json
```

MCP arguments: `limit` (1–100, default 50), `offset` (nonnegative, default 0),
`target_path` (exact vault-relative incoming-reference target), and
`include_resolved` (default false). Configure the server's policy file through
`OBSIDIAN_LINK_POLICY`. Defaults exclude `Templates`, `trash`, `Maintenance`, and
hidden entries. Other configured exclusions are paths/prefixes or case-sensitive
shell-style globs, not Obsidian's regex exclusion configuration.

Policy example:

```json
{
  "exclusions": ["Templates", "trash", "Maintenance", "Private/*"],
  "intentional": [{"source": "Projects/*.md", "target": "Future note"}]
}
```

`intentional` declares a missing destination deliberately unwritten. The source
is a glob and the target is the literal `target` value in the report (Markdown
hrefs can be URL-encoded). The declaration does not suppress a missing heading in
an existing note. Creating the destination makes the reference resolve normally.
Unknown policy keys and malformed values raise a validation error.

## Resolution contract

- Extract each occurrence, including repeats, from parsed Markdown. CommonMark
  handles fenced/indented code, code spans, escaped brackets, and Markdown
  reference definitions. Obsidian `%%` comments are ignored, including an
  unterminated comment; literal comment markers in code stay literal.
- Wikilinks and embeds support display aliases, file extensions, local headings,
  nested heading paths, and `#^block` IDs. A bare wikilink matches filename/stem
  using Unicode NFC and case folding; multiple matches are **ambiguous**, even
  when Obsidian might use proximity heuristics. Folder-qualified wikilinks are
  exact vault-root paths. Assets need their extensions.
- Markdown supports inline/image and defined reference-style links, URL-encoded
  destinations, balanced parentheses, and optional titles. `./` and `../` mean
  source-relative paths; `/` means vault-relative. Unprefixed paths can be emitted
  by Obsidian as vault-root paths, so the auditor considers both root and source
  directory and reports collisions. For a shortest bare filename with neither
  concrete path present, a unique vault basename can resolve it. It never uses a
  matching basename to forgive a missing folder-qualified path.
- Explicit paths use Unicode NFC normalization and case-sensitive inventory matches on every platform so a
  differently cased path is not silently repaired. Bare filenames use the rule
  above. Target paths are normalized and must remain inside the vault. Symlink
  files/directories are unsupported, and descriptor-relative reads prevent a
  link swap from reading outside the vault.
- ATX/setext headings are matched by rendered plain text, normalized case/Unicode;
  Markdown also accepts a documented simple slug (punctuation removal and spaces
  replaced by hyphens). Repeated headings and repeated block IDs are ambiguous.
  Block IDs use Obsidian's Latin letters/digits/dashes syntax and must be outside
  code. Heading suggestions and file suggestions remain advisory.
- `[[AI]]` is not resolved from another note's `aliases` property. Obsidian itself
  creates `[[Artificial Intelligence|AI]]` when selecting that alias; display
  aliases work. Frontmatter title/ID lookup from older tools is not used here.
  See [Obsidian aliases](https://obsidian.md/help/aliases) and
  [internal links](https://obsidian.md/help/links).

## Results and reverse index

Every reference has source path/revision, source line, raw syntax, destination,
embed flag, finding ID, status and reason. Existing anchor targets also carry their
revision. The reverse index includes references to known existing files, including
broken headings/blocks and self-links; it excludes unresolved filenames and
ambiguous file destinations because there is no single known target. A missing
file does not appear as an incoming edge merely because its name resembles a note.

Statuses are `resolved`, `broken`, `ambiguous`, `intentional-unwritten`, `excluded`,
`unsupported`, and `external`. External URIs are never fetched. The default page
contains broken, ambiguous, and unsupported occurrences; `--all` includes all
statuses. Counts always cover the selected scope, not only the visible page.

File repair candidates contain filename similarity evidence and current candidate
revisions. Candidate retrieval is bounded to 250 names selected by shared trigrams
(or the first 250 names for very short queries), returns at most three, and has no
claim of exhaustive or semantic ranking. Anchor suggestions similarly return up
to three existing close names. No suggestion is applied automatically.

Pages include a stable `snapshot` hash, `count`, and `next_offset`. Compare snapshots
between calls and restart pagination after changes. IDs survive reruns and line
shifts for the same source path, raw reference, and occurrence ordinal. Renaming a
source changes its finding IDs; this version has no persistent identity or
acknowledgement journal. It replaces old results rather than accumulating duplicate
findings. References with the same raw text are distinguished by occurrence ordinal.

`complete` means the declared scan/extraction coverage has no diagnostics; it does
not mean every reference resolved or that the live vault was locked. Unreadable
files/directories and concurrent edits produce diagnostics, never a claim that
such a target's heading is missing. An unreadable directory downgrades unresolved
filenames to `unsupported` because the inventory is incomplete. Source and target
revisions are evidence snapshots; later edits require re-analysis.

## Limitations and later validation

Frontmatter links and HTML links are not audited; relevant files get explicit
coverage diagnostics. Plugin-generated links, Canvas edges, HTML anchors, attachment
fragments such as PDF pages, and vault-wide heading/block search are unsupported.
This is a scoped auditor, not full Obsidian renderer compatibility. No scripts,
DataviewJS, or embedded code execute. No external URL availability checks run.

Hashes are reread across the vault on each reconciliation. Only changed note
content is reparsed, but resolution and reverse edges are rebuilt globally;
this is not an event-only or constant-time whole audit. The result cap bounds the
returned occurrences, not scan time or total in-memory index size.

Model ranking can later consume the candidates, raw reference, source revision,
and target evidence without changing deterministic classification. Test its top-1
precision, coverage, latency and review time separately. No model quality or
real-vault time saving has been established by the offline fixture.
