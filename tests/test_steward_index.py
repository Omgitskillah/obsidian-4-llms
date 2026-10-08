from dataclasses import replace
import hashlib
import os

import pytest

from obsidian_mcp import semantic


def config(root):
    return semantic.load_config(root, {'OBSIDIAN_SEMANTIC_DB': str(root / '.test-index.sqlite3'), 'OBSIDIAN_EMBED_DIGEST': 'fake-1'})


def fake(texts):
    return [[float(hashlib.sha256(t.encode()).digest()[0]) / 255, 1.0] for t in texts]


def state(conn):
    return list(conn.execute('SELECT path,title,hash FROM notes ORDER BY path')), list(conn.execute('SELECT path,text FROM chunks ORDER BY path,text'))


def test_preserved_mtime_and_full_rebuild_equivalence(tmp_path):
    path = tmp_path / 'a.md'
    path.write_text('First version, enough content for indexing.')
    cfg = config(tmp_path)
    with semantic.connect(cfg) as conn:
        semantic.refresh(conn, cfg, fake)
        before = path.stat()
        path.write_text('Other version, enough content for indexing.')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert semantic.refresh(conn, cfg, fake, force_hash=True).embedded == ['a.md']
        incremental = state(conn)
        assert semantic.refresh(conn, cfg, lambda _: pytest.fail('no-change embedding'), force_hash=True).embedded == []
        semantic.reset(conn)
        semantic.refresh(conn, cfg, fake)
        assert incremental == state(conn)


def test_failure_queue_restart_and_digest_change(tmp_path):
    (tmp_path / 'a.md').write_text('Some content to embed.')
    cfg = config(tmp_path)
    conn = semantic.connect(cfg)
    def unavailable(_):
        raise ConnectionError('offline')
    with pytest.raises(ConnectionError):
        semantic.refresh(conn, cfg, unavailable)
    assert semantic.maintenance_status(conn, cfg)['pending'] == ['a.md']
    conn.close()
    conn = semantic.connect(cfg)
    assert semantic.refresh(conn, cfg, fake).embedded == ['a.md']
    assert not semantic.maintenance_status(conn, cfg)['pending']
    with pytest.raises(ValueError, match='Rebuild'):
        semantic.refresh(conn, replace(cfg, model_digest='fake-2'), fake)
    conn.close()


def test_pause_and_batch_bounds(tmp_path):
    (tmp_path / 'a.md').write_text(('Sentence with many tokens.\n\n') * 200)
    cfg = replace(config(tmp_path), chunk_chars=200, batch_chunks=2)
    calls = []
    with semantic.connect(cfg) as conn:
        result = semantic.refresh(conn, cfg, fake, should_stop=lambda: True)
        assert result.interrupted and result.pending == ['a.md']
        def embed(texts):
            calls.append(len(texts))
            return fake(texts)
        semantic.refresh(conn, cfg, embed)
        assert max(calls) <= 2 and len(calls) > 1


def test_config_changes_refuse_vectors_and_bad_output(tmp_path):
    (tmp_path / 'a.md').write_text('content')
    cfg = config(tmp_path)
    with semantic.connect(cfg) as conn:
        with pytest.raises(ValueError, match='nonfinite'):
            semantic.refresh(conn, cfg, lambda texts: [[float('nan'), 1] for _ in texts])
        semantic.refresh(conn, cfg, fake)
        for changed in [replace(cfg, parser_version='2'), replace(cfg, exclusions=('archive',))]:
            with pytest.raises(ValueError):
                semantic.refresh(conn, changed, fake)
