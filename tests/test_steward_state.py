import sqlite3
import pytest

from obsidian_mcp.steward import Steward
from obsidian_mcp.steward_state import State


CONFIG = {'packages': ['properties'], 'schemas': [{'name': 'n', 'fields': {'owner': {'type': 'string', 'required': True}}}]}


def test_restart_rename_review_and_resolve(tmp_path):
    file = tmp_path / 'a.md'
    file.write_text('A note without metadata.')
    db = tmp_path / '.steward.sqlite3'
    first = Steward(tmp_path, CONFIG, state_path=db).run_once()
    row = first['findings'][0]
    state = State(db, Steward(tmp_path, CONFIG).snapshot)
    state.review(row['id'], row['evidence_revision'], 'suppressed')
    state.close()
    restarted = Steward(tmp_path, CONFIG, state_path=db)
    assert restarted.run_once()['findings'][0]['status'] == 'suppressed'
    file.rename(tmp_path / 'b.md')
    moved = restarted.run_once()['findings'][0]
    assert moved['id'] == row['id'] and moved['status'] == 'suppressed'
    (tmp_path / 'b.md').write_text('---\nowner: User\n---\n')
    final = restarted.run_once()
    assert not final['findings'] and final['review_states'] == {'resolved': 1}


def test_changed_evidence_and_invalid_review(tmp_path):
    (tmp_path / 'a.md').write_text('text')
    db = tmp_path / '.state.sqlite3'
    runner = Steward(tmp_path, CONFIG, state_path=db)
    row = runner.run_once()['findings'][0]
    state = State(db, runner.snapshot)
    with pytest.raises(ValueError, match='changed'):
        state.review(row['id'], 'stale', 'suppressed')
    state.review(row['id'], row['evidence_revision'], 'acknowledged')
    state.close()
    (tmp_path / 'a.md').write_text('changed')
    assert runner.run_once()['findings'][0]['status'] == 'open'


def test_state_cannot_overwrite_authored_or_unrelated_database(tmp_path):
    (tmp_path / 'a.md').write_text('text')
    runner = Steward(tmp_path, CONFIG)
    with pytest.raises(ValueError, match='excluded'):
        State(tmp_path / 'notes.sqlite3', runner.snapshot)
    db = tmp_path / '.other.sqlite3'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE unrelated(x)')
    conn.close()
    with pytest.raises(ValueError, match='unrelated'):
        State(db, runner.snapshot)


def test_package_validation_and_pagination(tmp_path):
    with pytest.raises(ValueError):
        Steward(tmp_path, {'packages': ['unknown']})
    for i in range(3):
        (tmp_path / f'{i}.md').write_text('text')
    runner = Steward(tmp_path, CONFIG)
    first = runner.run_once(limit=1)
    second = runner.run_once(limit=1, offset=1)
    assert first['snapshot'] == second['snapshot']
    assert first['next_offset'] == 1 and second['next_offset'] == 2
    assert first['finding_count'] == 3


def test_invalid_frontmatter_marks_incomplete_and_does_not_resolve(tmp_path):
    file = tmp_path / 'a.md'
    file.write_text('text')
    runner = Steward(tmp_path, CONFIG, state_path=tmp_path / '.state.sqlite3')
    row = runner.run_once()['findings'][0]
    file.write_text('---\nowner: [\n---\n')
    report = runner.run_once()
    assert not report['complete']
    state = State(tmp_path / '.state.sqlite3', runner.snapshot)
    assert state.conn.execute('SELECT status FROM findings WHERE id=?', (row['id'],)).fetchone()[0] == 'open'
    state.close()


def test_readonly_index_status_never_embeds(tmp_path):
    from obsidian_mcp import semantic
    (tmp_path / 'a.md').write_text('some text')
    env = {'OBSIDIAN_SEMANTIC_DB': str(tmp_path / '.semantic.sqlite3')}
    cfg = semantic.load_config(tmp_path, env)
    conn = semantic.connect(cfg)
    semantic.refresh(conn, cfg, lambda texts: [[1., 0.] for _ in texts])
    conn.close()
    runner = Steward(tmp_path, {'packages': ['index'], 'index_env': env})
    report = runner.run_once(include_details=True)
    assert report['details']['index']['index_status']['pending_count'] == 0
    assert report['model_calls'] == 0


def test_mcp_configured_and_unconfigured_readonly(tmp_path, monkeypatch):
    import json
    from obsidian_mcp import server
    monkeypatch.setattr(server, 'VAULT_PATH', tmp_path)
    monkeypatch.delenv('OBSIDIAN_STEWARD_CONFIG', raising=False)
    assert server.audit_stewardship()['state'] == 'not_configured'
    config = tmp_path / '.config.json'
    config.write_text(json.dumps(CONFIG))
    (tmp_path / 'a.md').write_text('text')
    monkeypatch.setenv('OBSIDIAN_STEWARD_CONFIG', str(config))
    report = server.audit_stewardship()
    assert report['finding_count'] == 1 and report['model_calls'] == 0
    assert not list(tmp_path.glob('*.sqlite3'))


def test_two_identical_renames_keep_inode_identities(tmp_path):
    for name in ('a.md', 'b.md'):
        (tmp_path / name).write_text('same bytes')
    runner = Steward(tmp_path, CONFIG, state_path=tmp_path / '.state.sqlite3')
    original = {f['source']: f['id'] for f in runner.run_once()['findings']}
    (tmp_path / 'a.md').rename(tmp_path / 'c.md')
    (tmp_path / 'b.md').rename(tmp_path / 'd.md')
    moved = {f['source']: f['id'] for f in runner.run_once()['findings']}
    assert moved == {'c.md': original['a.md'], 'd.md': original['b.md']}


def test_empty_existing_database_not_adopted(tmp_path):
    path = tmp_path / '.empty.sqlite3'
    path.write_bytes(b'')
    with pytest.raises(ValueError, match='unrelated'):
        State(path, Steward(tmp_path, CONFIG).snapshot)
    assert path.read_bytes() == b''


def test_state_alias_is_canonicalized_before_authored_path_check(tmp_path):
    runner = Steward(tmp_path, CONFIG)
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='excluded'):
        State(alias / 'notes.sqlite3', runner.snapshot)
    state = State(alias / '.state.sqlite3', runner.snapshot)
    assert state.path == tmp_path / '.state.sqlite3'
    state.close()


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_state_sidecar_symlink_cannot_write_outside_database(tmp_path, suffix):
    runner = Steward(tmp_path, CONFIG)
    outside = tmp_path / 'preserve'
    outside.write_bytes(b'preserve me')
    (tmp_path / ('.state.sqlite3' + suffix)).symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        State(tmp_path / '.state.sqlite3', runner.snapshot)
    assert outside.read_bytes() == b'preserve me'


# ---------------------------------------------------------------- F13: refusals
import os
import sys


def test_a_state_file_must_be_an_sqlite_file(tmp_path):
    with pytest.raises(ValueError, match=r'must end in \.sqlite3 or \.db'):
        State(tmp_path / 'state.json', Steward(tmp_path, CONFIG).snapshot)


def test_a_state_database_from_another_vault_is_refused(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    db = tmp_path / 'state.sqlite3'
    State(db, Steward(first, CONFIG).snapshot).close()
    with pytest.raises(ValueError, match='belongs to another vault'):
        State(db, Steward(second, CONFIG).snapshot)


def test_an_unknown_review_state_is_refused(tmp_path):
    state = State(tmp_path / 'state.sqlite3', Steward(tmp_path / 'vault', CONFIG).snapshot)
    try:
        with pytest.raises(ValueError, match='acknowledged, suppressed, or open'):
            state.review('x', 'y', 'deleted')
    finally:
        state.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='creating symlinks needs privileges on Windows')
def test_a_symlink_slipped_in_beside_the_resolved_path_is_refused(tmp_path, monkeypatch):
    """The first check sees the path as named; this one, the path it really leads to."""
    db, elsewhere = tmp_path / 'state.sqlite3', tmp_path / 'elsewhere'
    elsewhere.write_text('')
    real_resolve = type(db).resolve
    def resolve_then_race(self, *args, **kwargs):
        resolved = real_resolve(self, *args, **kwargs)
        if resolved == real_resolve(db) and not os.path.lexists(str(resolved) + '-wal'):
            os.symlink(elsewhere, str(resolved) + '-wal')
        return resolved
    monkeypatch.setattr(type(db), 'resolve', resolve_then_race)
    with pytest.raises(ValueError, match='cannot contain symlinks'):
        State(db, Steward(tmp_path / 'vault', CONFIG).snapshot)


def test_two_identical_new_notes_never_inherit_a_removed_notes_identity(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'a.md').write_text('Same text.')
    db = tmp_path / 'state.sqlite3'
    runner = Steward(vault, CONFIG, state_path=db)
    first = runner.run_once()['findings'][0]
    # Moved out of the vault, not deleted: its inode stays in use, so neither new note can be given it
    # and match the old note's identity hint. Only the content is shared.
    (vault / 'a.md').rename(tmp_path / 'a.md')
    (vault / 'b.md').write_text('Same text.')
    (vault / 'c.md').write_text('Same text.')
    report = runner.run_once()
    hints = {runner.snapshot.documents[p].identity_hint for p in ('b.md', 'c.md')}
    assert len(hints) == 2 and f"{(tmp_path / 'a.md').stat().st_dev}:{(tmp_path / 'a.md').stat().st_ino}" not in hints
    warnings = [d for d in report['diagnostics'] if d.get('reason') == 'ambiguous_rename_identity']
    assert sorted((w['path'], w['candidates']) for w in warnings) == [('b.md', ['a.md']), ('c.md', ['a.md'])]
    assert len(report['findings']) == 2 and first['status'] == 'open'
