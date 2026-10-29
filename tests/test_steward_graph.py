from obsidian_mcp.stewardship import Snapshot
from obsidian_mcp.steward_graph import knowledge_map


def test_graph_and_navigation(tmp_path):
    (tmp_path / 'a.md').write_text('---\nscope: private\ntags: [radio]\n---\n[[b]]\n[[missing]]\n')
    (tmp_path / 'b.md').write_text('---\nscope: private\ntype: project\n---\n')
    (tmp_path / 'c.md').write_text('---\nscope: private\n---\nAn intentionally isolated note.\n')
    report = knowledge_map(Snapshot(tmp_path).refresh(), target='b.md')
    assert report['incoming'] == ['a.md']
    assert report['edge_count'] == 1
    assert report['components'] == [['a.md', 'b.md'], ['c.md']]
    assert report['isolated_notes'] == ['c.md']
    assert not report['findings']
    assert next(i for i in report['navigation'] if i['kind'] == 'tag')['notes'] == ['a.md']


def test_boundaries_and_accepted_link(tmp_path):
    (tmp_path / 'a.md').write_text('---\nscope: private\n---\n[[b]]\n')
    (tmp_path / 'b.md').write_text('---\nscope: shared\n---\n')
    scan = Snapshot(tmp_path).refresh()
    report = knowledge_map(scan)
    assert report['boundary_edge_count'] == 1
    assert report['components'] == [['a.md'], ['b.md']]
    filtered = knowledge_map(scan, scopes=['private'])
    assert filtered['node_count'] == 1 and not filtered['edges']
    (tmp_path / 'b.md').write_text('---\nscope: private\n---\n[[a]]\n')
    assert len(knowledge_map(scan.refresh())['components']) == 1


import pytest


@pytest.mark.parametrize('kwargs, message', [({'scopes': 'private'}, 'scopes must be a list'), ({'scopes': [1]}, 'scopes must be a list'),
                                             ({'limit': 0}, 'Graph page limit must be 1..1000'), ({'limit': 1001}, 'Graph page limit')])
def test_graph_options_outside_their_bounds_are_refused(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        knowledge_map(Snapshot(tmp_path).refresh(), **kwargs)


def test_a_cycle_of_links_is_one_component(tmp_path):
    for name, target in (('a', 'b'), ('b', 'c'), ('c', 'a')):
        (tmp_path / f'{name}.md').write_text(f'[[{target}]]')
    (tmp_path / 'lone.md').write_text('no links')
    components = knowledge_map(Snapshot(tmp_path).refresh())['components']
    assert sorted(len(c['paths']) if isinstance(c, dict) else len(c) for c in components) == [1, 3]
