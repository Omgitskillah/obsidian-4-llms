from obsidian_mcp.stewardship import Snapshot, project_reviews


def test_review_states_and_evidence(tmp_path):
    (tmp_path / 'active.md').write_text('---\ntype: project\nproject: A\nstatus: active\nreview_due: 2026-10-01\n---\n')
    (tmp_path / 'paused.md').write_text('---\ntype: project\nproject: B\nstatus: paused\nreview_due: 2026-10-01\n---\n')
    (tmp_path / 'complete.md').write_text('---\ntype: project\nproject: C\nstatus: completed\n---\n')
    (tmp_path / 'tasks.md').write_text('---\nproject: A\n---\n- [ ] work\n')
    (tmp_path / 'decision.md').write_text('---\ntype: decision\nproject: A\ndecision_id: 7\ndecision_status: reopened\ndate: 2026-10-01\n---\n')
    report = project_reviews(Snapshot(tmp_path).refresh(), as_of='2026-10-08')
    assert report['counts'] == {'review.due': 1, 'review.missing_next_action': 1}
    active = next(r for r in report['reviews'] if r['project'] == 'A')
    assert active['task_count'] == active['unresolved_decision_count'] == 1
    assert active['decisions'][0]['source'] == 'decision.md'


def test_next_action_future_review_and_changed_status(tmp_path):
    file = tmp_path / 'a.md'
    file.write_text('---\ntype: project\nproject: A\nstatus: active\nnext_action: Test board\nreview_due: 2026-10-10\n---\n')
    scan = Snapshot(tmp_path).refresh()
    assert not project_reviews(scan, as_of='2026-10-08')['findings']
    file.write_text(file.read_text().replace('2026-10-10', 'tomorrow'))
    assert project_reviews(scan.refresh(), as_of='2026-10-08')['counts'] == {'review.invalid_due': 1}
