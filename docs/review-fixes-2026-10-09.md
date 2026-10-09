# PR3 review fixes — 2026-10-09

This round targets macOS, per the user's instruction. Windows import and mutation
portability are deferred; no Windows compatibility is claimed.

- Revision hashes cover original bytes; UTF-8 decoding uses replacement characters
  for embedding. A Latin-1 byte no longer blocks later notes.
- Database ancestor aliases are canonicalized. Database/WAL/SHM symlinks remain
  rejected. State exclusions and vault ownership use the canonical root/path.
- Existing indexes gain nullable size/ctime columns. Unknown signatures are read
  once; unchanged signatures are skipped except for the required rolling slice.
- Every pass hashes at most 100 otherwise unchanged notes, using a persistent
  cursor. Stable vault coverage takes ceil(N/100) completed passes. Explicit
  force_hash still supports a full reconcile. Changed/new notes are not capped.
- Signature updates are batched; unchanged notes and unchanged queue/configuration
  metadata are not rewritten. Traversal prunes excluded/symlink directories once.
- Busy or unreadable notes remain pending while later notes progress. Provider
  failures still abort without replacing the last complete revision.
- Interactive connections retain 30-second lock waiting; Nuru applies a one-second
  background timeout. Pinned digest checks remain per batch to prevent tag drift
  midway through a refresh. Configuration changes still require a rebuild.

Validation: 667 backend/vendor tests passed, one skipped, with fake embeddings and
real SQLite. Regressions cover raw-byte hashes, signature migration, bounded reads,
zero unchanged-note updates, busy-note progress, canonical aliases and sidecar
symlinks. Nuru's accompanying report records five 2,000-note no-change passes at
33.67–35.46 ms, 100 reads/pass, two DML statements/pass, zero note updates and zero
embeddings. These local warm-cache numbers are not a paired comparison with the
reviewer's earlier timings. No live models or paid APIs were called.

The earlier live embedding-contention gate remains failed historical evidence;
these deterministic improvements do not establish that it now passes.
