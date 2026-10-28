"""Deterministic failure and conflict regressions; never use a real vault."""
import concurrent.futures
import os
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file modes have no group or other bits")
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


def _link_directory(link, target):
    """A directory symlink, or on Windows a junction (which needs no privilege)."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_junction_or_directory_link_in_the_path_is_refused(vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "nuru").rename(vault / "old-nuru")
    _link_directory(vault / "nuru", outside)
    with pytest.raises(OSError):
        mutations.create(vault, vault / "nuru/new.md", b"escaped")
    with pytest.raises(OSError):
        mutations.read(vault, vault / "nuru/new.md")
    assert not list(outside.iterdir())


def test_a_directory_swapped_mid_write_cannot_redirect_it(vault, tmp_path, monkeypatch):
    """Between the path's check and the write, the note's folder is renamed away and a link to outside
    put in its place. Windows refuses the rename (the folder is held open); POSIX allows it, but the
    write follows the pinned descriptor into the renamed folder."""
    outside = tmp_path / "outside"
    outside.mkdir()
    real_stage, refused = mutations._stage, []
    def swap(fd, data, mode):
        try:
            (vault / "nuru").rename(vault / "old-nuru")
        except PermissionError:
            refused.append(True)
        else:
            _link_directory(vault / "nuru", outside)
        return real_stage(fd, data, mode)
    monkeypatch.setattr(mutations, "_stage", swap)
    mutations.create(vault, vault / "nuru/new.md", b"inside")
    assert not list(outside.iterdir())
    if sys.platform == "win32":
        assert refused and (vault / "nuru/new.md").read_bytes() == b"inside"
    else:
        assert (vault / "old-nuru/new.md").read_bytes() == b"inside"


def test_a_folder_above_the_vault_swapped_mid_write_cannot_redirect_it(tmp_path, monkeypatch):
    """The vault's parent folder (Documents\\Vaults for ...\\Vaults\\MyVault) is renamed away mid-write and a
    link put in its place, leading to a look-alike vault. Windows refuses the rename while the vault is
    held open; POSIX allows it, but every step goes through the vault's pinned descriptor."""
    root = tmp_path / "Vaults" / "MyVault"
    (root / "nuru").mkdir(parents=True)
    decoy = tmp_path / "decoy"
    (decoy / "MyVault" / "nuru").mkdir(parents=True)
    real_stage, refused = mutations._stage, []
    def swap(fd, data, mode):
        try:
            (tmp_path / "Vaults").rename(tmp_path / "Vaults-old")
        except PermissionError:
            refused.append(True)
        else:
            _link_directory(tmp_path / "Vaults", decoy)
        return real_stage(fd, data, mode)
    monkeypatch.setattr(mutations, "_stage", swap)
    mutations.create(root, root / "nuru/new.md", b"inside")
    assert not list((decoy / "MyVault" / "nuru").iterdir())
    if sys.platform == "win32":
        assert refused and (root / "nuru/new.md").read_bytes() == b"inside"
    else:
        assert (tmp_path / "Vaults-old" / "MyVault" / "nuru/new.md").read_bytes() == b"inside"


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
    code = '''import os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
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
        if os.path.basename(name) == "plan.md":
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


@pytest.mark.parametrize('operation', [
    lambda: server.write_note('new'),
    lambda: server.write_note('new', note='Plan', mode='replace'),
    lambda: server.write_note('new', note='Plan', mode='replace', expected_revision='stale'),
    lambda: server.write_note('new', note='Plan', type='correction'),
    lambda: server.rename_or_move_file('nuru/plan.md', 'nuru/plan.md'),
    lambda: server.create_note(type='correction', title='Fix', author='nuru'),
])
def test_pre_mutation_refusals_have_an_explicit_receipt(vault, operation):
    import json
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob('*.md')}
    with pytest.raises(ToolError) as caught:
        operation()
    receipt = json.loads(str(caught.value))
    assert receipt['code'] == 'REJECTED_BEFORE_WRITE'
    assert receipt['completion_certainty'] == 'not_completed'
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob('*.md')} == before


def test_post_update_permission_error_is_not_a_prewrite_rejection(vault, monkeypatch):
    original = mutations.update
    def update(*args):
        original(*args)
        raise PermissionError('receipt cleanup failed')
    monkeypatch.setattr(mutations, 'update', update)
    with pytest.raises(ToolError, match='PermissionError: receipt cleanup failed') as caught:
        server.write_note('published', note='Plan')
    assert 'REJECTED_BEFORE_WRITE' not in str(caught.value)
    assert server.read_note('Plan').endswith('published')


def test_replace_failure_after_backup_creation_is_not_a_prewrite_rejection(vault, monkeypatch):
    snapshot = server.read_note_snapshot('Plan')
    def fail(*args):
        raise ValueError('target changed before update')
    monkeypatch.setattr(mutations, 'update', fail)
    with pytest.raises(ToolError, match='target changed') as caught:
        server.write_note('replacement', note='Plan', mode='replace', expected_revision=snapshot['revision'])
    assert 'REJECTED_BEFORE_WRITE' not in str(caught.value)
    assert list((vault / 'trash').rglob('*.md'))


def test_move_recovery_refusal_is_not_a_prewrite_rejection(vault, monkeypatch):
    destination = vault / 'nuru/moved.md'
    def update(*args):
        destination.write_text('edited during move')
        raise ValueError('index update failed')
    monkeypatch.setattr(server.note_index, 'update_path', update)
    with pytest.raises(ToolError, match='Move recovery refused') as caught:
        server.rename_or_move_file('nuru/plan.md', 'nuru/moved.md')
    assert 'REJECTED_BEFORE_WRITE' not in str(caught.value)
    assert destination.exists() and (vault / 'nuru/plan.md').exists()


# ---------------------------------------------------------------- F13: refusals at the edges of a mutation
posix = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX")


@posix
def test_a_lock_file_that_is_not_a_regular_file_refuses_every_mutation(vault):
    (vault / ".obsidian-mcp-write.lock").unlink()
    os.mkfifo(vault / ".obsidian-mcp-write.lock")
    with pytest.raises(ToolError, match="Mutation lock is not a regular file"):
        server.write_note("more", note="Plan")
    assert (vault / "nuru" / "plan.md").read_text().endswith("original")


@posix
def test_a_note_that_is_not_a_regular_file_is_never_read_or_replaced(vault):
    os.mkfifo(vault / "nuru" / "pipe.md")
    with pytest.raises(ValueError, match="Mutation target is not a regular file"):
        mutations.read(vault, vault / "nuru" / "pipe.md")
    with mutations.coordinate(vault), pytest.raises(ValueError, match="not a regular file"):
        mutations.update(vault, vault / "nuru" / "pipe.md", b"", b"new")


@pytest.mark.parametrize("relative", ["", "nuru/../nuru/plan.md"])
def test_the_vault_itself_or_a_path_through_dotdot_is_not_a_mutation_target(vault, relative):
    with pytest.raises(ValueError, match="Invalid mutation path"):
        mutations.read(vault, vault / relative if relative else vault)


def test_an_update_from_a_stale_copy_is_refused_before_anything_is_staged(vault):
    path = vault / "nuru" / "plan.md"
    before = path.read_bytes()
    with mutations.coordinate(vault), pytest.raises(ValueError, match="Revision conflict: reread the note"):
        mutations.update(vault, path, b"what the caller last saw", b"new")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".obsidian-mcp-*.tmp"))


def test_a_source_changed_while_a_move_is_finishing_keeps_it_and_removes_only_our_copy(vault):
    source, destination = vault / "nuru" / "plan.md", vault / "nuru" / "moved.md"
    with mutations.coordinate(vault), pytest.raises(ValueError, match="Revision conflict during move"):
        with mutations.move(vault, source, destination):
            # Another editor saves the source (a new file, as editors write) before the move finishes.
            changed = source.with_name("plan.md.editor")
            changed.write_bytes(b"edited elsewhere")
            os.replace(changed, source)
    assert source.read_bytes() == b"edited elsewhere" and not destination.exists()


def test_a_destination_that_differs_right_after_linking_is_never_kept(vault, monkeypatch):
    source, destination = vault / "nuru" / "plan.md", vault / "nuru" / "moved.md"
    before = source.read_bytes()
    real_read, reads = mutations._read, []
    def read(fd, name):
        data, mode = real_read(fd, name)
        reads.append(name)
        if name == "moved.md" and len(reads) == 2:  # the check straight after the link
            return b"not what was linked", mode
        return data, mode
    monkeypatch.setattr(mutations, "_read", read)
    with mutations.coordinate(vault), pytest.raises(ValueError, match="Revision conflict during move"):
        with mutations.move(vault, source, destination):
            pytest.fail("the move went on after a mismatched link")
    assert source.read_bytes() == before and not destination.exists()
