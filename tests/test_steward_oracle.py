"""Every stewardship package at once, against an explicit oracle: a small vault whose every finding,
duplicate group, graph edge, route and table total is written down (steward_oracle.json, from
Nuru's bench, nuru-bench vault_steward/steward_fixture.json). The run reads and never writes."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from obsidian_mcp.steward import Steward

FIXTURE = json.loads(Path(__file__).with_name('steward_oracle.json').read_text(encoding='utf-8'))


def _hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_every_package_finds_exactly_what_the_oracle_lists_and_changes_nothing(tmp_path):
    for path, content in FIXTURE['files'].items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content, encoding='utf-8')
    before = _hashes(tmp_path)
    report = Steward(tmp_path, FIXTURE['config']).run_once(limit=100, include_details=True)
    oracle, details = FIXTURE['oracle'], report['details']
    found = Counter((r['namespace'], r['source'], r['rule'], r['line']) for r in report['findings'])
    assert found == Counter(tuple(r) for r in oracle['findings'])
    assert [r['paths'] for r in details['duplicates']['exact_groups']] == oracle['exact_note_groups']
    assert [r['paths'] for r in details['attachments']['duplicate_groups']] == oracle['asset_duplicate_groups']
    assert details['attachments']['potential_duplicate_bytes'] == oracle['potential_duplicate_bytes']
    assert [[r['source'], r['target']] for r in details['graph']['edges']] == oracle['graph_edges']
    assert details['triage']['queue'][0]['destination'] == oracle['route']
    total = next(r for r in report['findings'] if r['rule'] == 'table.total')
    assert total['evidence']['expected'] == oracle['table_expected_total']
    assert report['complete'] and report['model_calls'] == 0
    assert _hashes(tmp_path) == before
