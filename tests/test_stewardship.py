from datetime import date
from pathlib import Path

import pytest

from obsidian_mcp.stewardship import Snapshot, parse_document, properties, valid_date


SCHEMAS = [{'name': 'project', 'glob': 'Projects/*.md', 'fields': {
    'status': {'type': 'string', 'required': True, 'values': ['active', 'paused']},
    'due': {'type': 'date'}, 'budget': {'type': 'number'},
    'tags': {'type': 'list', 'item_type': 'string'}}, 'aliases': {'state': 'status'}}]


def write(root, path, text):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_properties_clean_and_defects(tmp_path):
    good = write(tmp_path, 'Projects/good.md', '---\nstatus: active\ndue: 2026-10-08\nbudget: 0\ntags: [α, β]\n---\n')
    bad = write(tmp_path, 'Projects/bad.md', '---\nstate: active\ndue: 2026-02-30\nbudget: true\ntags: [1]\n---\n')
    before = {p: p.read_bytes() for p in (good, bad)}
    report = properties(Snapshot(tmp_path).refresh(), SCHEMAS)
    assert report['counts'] == {'property.missing': 1, 'property.type': 2, 'property.item_type': 1, 'property.alias': 1}
    assert all(f['source'] == 'Projects/bad.md' and f['revision'] for f in report['findings'])
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize('text,rule', [
    ('---\na: 1\na: 2\n---\n', 'frontmatter.duplicate'),
    ('---\n[a, b]\n---\n', 'frontmatter.syntax'),
    ('---\na: [\n---\n', 'frontmatter.syntax'),
    ('---\na: 1\n', 'frontmatter.syntax'),
    ('---\n<<: {a: 1}\n---\n', 'frontmatter.syntax'),
])
def test_frontmatter_errors(text, rule):
    doc = parse_document('a.md', text.encode())
    assert rule in {i['rule'] for i in doc.issues}


def test_json_frontmatter_and_duplicate_keys():
    doc = parse_document('a.md', b'---\n{"status":"active","due":"2026-10-08"}\n---\ntext')
    assert doc.properties == {'status': 'active', 'due': '2026-10-08'}
    assert not doc.issues


@pytest.mark.parametrize('value,expected', [('2026-02-30', False), ('2026-2-01', False), ('2026-02-01', True), (date(2026, 1, 1), True), (0, False)])
def test_dates(value, expected):
    assert valid_date(value) == expected


def test_cache_schema_recheck_delete_rename(tmp_path):
    path = write(tmp_path, 'Projects/a.md', '---\nstatus: active\n---\n')
    scan = Snapshot(tmp_path).refresh()
    assert scan.stats['parsed_notes'] == 1
    assert not properties(scan, SCHEMAS)['findings']
    scan.refresh()
    assert scan.stats['parsed_notes'] == 0
    changed = [{'name': 'new', 'glob': '*.md', 'fields': {'owner': {'type': 'string', 'required': True}}}]
    assert properties(scan, changed)['counts'] == {'property.missing': 1}
    path.rename(tmp_path / 'Projects/b.md')
    scan.refresh()
    assert set(scan.documents) == {'Projects/b.md'}
    (tmp_path / 'Projects/b.md').unlink()
    scan.refresh()
    assert not scan.documents


def test_bad_schema_and_conflict(tmp_path):
    write(tmp_path, 'a.md', '---\nx: 1\n---\n')
    scan = Snapshot(tmp_path).refresh()
    with pytest.raises(ValueError):
        properties(scan, [{'name': 'bad', 'fields': {'x': {'type': 'guess'}}}])
    with pytest.raises(ValueError, match='Conflicting'):
        properties(scan, [{'name': 'one', 'fields': {'x': {'type': 'number'}}},
                          {'name': 'two', 'fields': {'x': {'type': 'string'}}}])


def test_cross_fields(tmp_path):
    write(tmp_path, 'a.md', '---\nstart: 2026-10-10\nend: 2026-10-01\n---\n')
    schema = [{'name': 'dates', 'fields': {'start': {'type': 'date'}, 'end': {'type': 'date'}},
               'rules': [{'left': 'start', 'op': 'le', 'right': 'end'}]}]
    assert properties(Snapshot(tmp_path).refresh(), schema)['counts'] == {'property.cross_field': 1}


def test_symlinks_and_exclusions(tmp_path):
    write(tmp_path, 'Templates/a.md', '---\nx: [\n---\n')
    (tmp_path / 'outside.md').symlink_to('/etc/hosts')
    scan = Snapshot(tmp_path).refresh()
    assert not scan.documents
    assert not scan.report([])['complete']
    assert scan.diagnostics[0]['reason'] == 'symlink_not_scanned'
