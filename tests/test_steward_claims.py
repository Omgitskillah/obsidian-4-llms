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


# ---------------------------------------------------------------- F13: refusals
import pytest
from obsidian_mcp.steward_claims import finite


@pytest.mark.parametrize('value, message', [(True, 'Boolean is not a numerical claim'), ('ten', 'Invalid numerical claim'),
                                            ('NaN', 'Nonfinite numerical claim'), ('Infinity', 'Nonfinite numerical claim')])
def test_a_value_that_is_not_a_finite_number_is_refused(value, message):
    with pytest.raises(ValueError, match=message):
        finite(value)


@pytest.mark.parametrize('fields, kwargs, message', [
    ([], {}, 'Claim fields must be declared'),
    (FIELDS, {'max_pairs': 0}, 'max_pairs 1..5000'),
    ({'duration': {'units': 'ms'}}, {}, 'explicit unit factor/offset mappings'),
    ({'duration': {'units': {}, 'extra': 1}}, {}, 'explicit unit factor/offset mappings'),
    ({'duration': {'units': {}, 'tolerance': '-1'}}, {}, 'Negative tolerance'),
    ({'duration': {'units': {'ms': {'offset': '1'}}}}, {}, 'positive factor'),
    ({'duration': {'units': {'ms': {'factor': '0'}}}}, {}, 'positive factor'),
    ({'duration': {'units': {'ms': {'factor': '1', 'offset': 'x'}}}}, {}, 'Invalid numerical claim'),
])
def test_a_field_declaration_outside_the_known_shape_is_refused(tmp_path, fields, kwargs, message):
    with pytest.raises(ValueError, match=message):
        contradictions(Snapshot(tmp_path).refresh(), fields, **kwargs)


@pytest.mark.parametrize('claims, reason', [
    ('one claim', 'claims_must_be_list'),
    (['not a mapping'], 'Invalid claim keys'),
    ([{'entity': 'board-A', 'colour': 'red'}], 'Invalid claim keys'),
    ([{'entity': 'board-A', 'field': 'duration', 'value': '1', 'unit': 'ms', 'scope': '', 'date': '2026-10-08', 'kind': 'measurement'}],
     'Claims require entity/field/unit/scope/date/kind strings'),
    ([{'entity': 'board-A', 'field': 'duration', 'value': '1', 'unit': 'ms', 'scope': 'idle', 'date': 'soon', 'kind': 'measurement'}],
     'Invalid claim date/kind/status'),
    ([{'entity': 'board-A', 'field': 'duration', 'value': '1', 'unit': 'ms', 'scope': 'idle', 'date': '2026-10-08', 'kind': 'rumour'}],
     'Invalid claim date/kind/status'),
    ([{'entity': 'board-A', 'field': 'duration', 'value': '1', 'unit': 'ms', 'scope': 'idle', 'date': '2026-10-08', 'kind': 'measurement',
       'status': 'retracted'}], 'Invalid claim date/kind/status'),
    ([{'entity': 'board-A', 'field': 'duration', 'value': '1', 'unit': 'ms', 'scope': 'idle', 'date': '2026-10-08', 'kind': 'measurement',
       'qualifier': 3}], 'Qualifier must be a literal string'),
    ([{'entity': 'board-A', 'field': 'weight', 'value': '1', 'unit': 'kg', 'scope': 'idle', 'date': '2026-10-08', 'kind': 'measurement'}],
     'Undeclared field or unit'),
])
def test_a_malformed_claim_is_a_defect_finding_not_a_crash(tmp_path, claims, reason):
    (tmp_path / 'a.md').write_text('---\n' + yaml.safe_dump({'claims': claims}) + '---\n')
    report = contradictions(Snapshot(tmp_path).refresh(), FIELDS)
    (row,) = report['findings']
    assert row['rule'] == 'claim.invalid' and row['severity'] == 'defect'
    assert (row['evidence'].get('reason')) == reason


def test_a_note_with_broken_frontmatter_offers_no_claims(tmp_path):
    make(tmp_path, 'a.md')
    (tmp_path / 'b.md').write_text('---\nclaims: []\nclaims: []\n---\n')
    report = contradictions(Snapshot(tmp_path).refresh(), FIELDS)
    assert report['findings'] == [] and report['comparisons'] == []
