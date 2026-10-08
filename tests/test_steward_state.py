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
