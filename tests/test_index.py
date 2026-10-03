import pytest

from obsidian_mcp import index


def _connect(tmp_path):
    return index.connect(tmp_path / "index.sqlite3")


def test_note_exists_false_for_empty_index(tmp_path):
    conn = _connect(tmp_path)
    assert index.note_exists(conn, "nonexistent") is False


def test_insert_note_then_note_exists_true(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    assert index.note_exists(conn, "id1") is True


def test_get_path_returns_none_for_unknown_id(tmp_path):
    conn = _connect(tmp_path)
    assert index.get_path(conn, "nonexistent") is None


def test_get_path_returns_the_stored_path(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    assert index.get_path(conn, "id1") == "shared/x.md"


def test_insert_note_rejects_duplicate_id(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    with pytest.raises(Exception):
        index.insert_note(
            conn, id="id1", path="shared/y.md", type="project", title="Y", author="both",
            created="2026-09-14T12:01", supersedes=None,
        )


def test_connect_is_idempotent_against_an_existing_database(tmp_path):
    db_path = tmp_path / "index.sqlite3"
    conn1 = index.connect(db_path)
    index.insert_note(
        conn1, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    conn1.close()

    conn2 = index.connect(db_path)
    assert index.note_exists(conn2, "id1") is True


def test_all_by_path_returns_id_type_title_and_created_keyed_by_path(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    index.insert_note(
        conn, id="id2", path="nuru/y.md", type="topic", title="Y", author="nuru",
        created="2026-09-14T12:00", supersedes=None,
    )

    assert index.all_by_path(conn) == {
        "shared/x.md": {"id": "id1", "type": "project", "title": "X", "created": "2026-09-14T12:00"},
        "nuru/y.md": {"id": "id2", "type": "topic", "title": "Y", "created": "2026-09-14T12:00"},
    }


def test_all_by_path_is_empty_for_an_empty_index(tmp_path):
    assert index.all_by_path(_connect(tmp_path)) == {}


def test_update_path_repoints_the_note(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )

    index.update_path(conn, "shared/x.md", "shared/y.md")

    assert index.get_path(conn, "id1") == "shared/y.md"


def test_update_path_is_a_no_op_if_nothing_is_indexed_at_old_path(tmp_path):
    conn = _connect(tmp_path)

    index.update_path(conn, "shared/nonexistent.md", "shared/new.md")  # must not raise


def test_delete_by_path_removes_the_note(tmp_path):
    conn = _connect(tmp_path)
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )

    index.delete_by_path(conn, "shared/x.md")

    assert index.note_exists(conn, "id1") is False
    assert index.get_path(conn, "id1") is None


def test_delete_by_path_is_a_no_op_if_nothing_is_indexed_at_path(tmp_path):
    conn = _connect(tmp_path)

    index.delete_by_path(conn, "shared/nonexistent.md")  # must not raise


def _vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_reconcile_removes_a_row_whose_file_no_longer_exists(tmp_path):
    vault = _vault(tmp_path)
    conn = index.connect(vault / "index.sqlite3")
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )
    # No shared/x.md written to vault - simulates the vault having changed
    # (deleted, moved, cleared) outside this server's own tools.

    removed = index.reconcile(conn, vault)

    assert removed == ["shared/x.md"]
    assert index.note_exists(conn, "id1") is False


def test_reconcile_keeps_a_row_whose_file_still_exists(tmp_path):
    vault = _vault(tmp_path)
    (vault / "shared").mkdir()
    (vault / "shared" / "x.md").write_text("content")
    conn = index.connect(vault / "index.sqlite3")
    index.insert_note(
        conn, id="id1", path="shared/x.md", type="project", title="X", author="both",
        created="2026-09-14T12:00", supersedes=None,
    )

    removed = index.reconcile(conn, vault)

    assert removed == []
    assert index.note_exists(conn, "id1") is True


def test_reconcile_is_a_no_op_on_an_empty_index(tmp_path):
    vault = _vault(tmp_path)
    conn = index.connect(vault / "index.sqlite3")

    assert index.reconcile(conn, vault) == []
