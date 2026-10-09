import yaml
from obsidian_mcp.stewardship import Snapshot
from obsidian_mcp.steward_claims import contradictions


FIELDS = {'duration': {'units': {'ms': {'factor': '1'}, 's': {'factor': '1000'}}, 'tolerance': '0'}}


def make(root, name, **changes):
    raw = {'entity': 'board-A', 'field': 'duration', 'value': '1000', 'unit': 'ms', 'scope': 'idle',
           'date': '2026-10-08', 'kind': 'measurement', **changes}
    (root / name).write_text('---\n' + yaml.safe_dump({'claims': [raw]}) + '---\n')


def test_units_equivalent_and_conflict(tmp_path):
    make(tmp_path, 'a.md')
    make(tmp_path, 'b.md', unit='s', value='1')
    scan = Snapshot(tmp_path).refresh()
    assert contradictions(scan, FIELDS)['comparisons'][0]['classification'] == 'equivalent'
    make(tmp_path, 'b.md', unit='s', value='2')
    report = contradictions(scan.refresh(), FIELDS)
    assert report['counts'] == {'claim.conflict': 1}
    assert report['findings'][0]['evidence']['right']['normalized'] == '2000'


def test_scopes_dates_estimates_and_supersession(tmp_path):
    make(tmp_path, 'base.md')
    for name, change in [('scope', {'scope': 'active'}), ('date', {'date': '2026-10-09'}), ('kind', {'kind': 'estimate'}),
                         ('condition', {'qualifier': 'if offline'}), ('old', {'status': 'superseded'})]:
        make(tmp_path, f'{name}.md', value='99', **change)
    report = contradictions(Snapshot(tmp_path).refresh(), FIELDS)
    assert not report['findings']
    assert {'different_scope', 'different_date', 'different_claim_kind', 'different_condition', 'explicitly_superseded'} <= {p['classification'] for p in report['comparisons']}


def test_invalid_units_and_candidate_bound(tmp_path):
    make(tmp_path, 'bad.md', unit='minutes')
    make(tmp_path, 'a.md')
    make(tmp_path, 'b.md')
    make(tmp_path, 'c.md')
    report = contradictions(Snapshot(tmp_path).refresh(), FIELDS, max_pairs=1)
    assert report['counts'] == {'claim.invalid': 1}
    assert report['candidate_pairs_examined'] == 1 and report['comparisons_truncated']
