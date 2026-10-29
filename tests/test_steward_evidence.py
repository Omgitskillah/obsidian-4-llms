import pytest

from obsidian_mcp.stewardship import Snapshot
from obsidian_mcp.steward_evidence import prepare


def scan(tmp_path, notes):
    for path, text in notes.items():
        (tmp_path / path).write_text(text)
    return Snapshot(tmp_path).refresh()


def test_link_examples_are_not_targets(tmp_path):
    s = scan(tmp_path, {'a.md': '[[Missing]]\n```\n[[Fake]]\n```\n<!-- [[Comment]] -->\n'})
    row = prepare(s, 'links', ['a.md'], {})
    assert [r['quote'] for r in row['evidence']] == ['[[Missing]]']
    assert row['allowed_proposals'] == [{'type': 'review', 'values': {}}]


def test_all_task_mirrors_included_and_scope_guarded(tmp_path):
    header = '---\nproject: Pine\nscope: private\n---\n'
    s = scan(tmp_path, {'a.md': header+'- [ ] Record [task:x]\n', 'b.md': header+'- [x] Record [task:x]\n'})
    row = prepare(s, 'tasks', ['a.md'], {})
    assert {r['status'] for r in row['facts']['occurrences']} == {'open', 'completed'}
    assert len(row['sources']) == 2 and 'supported' not in row['allowed_decisions']
    (tmp_path/'b.md').write_text(header.replace('private', 'shared')+'- [x] Record [task:x]\n')
    with pytest.raises(ValueError, match='scope'):
        prepare(s.refresh(), 'tasks', ['a.md'], {})


def test_code_owns_total_and_clean_control(tmp_path):
    s = scan(tmp_path, {'a.md': '| Item | Cost |\n|---|---|\n| Cable | €12.25 |\n| Plug | €5.25 |\n| Total | €20.00 |\n'})
    config = {'tables': [{'name': 'costs', 'columns': {'Item': {'type': 'string'}, 'Cost': {'type': 'number', 'currency': '€'}},
                          'totals': [{'label_column': 'Item', 'label': 'Total', 'column': 'Cost'}]}]}
    row = prepare(s, 'tables', ['a.md'], config)
    assert row['allowed_proposals'][1]['values']['expected'] == '17.50'
    assert row['allowed_proposals'][1]['values']['authored'] == '20.00'
    (tmp_path/'a.md').write_text((tmp_path/'a.md').read_text().replace('20.00', '17.50'))
    clean = prepare(s.refresh(), 'tables', ['a.md'], config)
    assert clean['facts']['findings'] == []


def test_attachment_no_absence_or_delete_permission(tmp_path):
    s = scan(tmp_path, {'a.md': '![[pixel.png]]\n'})
    (tmp_path/'pixel.png').write_bytes(b'image')
    row = prepare(s.refresh(), 'attachments', ['a.md'], {})
    assert row['facts']['assets'][0]['ownership'] == 'referenced'
    assert row['facts']['deletion_eligible'] is False
    assert 'plugin' in ' '.join(row['limitations'])


def test_graph_navigation_does_not_assert_dependency(tmp_path):
    s = scan(tmp_path, {'a.md': '[[b]]\n', 'b.md': 'Calibration\n'})
    row = prepare(s, 'graph', ['a.md'], {})
    assert row['facts']['edges'][0]['kind'] == 'navigation'
    assert row['facts']['asserted_dependencies'] == []


def test_unit_equivalence_and_conditions(tmp_path):
    import json
    def note(value, unit, qualifier=''):
        return '---\n'+json.dumps({'scope':'private','claims':[{'entity':'p','field':'duration','value':value,'unit':unit,'scope':'lab','date':'2026-10-09','kind':'measurement','qualifier':qualifier}]})+'\n---\nTiming\n'
    config = {'claims': {'duration': {'units': {'s': {'factor':'1000'}, 'ms': {'factor':'1'}}}}}
    s = scan(tmp_path, {'a.md':note('2.5','s'), 'b.md':note('2500','ms')})
    row = prepare(s,'claims',['a.md','b.md'],config)
    assert row['facts']['comparisons'][0]['classification'] == 'equivalent'
    assert 'supported' in row['allowed_decisions']
    (tmp_path/'b.md').write_text(note('2500','ms','hot'))
    assert 'supported' not in prepare(s.refresh(),'claims',['a.md','b.md'],config)['allowed_decisions']


def test_incomplete_task_coverage_abstains(tmp_path):
    s = scan(tmp_path, {'a.md':'- [ ] T [task:x]\n'})
    s.diagnostics.append({'path':'unreadable.md','reason':'read_failure'})
    with pytest.raises(ValueError,match='coverage'):
        prepare(s,'tasks',['a.md'],{})


# ---------------------------------------------------------------- F13: abstentions and bounds
def test_two_identical_notes_are_recorded_as_exactly_equal_but_no_merge_is_approved(tmp_path):
    s = scan(tmp_path, {'a.md': '---\nscope: private\n---\nSame words.\n', 'b.md': '---\nscope: private\n---\nSame words.\n'})
    row = prepare(s, 'duplicates', ['a.md', 'b.md'], {})
    assert row['facts']['exact_bytes_equal'] is True and 'supported' in row['allowed_decisions']
    assert {'type': 'compare', 'values': {}} in row['allowed_proposals']
    assert 'no merge or deletion is approved' in ' '.join(row['factual_statements'])


def test_an_ambiguous_link_offers_each_target_in_the_same_scope_and_no_other(tmp_path):
    for folder in ('x', 'y', 'z'):
        (tmp_path / folder).mkdir()
    private = '---\nscope: private\n---\n'
    s = scan(tmp_path, {'a.md': private + '[[target]]\n', 'x/target.md': private + 'The one in x.\n',
                        'y/target.md': private + 'The one in y.\n', 'z/target.md': '---\nscope: shared\n---\nShared.\n'})
    row = prepare(s, 'links', ['a.md'], {})
    targets = {p['values']['target'] for p in row['allowed_proposals'] if p['type'] == 'replacement_target'}
    assert targets == {'x/target.md', 'y/target.md'}
    assert sorted(r['source'] for r in row['evidence'] if r['classification'] == 'eligible_target') == ['x/target.md', 'y/target.md']


def test_an_ambiguous_link_between_notes_with_no_declared_scope_abstains(tmp_path):
    """An empty target gives nothing to quote; the undeclared scopes then stop preparation."""
    (tmp_path / 'x').mkdir()
    (tmp_path / 'y').mkdir()
    s = scan(tmp_path, {'a.md': '[[target]]\n', 'x/target.md': 'The one in x.\n', 'y/target.md': ''})
    with pytest.raises(ValueError, match='undeclared or differing scope boundaries'):
        prepare(s, 'links', ['a.md'], {})


def test_preparation_abstains_without_evidence_or_past_its_bounds(tmp_path, monkeypatch):
    from obsidian_mcp import steward_evidence
    s = scan(tmp_path, {'empty.md': '', **{f'n{i}.md': f'note {i}\n' for i in range(21)}})
    with pytest.raises(ValueError, match='No eligible evidence; preparation abstains'):
        prepare(s, 'properties', ['empty.md'], {})
    with pytest.raises(ValueError, match='Evidence bound exceeded'):
        prepare(s, 'properties', [f'n{i}.md' for i in range(21)], {})
    monkeypatch.setattr(steward_evidence, 'MAX_BYTES', 10)
    with pytest.raises(ValueError, match='Evidence byte bound exceeded'):
        prepare(s, 'properties', ['n1.md'], {})


def test_notes_with_broken_frontmatter_are_not_evidence_even_if_the_scan_lost_its_diagnostic(tmp_path):
    """Every frontmatter issue is also a scan diagnostic, refused first; the issue check is the backstop."""
    s = scan(tmp_path, {'a.md': '---\na: 1\na: 2\n---\nBody.\n'})
    with pytest.raises(ValueError, match='Incomplete or changed evidence coverage'):
        prepare(s, 'properties', ['a.md'], {})
    s.diagnostics = []
    with pytest.raises(ValueError, match='Invalid evidence frontmatter'):
        prepare(s, 'properties', ['a.md'], {})
