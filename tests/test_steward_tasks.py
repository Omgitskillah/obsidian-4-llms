import pytest

from obsidian_mcp.stewardship import Snapshot, tasks


def note(root, name, body, project='Alpha', historical=False):
    (root / name).write_text(f'---\nproject: {project}\n' + ('task_record: historical\n' if historical else '') + '---\n' + body)


def test_task_clock_status_and_examples(tmp_path):
    note(tmp_path, 'a.md', '- [ ] past [due:2026-10-01]\n- [x] done [due:2026-10-01]\n'
         '- [-] canceled [due:2026-10-01]\n- [/] work [due:2026-10-09]\n- [ ] undated\n'
         '> - [ ] quote [due:2026-10-01]\n```\n- [ ] example\n```\n%% - [ ] hidden %%\n')
    report = tasks(Snapshot(tmp_path).refresh(), as_of='2026-10-08', timezone='Europe/Berlin')
    assert len(report['tasks']) == 5
    assert report['counts'] == {'task.overdue': 1}
    assert [t['status'] for t in report['tasks']] == ['open', 'completed', 'canceled', 'in_progress', 'open']


def test_mirrors_project_and_history(tmp_path):
    note(tmp_path, 'active.md', '- [ ] Fix [task:42]\n')
    note(tmp_path, 'mirror.md', '- [x] Fixed [task:42]\n')
    note(tmp_path, 'other.md', '- [ ] Fixed [task:42]\n', project='Beta')
    note(tmp_path, 'history.md', '- [ ] Fix [task:42]\n', historical=True)
    report = tasks(Snapshot(tmp_path).refresh(), as_of='2026-10-08')
    assert report['counts'] == {'task.mirror_conflict': 2}
    conflict = report['findings'][0]['evidence']['occurrences']
    assert {r['source'] for r in conflict} == {'active.md', 'mirror.md'}
    assert report['tasks'][0]['identity'] != next(r['identity'] for r in report['tasks'] if r['source'] == 'other.md')


def test_recurrence_invalid_date_and_stable_id(tmp_path):
    note(tmp_path, 'a.md', '- [ ] weekly [task:w] [repeat:weekly] [due:2026-02-30]\n')
    scan = Snapshot(tmp_path).refresh()
    first = tasks(scan, as_of='2026-10-08')
    assert first['counts'] == {'task.invalid_due': 1, 'task.recurrence_review': 1}
    note(tmp_path, 'a.md', '\n\n- [ ] weekly [task:w] [repeat:weekly] [due:2026-02-30]\n')
    second = tasks(scan.refresh(), as_of='2026-10-08')
    assert first['tasks'][0]['occurrence_id'] == second['tasks'][0]['occurrence_id']


def test_clock_required(tmp_path):
    with pytest.raises(ValueError):
        tasks(Snapshot(tmp_path).refresh(), as_of='tomorrow')


def test_repeated_id_occurrences_are_distinct(tmp_path):
    note(tmp_path, 'a.md', '- [ ] work [task:42]\n- [x] work [task:42]\n')
    report = tasks(Snapshot(tmp_path).refresh(), as_of='2026-10-08')
    assert len({r['occurrence_id'] for r in report['tasks']}) == 2
    assert len({r['id'] for r in report['findings']}) == 2
