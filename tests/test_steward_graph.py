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
