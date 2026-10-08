import json

from obsidian_mcp.stewardship import Snapshot, attachments, asset_hash


def test_assets_and_owners(tmp_path):
    (tmp_path / 'a.png').write_bytes(b'ABC')
    (tmp_path / 'b.png').write_bytes(b'ABC')
    (tmp_path / 'c.png').write_bytes(b'XYZ')
    (tmp_path / 'n.md').write_text('---\ncover: "[[b.png]]"\n---\n![[a.png]]\n![[missing.png]]\n')
    (tmp_path / 'map.canvas').write_text(json.dumps({'nodes': [{'id': '1', 'type': 'file', 'file': 'c.png'}]}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    report = attachments(Snapshot(tmp_path).refresh(), large_bytes=3, reference_properties=('cover',))
    inventory = {r['path']: r for r in report['inventory']}
    assert all(inventory[p]['ownership'] == 'referenced' for p in ('a.png', 'b.png', 'c.png'))
    assert inventory['map.canvas']['ownership'] == 'unknown'
    assert all(not r['deletion_eligible'] for r in inventory.values())
    assert report['duplicate_groups'][0]['paths'] == ['a.png', 'b.png']
    assert report['potential_duplicate_bytes'] == 3
    assert report['counts'] == {'attachment.missing_embed': 1}
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_large_hash_and_unknown_ownership(tmp_path):
    file = tmp_path / 'huge.dat'
    file.write_bytes(b'a' * (3 * 1024 * 1024 + 17))
    revision, size = asset_hash(tmp_path, file)
    assert size == 3 * 1024 * 1024 + 17
    report = attachments(Snapshot(tmp_path).refresh(), large_bytes=1)
    assert report['inventory'][0]['revision'] == revision
    assert report['inventory'][0]['large']
    assert report['inventory'][0]['ownership'] == 'unknown'


def test_changed_duplicate_and_invalid_canvas(tmp_path):
    (tmp_path / 'a.dat').write_bytes(b'abc')
    (tmp_path / 'b.dat').write_bytes(b'abc')
    (tmp_path / 'map.canvas').write_text('{bad')
    scan = Snapshot(tmp_path).refresh()
    first = attachments(scan)
    assert not first['complete']
    assert len(first['duplicate_groups']) == 1
    (tmp_path / 'b.dat').write_bytes(b'ABC')
    assert not attachments(scan.refresh())['duplicate_groups']
