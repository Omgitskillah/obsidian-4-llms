"""F13: the stewardship runner's limits, refusals and command line (obsidian_mcp.steward)."""
import json
import runpy
import sys

import pytest

import obsidian_mcp
from obsidian_mcp import steward
from obsidian_mcp.link_integrity import AuditCancelled
from obsidian_mcp.steward import Steward, bounded

CONFIG = {'packages': ['properties'], 'schemas': [{'name': 'n', 'fields': {'owner': {'type': 'string', 'required': True}}}]}


def test_evidence_is_bounded_in_depth_length_items_and_keys():
    deep = current = {}
    for _ in range(12):
        current['next'] = current = {}
    shown = bounded(deep)
    for _ in range(9):
        shown = shown['next']
    assert shown == {'truncated': True, 'reason': 'nesting_limit'}
    assert bounded('x' * 2001) == {'excerpt': 'x' * 2000, 'truncated': True, 'characters': 2001}
    assert bounded(list(range(25))) == {'items': list(range(20)), 'truncated': True, 'count': 25}
    assert bounded(list(range(3))) == [0, 1, 2]
    many = bounded({f'k{i}': i for i in range(45)})
    assert len(many) == 41 and many['_truncated_keys'] == 5


@pytest.mark.parametrize('config, message', [
    ([], 'Invalid stewardship configuration keys'),
    ({**CONFIG, 'model': 'x'}, 'Invalid stewardship configuration keys'),
    ({'packages': 'properties'}, 'packages must explicitly select known feature names'),
    ({'packages': ['properties', 'telepathy']}, 'packages must explicitly select known feature names'),
    ({'packages': ['properties', 'properties']}, 'Duplicate package selection'),
    ({**CONFIG, 'policy': {'exclusions': 'drafts/'}}, 'string-list exclusions override'),
    ({**CONFIG, 'policy': {'include': []}}, 'string-list exclusions override'),
    ({**CONFIG, 'policy': {'exclusions': [3]}}, 'string-list exclusions override'),
])
def test_a_configuration_outside_the_known_shape_is_refused(tmp_path, config, message):
    with pytest.raises(ValueError, match=message):
        Steward(tmp_path, config)


@pytest.mark.parametrize('limit, offset', [(0, 0), (101, 0), (10, -1), (True, 0), (10, 1.0)])
def test_finding_pages_must_be_bounded(tmp_path, limit, offset):
    with pytest.raises(ValueError, match='limit 1..100 and nonnegative offset'):
        Steward(tmp_path, CONFIG).run_once(limit=limit, offset=offset)


def test_an_exclusions_policy_keeps_those_notes_out(tmp_path):
    (tmp_path / 'drafts').mkdir()
    (tmp_path / 'drafts' / 'a.md').write_text('No owner.')
    (tmp_path / 'b.md').write_text('No owner either.')
    report = Steward(tmp_path, {**CONFIG, 'policy': {'exclusions': ['drafts/']}}).run_once()
    assert [f['source'] for f in report['findings']] == ['b.md']


def test_an_index_not_built_yet_is_reported_as_incomplete(tmp_path):
    report = Steward(tmp_path, {'packages': ['index'], 'index_env': {'OBSIDIAN_SEMANTIC_DB': str(tmp_path / 'none.sqlite3')}}
                     ).run_once(include_details=True)
    assert report['details']['index']['index_status'] == {'state': 'not_built'} and report['complete'] is False


@pytest.mark.parametrize('when', ['after the scan', 'during the last package'])
def test_a_stop_asked_for_during_a_pass_publishes_nothing(tmp_path, monkeypatch, when):
    (tmp_path / 'a.md').write_text('No owner.')
    stopped, db = [], tmp_path / 'state.sqlite3'
    runner = Steward(tmp_path, CONFIG, state_path=db, should_stop=lambda: bool(stopped))
    if when == 'after the scan':  # before any package runs
        scan = runner.snapshot.refresh
        runner.snapshot.refresh = lambda: (scan(), stopped.append(True))[0]
    else:  # the packages are done; nothing has been recorded yet
        real = steward.properties
        monkeypatch.setattr(steward, 'properties', lambda *args: (real(*args), stopped.append(True))[0])
    with pytest.raises(AuditCancelled):
        runner.run_once()
    assert not db.exists()


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, 'argv', ['obsidian-steward', *map(str, argv)])
    steward.main()
    return json.loads(capsys.readouterr().out)


def test_the_command_reports_and_records_a_review(tmp_path, monkeypatch, capsys):
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'a.md').write_text('No owner.')
    config, db = tmp_path / 'config.json', tmp_path / 'state.sqlite3'
    config.write_text(json.dumps(CONFIG))
    first = run_cli(monkeypatch, capsys, vault, '--config', config, '--state', db, '--limit', 5)
    (row,) = first['findings']
    assert row['status'] == 'open' and first['model_calls'] == 0
    reviewed = run_cli(monkeypatch, capsys, vault, '--config', config, '--state', db, '--review-id', row['id'],
                       '--evidence-revision', row['evidence_revision'], '--review-status', 'acknowledged', '--details')
    assert reviewed['findings'][0]['status'] == 'acknowledged'


def test_a_review_without_all_its_arguments_is_refused(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(CONFIG))
    monkeypatch.setattr(sys, 'argv', ['obsidian-steward', str(tmp_path), '--config', str(config), '--review-id', 'x'])
    with pytest.raises(SystemExit):
        steward.main()
    assert 'Review requires --state, --review-id, --evidence-revision and --review-status' in capsys.readouterr().err


def test_the_module_runs_as_a_command(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'packages': []}))
    monkeypatch.setattr(sys, 'argv', ['obsidian-steward', str(tmp_path), '--config', str(config)])
    monkeypatch.delitem(sys.modules, 'obsidian_mcp.steward')  # a fresh __main__, as `python -m` runs it
    runpy.run_module('obsidian_mcp.steward', run_name='__main__')
    assert json.loads(capsys.readouterr().out)['finding_count'] == 0


def test_the_package_entry_point_starts_the_notes_server(monkeypatch):
    from obsidian_mcp import server
    started = []
    monkeypatch.setattr(server, 'main', lambda: started.append(True))
    obsidian_mcp.main()
    assert started == [True]
