# Development log

Append-only implementation and verification record.

## 2026-10-03 — Standalone setup and mutation safety fixes

- Base: upstream main at `7301b0c53baf03b1599064f9308a62f46d0aee66`.
  Branch: `fix/vault-mutation-safety`. This entry describes the initial local
  implementation; the branch was subsequently published for PR review.
- Added server-wide `OBSIDIAN_READ_ONLY=1` enforcement on every mutating entry
  point. Read-only metadata lookups use SQLite read-only connections rather
  than reconciling and modifying the index.
- Added `read_note_snapshot` and required `expected_revision` for body
  replacement. Kept the existing text-returning read_note API.
- Added a POSIX mutation helper: reentrant in-process lock, cross-process flock,
  pinned directory descriptors with O_NOFOLLOW, exclusive publication of fully
  written files, staged atomic replacements with permissions and byte checks,
  and no-clobber same-filesystem moves retaining source until index success.
- Removed abandoned filesystem workers. Mutations run to completion and may
  block on stalled disks. Ripgrep retains its subprocess timeout. Removed the
  three old worker-timeout tests because those workers no longer exist.
- Added deterministic tests for revision conflicts, staging and replacement
  failures, symlink swaps, index failures and guarded recovery, competing move
  destinations, file permissions, thread and process coordination, read-only
  execution, and MCP protocol calls. Index database symlinks are rejected.
- Redacted request params and exception messages from access logs; results were
  already omitted. Tested that private content appears nowhere in log output.
- Made vendor/access-logging a local path dependency, refreshed root uv.lock,
  and switched CI to the standalone root commands. Added .env.obsidian.example
  with read-only/protected-user defaults. Configuration loads from cwd or an
  explicit OBSIDIAN_ENV_FILE instead of assuming a parent repository layout.
- Corrected README append-only/version claims, missing parent links, manual
  semantic-index refresh, private index contents, setup and recovery limits.
  Kept the note schema and stdio transport; no remote HTTP transport was added.
- `uv lock` initially hit the sandbox's uv-cache restriction; approved escalation
  allowed `uv lock && uv sync --locked` to install successfully from this repo.
- Initial existing-suite run: 456 passed, 1 skipped. New regression runs exposed
  two test assertions expecting raw exceptions rather than ToolError: one run
  had 466 passed/1 failed/1 skipped and another had 472 passed/1 failed/1 skipped.
  Corrected the assertions; no production error handling was weakened.
- Coverage run after those corrections: 473 passed, 1 skipped, 97% coverage.
  Final run after read-only SQLite changes:
  `VAULT_PATH=/private/tmp/obsidian-fixes-test-vault .venv/bin/python -m pytest
  tests vendor/access-logging/tests -q --cov=obsidian_mcp --cov=access_logging
  --cov-report=term`: 475 passed, 1 skipped, 97% overall statement coverage;
  mutations.py 94%. The skipped test requires live Ollama; it was not exercised.
  Coverage runs emitted 33 ResourceWarnings from older test-created SQLite
  connections. They do not represent a warning-free verification result.
- `python -m compileall -q src vendor/access-logging/src` and `git diff --check`
  passed. Reviewed the implementation and documentation diffs.
- Limits: POSIX-only mutation primitives; hard-link moves require one filesystem;
  locks do not coordinate other applications such as Obsidian; an external writer can race after
  the last revision check; no atomic SQLite/filesystem crash journal or client
  retry deduplication. No personal vault, credentials, or live service changed.

## 2026-10-03 — Address Nuru integration review

- Restored Nuru's original repository-root configuration fallback, preserving
  explicit config and standalone cwd precedence. Added a subprocess import test
  using Nuru's servers/obsidian cwd and protection-only environment.
- Removed read_note_snapshot from the MCP tool list. read_note now accepts
  include_revision=true for {path, content, revision}; note_info includes revision.
  Default read_note remains text. Replacements still require a revision, including
  protected Nuru notes: folder limits and backups do not prevent stale overwrites.
- Added protocol tests using only the existing read_note/write_note tools with
  user-authored protection enabled. Old prompt sets that omit expected_revision
  still need to request a revision; no extra allowlisted tool is required.
- Invalid UTF-8 produces an intentional repair-encoding error without dropping
  bytes; added read, revision-read, and append regression tests.
- Writable index lookups reconcile again under writer coordination. Read-only
  lookups remain non-mutating and filter missing files. Reconciliation prunes
  missing rows; it does not import or rediscover externally moved notes by itself.
- Documented owner-only new files/backups, the persistent vault-root lock file,
  and POSIX dependence. Removed unrelated project prose from README and clarified
  the historical publication status in this log.
- The separate Nuru tools/logging checkout is not present here; the vendored copy
  is redacted, but upstream integration must use that copy or apply the same
  redaction. No assertion is made that Nuru's other copy has been changed.
- Initial run of the updated existing suite: 475 passed, 1 skipped. The command
  attempting to add regression files first used incorrect relative paths and
  created no files; corrected the paths before the regression run.
- Final regression command: `VAULT_PATH=/private/tmp/obsidian-review-feedback
  .venv/bin/python -m pytest tests vendor/access-logging/tests -q`:
  481 passed, 1 skipped (live Ollama). `git diff --check` passed.
- Changes are uncommitted pending review. Existing published history was not
  rewritten; a follow-up commit should describe Nuru compatibility and revision
  handling rather than claiming the change is only access logging.
