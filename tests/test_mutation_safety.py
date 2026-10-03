"""Deterministic failure and conflict regressions; never use a real vault."""
import concurrent.futures
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from obsidian_mcp import mutations, server


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.delenv("OBSIDIAN_READ_ONLY", raising=False)
    server.write_note("original", note="Plan")
    return tmp_path


def test_replace_requires_fresh_client_revision(vault):
    snapshot = server.read_note_snapshot("Plan")
    with pytest.raises(ToolError, match="requires expected_revision"):
        server.write_note("new", note="Plan", mode="replace")
    server.write_note("intervening edit", note="Plan")
    with pytest.raises(ToolError, match="Revision conflict"):
        server.write_note("stale", note="Plan", mode="replace", expected_revision=snapshot["revision"])
    assert server.read_note("Plan").endswith("intervening edit")
    assert not (vault / "trash").exists()


def test_atomic_update_failure_keeps_original_and_cleans_temp(vault, monkeypatch):
    path = vault / "nuru/plan.md"
    before = path.read_bytes()
    path.chmod(0o640)
    def fail(*args, **kwargs):
        raise OSError("injected replacement failure")
    monkeypatch.setattr(mutations.os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        mutations.update(vault, path, before, b"new")
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_update_rechecks_revision_immediately_before_commit(vault, monkeypatch):
    path = vault / "nuru/plan.md"
    before = path.read_bytes()
    real_stage = mutations._stage
    def change(fd, data, mode):
        temp = real_stage(fd, data, mode)
        path.write_bytes(b"newer user data")
        return temp
    monkeypatch.setattr(mutations, "_stage", change)
    with pytest.raises(ValueError, match="Revision conflict"):
        mutations.update(vault, path, before, b"replacement")
    assert path.read_bytes() == b"newer user data"


def test_append_is_atomic_and_preserves_permissions(vault):
    path = vault / "nuru/plan.md"
    path.chmod(0o640)
    server.write_note("more", note="Plan")
    assert path.read_text().endswith("original\n\nmore")
    assert path.stat().st_mode & 0o777 == 0o640


def test_move_never_overwrites_destination_created_after_check(vault, monkeypatch):
    source = vault / "nuru/plan.md"
    destination = vault / "nuru/moved.md"
    before = source.read_bytes()
    real_link = mutations.os.link
    def race(src, dst, **kwargs):
        destination.write_text("competing user note")
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(mutations.os, "link", race)
    with pytest.raises(ToolError, match="FileExistsError"):
        server.rename_or_move_file("Plan", "nuru/moved.md")
    assert source.read_bytes() == before
    assert destination.read_text() == "competing user note"
    assert server.read_note_snapshot("Plan")["path"] == "nuru/plan.md"


def test_index_failure_rolls_back_move(vault, monkeypatch):
    def fail(*args):
        raise RuntimeError("index failure")
    monkeypatch.setattr(server.note_index, "update_path", fail)
    with pytest.raises(RuntimeError, match="index failure"):
        server.rename_or_move_file("Plan", "nuru/moved.md")
    assert (vault / "nuru/plan.md").exists()
    assert not (vault / "nuru/moved.md").exists()


def test_index_failure_removes_only_our_created_note(vault, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("index insert failure")
    monkeypatch.setattr(server.note_index, "insert_note", fail)
    with pytest.raises(RuntimeError, match="index insert failure"):
        server.write_note("new", note="Other")
    assert not (vault / "nuru/other.md").exists()


def test_directory_symlink_swap_after_resolution_cannot_escape(vault, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "new.md").write_text("preserve")
    target = vault / "nuru/new.md"
    (vault / "nuru").rename(vault / "old-nuru")
    (vault / "nuru").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        mutations.create(vault, target, b"clobber")
    assert (outside / "new.md").read_text() == "preserve"


def test_read_only_rejects_every_mutation_but_allows_reading(vault, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_READ_ONLY", "1")
    for call in (
        lambda: server.write_note("new", note="Plan"),
        lambda: server.rename_or_move_file("Plan", "archive"),
    ):
        with pytest.raises(ToolError, match="Read-only"):
            call()
    with pytest.raises(ToolError, match="Read-only"):
        server.create_note(type="topic", title="Other", author="nuru")
    assert server.read_note("Plan").endswith("original")


def test_parallel_appends_are_serialized_without_lost_updates(vault):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: server.write_note(f"unique-{i}", note="Plan"), range(12)))
    text = server.read_note("Plan")
    for i in range(12):
        assert f"unique-{i}" in text


def test_lock_is_held_across_processes_and_released(vault):
    code = '''import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(7)
'''
    def acquire():
        return subprocess.run([sys.executable, "-c", code, str(vault / ".obsidian-mcp-write.lock")], timeout=5).returncode
    with mutations.coordinate(vault):
        assert acquire() == 7
    assert acquire() == 0


def test_staging_failure_does_not_publish_partial_create(vault, monkeypatch):
    def fail(fd):
        raise OSError("injected flush failure")
    monkeypatch.setattr(mutations.os, "fsync", fail)
    with pytest.raises(OSError, match="flush failure"):
        mutations.create(vault, vault / "nuru/other.md", b"partial")
    assert not (vault / "nuru/other.md").exists()
    assert not list((vault / "nuru").glob("*.tmp"))


def test_failed_move_finalization_restores_index_and_files(vault, monkeypatch):
    real_unlink = mutations.os.unlink
    def fail_source(name, **kwargs):
        if name == "plan.md":
            raise PermissionError("injected source unlink failure")
        return real_unlink(name, **kwargs)
    monkeypatch.setattr(mutations.os, "unlink", fail_source)
    with pytest.raises(ToolError, match="source unlink failure"):
        server.rename_or_move_file("Plan", "nuru/moved.md")
    assert (vault / "nuru/plan.md").exists()
    assert not (vault / "nuru/moved.md").exists()
    assert server.read_note_snapshot("Plan")["path"] == "nuru/plan.md"


def test_recovery_refuses_to_delete_newer_content(vault, monkeypatch):
    path = vault / "nuru/other.md"
    def fail(*args, **kwargs):
        path.write_text("newer user edit")
        raise RuntimeError("index failure")
    monkeypatch.setattr(server.note_index, "insert_note", fail)
    with pytest.raises(ToolError, match="Recovery refused"):
        server.write_note("original", note="Other")
    assert path.read_text() == "newer user edit"


def test_read_only_lookup_does_not_reconcile_or_write_index(vault, monkeypatch):
    db = vault / server._INDEX_DB_NAME
    before = db.read_bytes()
    (vault / "nuru/plan.md").unlink()
    monkeypatch.setenv("OBSIDIAN_READ_ONLY", "1")
    assert server._indexed_notes() == {}
    assert db.read_bytes() == before


def test_symlinked_index_cannot_modify_outside_database(vault, monkeypatch):
    db = vault / server._INDEX_DB_NAME
    outside = vault / "outside.sqlite3"
    db.rename(outside)
    before = outside.read_bytes()
    db.symlink_to(outside)
    with pytest.raises(ToolError, match="symlink"):
        server.write_note("new", note="Other")
    assert outside.read_bytes() == before
    assert not (vault / "nuru/other.md").exists()
