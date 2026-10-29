"""F13: stewardship's refusals and edges: frontmatter it won't parse, files it won't scan, configurations it
won't take (obsidian_mcp.stewardship)."""
import os
import sys

import pytest

from obsidian_mcp import mutations, stewardship
from obsidian_mcp.link_integrity import AuditCancelled
from obsidian_mcp.stewardship import (Snapshot, attachments, inbox_triage, parse_document, project_reviews, properties,
                                      tasks, validate_schemas)

posix = pytest.mark.skipif(sys.platform == 'win32', reason='FIFOs, chmod and symlinks are POSIX here')


def issue(text):
    (only,) = parse_document('a.md', text.encode()).issues
    return only['evidence']


# ---------------------------------------------------------------- frontmatter it won't take
@pytest.mark.parametrize('frontmatter, message', [
    ('a: &x 1\nb: *x\n', 'YAML aliases are unsupported'),
    ('items:\n' + ''.join(f'  - {i}\n' for i in range(10001)), 'Frontmatter exceeds 10000 nodes'),
    ('a:\n  ? [x]\n  : 1\n', 'Nested property keys must be scalar strings'),
    ('a:\n  <<: {b: 1}\n', 'YAML merge keys are unsupported'),
    ('? [x]\n: 1\n', 'Property keys must be scalar strings'),
    ('1: one\n', 'Property keys must be strings'),
])
def test_frontmatter_with_unsupported_yaml_is_an_issue_not_a_property(frontmatter, message):
    assert message in issue(f'---\n{frontmatter}---\nBody.\n')


def test_a_repeated_nested_key_is_reported_with_its_line():
    doc = parse_document('a.md', b'---\na:\n  b: 1\n  b: 2\n---\n')
    assert {'line': 4, 'rule': 'frontmatter.duplicate', 'evidence': 'b'} in doc.issues


# ---------------------------------------------------------------- files it won't scan
def test_a_vault_that_is_not_a_folder_is_refused(tmp_path):
    with pytest.raises(ValueError, match='Vault must be an existing directory'):
        Snapshot(tmp_path / 'missing').refresh()


@posix
def test_unreadable_folders_symlinked_folders_and_special_files_are_diagnosed_not_followed(tmp_path):
    (tmp_path / 'locked').mkdir()
    (tmp_path / 'locked' / 'secret.md').write_text('x')
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'linked').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    os.mkfifo(tmp_path / 'pipe.png')
    (tmp_path / 'locked').chmod(0)
    try:
        scan = Snapshot(tmp_path).refresh()
    finally:
        (tmp_path / 'locked').chmod(0o700)
    reasons = {d['path'].rsplit('/', 1)[-1]: d['reason'] for d in scan.diagnostics}
    assert reasons['linked'] == 'symlink_not_scanned' and reasons['pipe.png'] == 'not_regular_file'
    assert 'locked' in reasons and 'locked/secret.md' not in scan.documents


def test_a_note_over_16_mib_is_not_read(tmp_path):
    with open(tmp_path / 'huge.md', 'wb') as file:
        file.truncate(16 * 1024 * 1024 + 1)
    scan = Snapshot(tmp_path).refresh()
    assert {'path': 'huge.md', 'reason': 'note_exceeds_16MiB_limit'} in scan.diagnostics and not scan.documents


def test_a_note_or_asset_changed_while_it_was_read_is_diagnosed(tmp_path, monkeypatch):
    (tmp_path / 'a.md').write_text('before')
    (tmp_path / 'b.png').write_bytes(b'image')
    real_read, real_fstat = mutations.read, os.fstat
    def read_then_edit(root, path):
        data = real_read(root, path)
        path.write_text('edited while being read, and longer')
        return data
    monkeypatch.setattr(mutations, 'read', read_then_edit)
    calls = []
    def grows(fd):
        info = real_fstat(fd)
        calls.append(1)
        return os.stat_result((*info[:6], info.st_size + len(calls) - 1, *info[7:])) if len(calls) > 1 else info
    monkeypatch.setattr(stewardship.os, 'fstat', grows)
    scan = Snapshot(tmp_path).refresh()
    assert {'path': 'a.md', 'reason': 'changed_during_read'} in scan.diagnostics
    assert {'path': 'b.png', 'reason': 'changed_during_read'} in scan.diagnostics


def test_stopping_during_an_asset_hash_cancels_the_scan(tmp_path):
    (tmp_path / 'b.png').write_bytes(b'image')
    stops = iter([False, True])  # the per-file check passes; the per-chunk one stops it
    with pytest.raises(AuditCancelled):
        Snapshot(tmp_path, should_stop=lambda: next(stops)).refresh()


def test_a_note_changed_between_the_scan_and_the_link_index_is_diagnosed(tmp_path):
    (tmp_path / 'a.md').write_text('[[b]]')
    scan = Snapshot(tmp_path).refresh()
    (tmp_path / 'a.md').write_text('[[c]]')
    scan.links()
    assert {'path': 'a.md', 'reason': 'changed_between_snapshot_and_links'} in scan.diagnostics


# ---------------------------------------------------------------- configurations it won't take
FIELD = {'owner': {'type': 'string'}}


@pytest.mark.parametrize('schemas, message', [
    ({}, 'schemas must be a list'),
    (['n'], 'Invalid schema keys'),
    ([{'name': 'n', 'fields': FIELD, 'colour': 'red'}], 'Invalid schema keys'),
    ([{'fields': FIELD}], 'Schema names must be unique strings'),
    ([{'name': 'n', 'fields': FIELD}, {'name': 'n', 'fields': FIELD}], 'Schema names must be unique strings'),
    ([{'name': 'n', 'fields': FIELD, 'glob': 3}], 'Schema requires fields and a path glob'),
    ([{'name': 'n', 'fields': []}], 'Schema requires fields and a path glob'),
    ([{'name': 'n', 'fields': {'owner': 'string'}}], 'Invalid field specification'),
    ([{'name': 'n', 'fields': {'owner': {'type': 'string', 'max': 3}}}], 'Invalid field specification'),
    ([{'name': 'n', 'fields': {'owner': {'type': 'person'}}}], 'Unsupported property type'),
    ([{'name': 'n', 'fields': {'owner': {'type': 'list', 'item_type': 'person'}}}], 'Unsupported property type'),
    ([{'name': 'n', 'fields': {'owner': {'type': 'string', 'required': 'yes'}}}], 'required must be boolean'),
    ([{'name': 'n', 'fields': {'owner': {'type': 'string', 'values': 'me'}}}], 'values must be a list'),
    ([{'name': 'n', 'fields': FIELD, 'aliases': {'author': 'writer'}}], 'aliases must map legacy property names'),
    ([{'name': 'n', 'fields': FIELD, 'rules': [{'left': 'owner', 'op': 'ne', 'right': 'owner'}]}], 'Cross-field rules accept'),
    ([{'name': 'n', 'fields': FIELD, 'rules': [{'left': 'owner', 'op': 'eq', 'right': 'team'}]}], 'reference undeclared fields'),
])
def test_a_property_schema_outside_the_known_shape_is_refused(schemas, message):
    with pytest.raises(ValueError, match=message):
        validate_schemas(schemas)


def test_values_outside_the_allowed_set_and_incomparable_rule_operands_are_findings(tmp_path):
    (tmp_path / 'a.md').write_text('---\nstatus: lost\nstart: 2026-10-01\nend: 3\n---\n')
    schema = [{'name': 'n', 'fields': {'status': {'type': 'string', 'values': ['open', 'done']},
                                       'start': {'type': 'string'}, 'end': {'type': 'number'}},
               'rules': [{'left': 'start', 'op': 'le', 'right': 'end'}]}]
    rules = sorted(f['rule'] for f in properties(Snapshot(tmp_path).refresh(), schema)['findings'])
    assert rules == ['property.cross_field', 'property.value']


def test_attachment_and_canvas_inputs_it_cannot_read_are_diagnosed(tmp_path, monkeypatch):
    (tmp_path / 'list.canvas').write_text('[]')
    (tmp_path / 'nodes.canvas').write_text('{"nodes": ["not a node"]}')
    (tmp_path / 'a.md').write_text('---\ncover: plain-name.png\n---\n')
    (tmp_path / 'plain-name.png').write_bytes(b'png')
    scan = Snapshot(tmp_path).refresh()
    with pytest.raises(ValueError, match='large_bytes must be a positive integer'):
        attachments(scan, large_bytes=0)
    report = attachments(scan, reference_properties=('cover',))
    reasons = {d['path']: d['reason'] for d in report['diagnostics'] if d['path'].endswith('.canvas')}
    assert reasons == {'list.canvas': 'invalid_canvas', 'nodes.canvas': 'invalid_canvas_node'}
    owned = next(row for row in report['inventory'] if row['path'] == 'plain-name.png')
    assert owned['owners'] == [{'source': 'a.md', 'line': 2, 'syntax': 'property', 'property': 'cover'}]
    scan.assets['list.canvas'] = (scan.assets['list.canvas'][0], 16 * 1024 * 1024 + 1)
    assert {'path': 'list.canvas', 'reason': 'canvas_too_large'} in attachments(scan)['diagnostics']


def test_an_unknown_checkbox_mark_is_a_finding_not_a_task(tmp_path):
    (tmp_path / 'a.md').write_text('- [?] Maybe\n- [ ] Real\n')
    report = tasks(Snapshot(tmp_path).refresh(), as_of='2026-10-08')
    assert [f['rule'] for f in report['findings']] == ['task.unsupported_status'] and len(report['tasks']) == 1


def test_review_limits_and_projects_without_valid_metadata(tmp_path):
    (tmp_path / 'p.md').write_text('---\ntype: project\nproject: A\nstatus: dreaming\n---\n')
    scan = Snapshot(tmp_path).refresh()
    with pytest.raises(ValueError, match='Review evidence limit must be 1..100'):
        project_reviews(scan, as_of='2026-10-08', limit=0)
    assert project_reviews(scan, as_of='2026-10-08')['counts'] == {'review.invalid_metadata': 1}


@pytest.mark.parametrize('config, message', [
    ([], 'Inbox config accepts inboxes/destinations/rules'),
    ({'inboxes': [], 'destinations': {}, 'rules': [], 'model': 'x'}, 'Inbox config accepts inboxes/destinations/rules'),
    ({'inboxes': 'Inbox/*', 'destinations': {}, 'rules': []}, 'Invalid inbox configuration'),
    ({'inboxes': [], 'destinations': {}, 'rules': [{'field': 'topic', 'value': 'x', 'destination': 'Nowhere'}]},
     'Routing rules require field/value/declared destination'),
])
def test_an_inbox_configuration_outside_the_known_shape_is_refused(tmp_path, config, message):
    with pytest.raises(ValueError, match=message):
        inbox_triage(Snapshot(tmp_path).refresh(), config)


def test_triage_never_routes_to_two_places_or_into_an_excluded_folder(tmp_path):
    from obsidian_mcp.link_integrity import Policy
    for folder in ('Inbox', 'Work', 'Home', '.archive'):
        (tmp_path / folder).mkdir()
    (tmp_path / 'Inbox' / 'both.md').write_text('---\nscope: private\ntopic: x\nkind: y\n---\n')
    (tmp_path / 'Inbox' / 'hidden.md').write_text('---\nscope: private\ntopic: z\n---\n')
    config = {'inboxes': ['Inbox/*'], 'destinations': {'Work': {'scope': 'private'}, 'Home': {'scope': 'private'},
                                                       '.archive': {'scope': 'private'}},
              'rules': [{'field': 'topic', 'value': 'x', 'destination': 'Work'}, {'field': 'kind', 'value': 'y', 'destination': 'Home'},
                        {'field': 'topic', 'value': 'z', 'destination': '.archive'}]}
    queue = {row['source']: row for row in inbox_triage(Snapshot(tmp_path, Policy()).refresh(), config)['queue']}
    assert (queue['Inbox/both.md']['reason'], queue['Inbox/both.md']['destination']) == ('ambiguous_routes', None)
    assert (queue['Inbox/hidden.md']['reason'], queue['Inbox/hidden.md']['destination']) == ('destination_excluded', None)


def test_a_reference_property_that_is_not_text_and_a_task_note_with_broken_frontmatter_are_skipped(tmp_path):
    (tmp_path / 'a.md').write_text('---\ncover: [3, plain.png]\n---\n')
    (tmp_path / 'plain.png').write_bytes(b'png')
    (tmp_path / 'b.md').write_text('---\nx: 1\nx: 2\n---\n- [ ] Hidden task\n')
    scan = Snapshot(tmp_path).refresh()
    owned = next(row for row in attachments(scan, reference_properties=('cover',))['inventory'] if row['path'] == 'plain.png')
    assert [o['source'] for o in owned['owners']] == ['a.md']
    assert tasks(scan, as_of='2026-10-08')['tasks'] == []
