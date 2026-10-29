from obsidian_mcp.stewardship import Snapshot
from obsidian_mcp.steward_duplicates import duplicates


def test_exact_and_metadata_not_erased(tmp_path):
    (tmp_path / 'a.md').write_text('---\nscope: private\ndate: 2026-10-01\n---\none two three four five six seven eight nine ten\n')
    (tmp_path / 'b.md').write_bytes((tmp_path / 'a.md').read_bytes())
    (tmp_path / 'c.md').write_text((tmp_path / 'a.md').read_text().replace('2026-10-01', '2026-10-02'))
    report = duplicates(Snapshot(tmp_path).refresh())
    assert report['exact_groups'][0]['paths'] == ['a.md', 'b.md']
    assert report['normalized_text_groups'][0]['paths'] == ['a.md', 'b.md']
    assert all(p['classification'] == 'uncertain' and not p['merge_eligible'] for p in report['pairs'])


def test_scope_and_numeric_difference(tmp_path):
    body = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen value 10'
    for name, scope, text in [('a', 'private', body), ('b', 'private', body.replace('10', '20')), ('c', 'shared', body)]:
        (tmp_path / f'{name}.md').write_text(f'---\nscope: {scope}\n---\n{text}')
    report = duplicates(Snapshot(tmp_path).refresh(), threshold=0.5)
    assert len(report['pairs']) == 1
    assert report['pairs'][0]['paths'] == ['a.md', 'b.md']
    assert report['pairs'][0]['classification'] == 'uncertain'


def test_candidate_bound_and_delete(tmp_path):
    for i in range(20):
        (tmp_path / f'{i}.md').write_text(f'---\nscope: private\nversion: {i}\n---\na b c d e f g h i j k l m n o p')
    scan = Snapshot(tmp_path).refresh()
    report = duplicates(scan, max_pairs=7)
    assert report['candidate_pairs_examined'] == 7
    (tmp_path / '0.md').unlink()
    assert all('0.md' not in p['paths'] for p in duplicates(scan.refresh(), max_pairs=7)['pairs'])


import pytest


@pytest.mark.parametrize('kwargs, message', [({'max_pairs': 0}, 'Candidate bounds'), ({'per_note': 51}, 'Candidate bounds'),
                                             ({'max_pairs': True}, 'Candidate bounds'), ({'threshold': 0}, 'threshold must be in'),
                                             ({'threshold': 1.5}, 'threshold must be in'), ({'threshold': '0.7'}, 'threshold must be in')])
def test_candidate_bounds_and_threshold_are_checked(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        duplicates(Snapshot(tmp_path).refresh(), **kwargs)


def test_notes_sharing_a_little_wording_are_not_paired_below_the_threshold(tmp_path):
    words = ' '.join(f'w{i}' for i in range(30))
    (tmp_path / 'a.md').write_text('---\nscope: private\n---\n' + words + ' tail one two three four five')
    (tmp_path / 'b.md').write_text('---\nscope: private\n---\n' + ' '.join(f'w{i}' for i in range(6)) + ' other words entirely here now and more')
    assert duplicates(Snapshot(tmp_path).refresh(), threshold=0.9)['pairs'] == []
