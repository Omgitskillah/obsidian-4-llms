"""Actual SQLite locks/corruption and mid-inference edits; no live models."""
import hashlib
import sqlite3
import time

import pytest

from obsidian_mcp import semantic


def embed(texts):
    return [[1.,float(len(t)%7),.5] for t in texts]


def setup(tmp_path):
    root=tmp_path/'vault';root.mkdir();(root/'a.md').write_text('Original note.\n')
    config=semantic.Config(root,tmp_path/'index.db','fake',400,'http://unused')
    conn=semantic.connect(config);semantic.refresh(conn,config,embed)
    return root,config,conn


def test_actual_write_lock_release_and_reconciliation(tmp_path):
    root,config,conn=setup(tmp_path)
    other=sqlite3.connect(config.db_path)
    other.execute('BEGIN IMMEDIATE')
    conn.execute('PRAGMA busy_timeout=50')
    (root/'a.md').write_text('Changed note.\n')
    start=time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            semantic.refresh(conn,config,embed,force_hash=True)
        assert time.monotonic()-start < 1
    finally:
        other.rollback();other.close()
    assert semantic.refresh(conn,config,embed,force_hash=True).embedded == ['a.md']
    conn.close()


def test_truncated_index_fails_and_explicit_rebuild(tmp_path):
    root,config,conn=setup(tmp_path);conn.close()
    config.db_path.write_bytes(b'truncated SQLite data')
    with pytest.raises(sqlite3.DatabaseError):
        semantic.connect(config)
    assert config.db_path.read_bytes() == b'truncated SQLite data'
    # Disposable owned DB only: operator explicitly removes corruption to rebuild.
    config.db_path.unlink()
    conn=semantic.connect(config)
    assert semantic.refresh(conn,config,embed).embedded == ['a.md']
    conn.close()


def test_failed_batch_keeps_old_note_and_persistent_pending(tmp_path):
    root,config,conn=setup(tmp_path)
    old=semantic.maintenance_status(conn,config)['indexed_revisions']['a.md']
    (root/'a.md').write_text('Changed note.\n'*100)
    calls=0
    def fail(texts):
        nonlocal calls
        calls+=1
        if calls == 2:
            raise ConnectionError('injected batch failure')
        return embed(texts)
    config=semantic.Config(root,config.db_path,'fake',400,'http://unused',batch_chunks=1)
    with pytest.raises(ConnectionError):
        semantic.refresh(conn,config,fail,force_hash=True)
    status=semantic.maintenance_status(conn,config)
    assert status['pending'] == ['a.md'] and 'injected' in status['last_error']
    assert status['indexed_revisions']['a.md'] == old
    conn.close();conn=semantic.connect(config)
    assert semantic.maintenance_status(conn,config)['pending'] == ['a.md']
    assert semantic.refresh(conn,config,embed,force_hash=True).embedded == ['a.md']
    assert semantic.maintenance_status(conn,config)['last_error'] is None
    conn.close()


def test_edit_during_embedding_does_not_publish_stale_revision(tmp_path):
    root,config,conn=setup(tmp_path)
    old=semantic.maintenance_status(conn,config)['indexed_revisions']['a.md']
    (root/'a.md').write_text('First edit.\n')
    def concurrent(texts):
        (root/'a.md').write_text('Second edit while inference runs.\n')
        return embed(texts)
    with pytest.raises(ValueError,match='changed during embedding'):
        semantic.refresh(conn,config,concurrent,force_hash=True)
    status=semantic.maintenance_status(conn,config)
    assert status['indexed_revisions']['a.md'] == old and status['pending'] == ['a.md']
    assert 'changed during embedding' in status['last_error']
    semantic.refresh(conn,config,embed,force_hash=True)
    assert semantic.maintenance_status(conn,config)['indexed_revisions']['a.md'] == hashlib.sha256((root/'a.md').read_bytes()).hexdigest()
    conn.close()


def test_unrelated_database_not_modified(tmp_path):
    config=semantic.Config(tmp_path,tmp_path/'unrelated.db','fake',400,'http://unused')
    conn=sqlite3.connect(config.db_path);conn.execute('CREATE TABLE unrelated (data TEXT)');conn.commit();conn.close()
    before=config.db_path.read_bytes()
    with pytest.raises(ValueError,match='unrelated'):
        semantic.connect(config)
    assert config.db_path.read_bytes() == before


def test_index_cannot_be_reused_for_another_vault(tmp_path):
    from dataclasses import replace
    root,config,conn=setup(tmp_path)
    other=tmp_path/'another';other.mkdir();(other/'b.md').write_text('Other vault\n')
    before=semantic.maintenance_status(conn,config)['indexed_revisions']
    with pytest.raises(ValueError,match='different vault root'):
        semantic.refresh(conn,replace(config,vault=other),embed)
    assert semantic.maintenance_status(conn,config)['indexed_revisions'] == before
    conn.close()
