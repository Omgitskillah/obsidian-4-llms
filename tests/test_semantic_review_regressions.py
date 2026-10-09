"""Review regressions using fake embeddings and real SQLite, never live inference."""
import hashlib
import os
from dataclasses import replace

from obsidian_mcp import semantic


def embed(texts):
    return [[1., float(len(text) % 7), .5] for text in texts]


def fixture(tmp_path, count=1):
    root = tmp_path / 'vault'
    root.mkdir()
    for n in range(count):
        (root / f'{n:04}.md').write_text(f'Note {n}.\n')
    config = semantic.Config(root, tmp_path / 'index.db', 'fake', 400, 'http://unused')
    conn = semantic.connect(config)
    semantic.refresh(conn, config, embed)
    return root, config, conn


def test_invalid_utf8_hashes_original_bytes_and_does_not_block_other_notes(tmp_path):
    root, config, conn = fixture(tmp_path)
    raw = b'caf\xe9'
    (root / 'bad.md').write_bytes(raw)
    (root / 'later.md').write_text('Later note.')
    result = semantic.refresh(conn, config, embed)
    assert result.embedded == ['bad.md', 'later.md']
    assert conn.execute("SELECT hash FROM notes WHERE path='bad.md'").fetchone()[0] == hashlib.sha256(raw).hexdigest()
    assert '\ufffd' in conn.execute("SELECT text FROM chunks WHERE path='bad.md'").fetchone()[0]
    conn.close()


def test_unchanged_pass_has_bounded_reads_and_no_note_updates(tmp_path, monkeypatch):
    root, config, conn = fixture(tmp_path, 250)
    reads, statements = [], []
    original = semantic._read_note
    monkeypatch.setattr(semantic, '_read_note', lambda root, path: reads.append(path.name) or original(root, path))
    conn.set_trace_callback(statements.append)
    covered = set()
    for _ in range(3):
        reads.clear()
        result = semantic.refresh(conn, config, lambda _: (_ for _ in ()).throw(AssertionError('unexpected embeddings')))
        assert not result.embedded
        assert len(reads) == 100
        covered.update(reads)
    assert len(covered) == 250
    assert not any(s.startswith('UPDATE notes') for s in statements)
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
    conn.close()


def test_busy_first_note_keeps_pending_but_later_note_is_indexed(tmp_path):
    root, config, conn = fixture(tmp_path)
    (root / '0000.md').write_text('First edit.')
    (root / 'later.md').write_text('Later note.')
    def concurrent(texts):
        if '0000' in texts[0]:
            (root / '0000.md').write_text('Another edit.')
        return embed(texts)
    result = semantic.refresh(conn, config, concurrent)
    assert result.embedded == ['later.md']
    assert result.pending == ['0000.md']
    assert 'changed during embedding' in result.errors['0000.md']
    assert semantic.refresh(conn, config, embed).embedded == ['0000.md']
    conn.close()


def test_canonical_parent_alias_is_allowed(tmp_path):
    root, config, conn = fixture(tmp_path)
    conn.close()
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    with semantic.connect(replace(config, db_path=alias / 'index.db')) as conn:
        assert semantic.refresh(conn, config, embed).embedded == []


def test_legacy_stat_columns_are_migrated_and_preserved_mtime_is_detected(tmp_path):
    root, config, conn = fixture(tmp_path, 150)
    # A migrated row with unknown signature must be read even outside this pass's rolling slice.
    conn.execute("UPDATE notes SET size=NULL,ctime_ns=NULL WHERE path='0149.md'")
    conn.commit()
    path = root / '0149.md'
    stamp = path.stat()
    path.write_text('New same-time content.')
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert semantic.refresh(conn, config, embed).embedded == ['0149.md']
    conn.close()


def test_existing_index_schema_gains_nullable_signature_columns(tmp_path):
    root, config, conn = fixture(tmp_path)
    old_hash = conn.execute('SELECT hash FROM notes').fetchone()[0]
    conn.execute('ALTER TABLE notes DROP COLUMN size')
    conn.execute('ALTER TABLE notes DROP COLUMN ctime_ns')
    conn.close()
    conn = semantic.connect(config)
    assert {'size', 'ctime_ns'} <= {r[1] for r in conn.execute('PRAGMA table_info(notes)')}
    assert semantic.refresh(conn, config, embed).embedded == []
    assert conn.execute('SELECT hash,size,ctime_ns FROM notes').fetchone()[0] == old_hash
    assert conn.execute('SELECT size FROM notes').fetchone()[0] is not None
    conn.close()
