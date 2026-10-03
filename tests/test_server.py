import base64
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from obsidian_mcp import server


def _no_done(result):
    """A write/move result without its "done" sentence, for comparing the rest exactly."""
    return {k: v for k, v in result.items() if k != "done"}


def _notes():
    """Every note the tools can see, as vault-relative paths (what list_notes returned)."""
    return [server._rel(p) for p in server._all_notes()]


def _files():
    """Every visible file outside trash/, as vault-relative paths (what list_files returned)."""
    root = server.VAULT_PATH
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and server._is_visible(p) and not server._is_trashed(p)
    )


# --- which notes the tools see ---


def test_list_notes_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _notes() == []


def test_list_notes_returns_markdown_files_only(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("# Welcome")
    (tmp_path / "notes.txt").write_text("not markdown")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _notes() == ["Welcome.md"]


def test_list_notes_includes_nested_files_as_relative_paths(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("# Welcome")
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Nuru MCP.md").write_text("# Nuru MCP")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _notes() == ["Projects/Nuru MCP.md", "Welcome.md"]


def test_browse_raises_clear_error_on_permission_denied(monkeypatch):
    class DeniedPath:
        def iterdir(self):
            raise PermissionError("denied")

    monkeypatch.setattr(server, "VAULT_PATH", DeniedPath())

    with pytest.raises(ToolError, match="Access denied"):
        server.browse()


def test_list_notes_excludes_templates_folder(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("# Welcome")
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates" / "01 Profile.md").write_text("---\n---\n\n# {{title}}\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _notes() == ["Welcome.md"]


# --- _folder_listing (browse's folder) ---


def test_list_files_in_directory_lists_recursively(tmp_path, monkeypatch):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Nuru MCP.md").write_text("# Nuru MCP")
    (tmp_path / "Projects" / "Sub").mkdir()
    (tmp_path / "Projects" / "Sub" / "diagram.png").write_bytes(b"data")
    (tmp_path / "Other.md").write_text("# Other")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server._folder_listing("Projects")

    assert result["files"] == ["Projects/Nuru MCP.md", "Projects/Sub/diagram.png"]
    assert result["count"] == 2
    assert result["directory"] == "Projects"
    assert "hint" not in result


def test_list_files_in_directory_returns_empty_files_and_a_hint_if_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server._folder_listing("NoSuchDir")

    assert result["files"] == []
    assert result["count"] == 0
    assert "NoSuchDir" in result["hint"]
    assert "browse without a folder" in result["hint"]


def test_list_files_in_directory_hints_when_the_directory_exists_but_is_empty(tmp_path, monkeypatch):
    (tmp_path / "Empty").mkdir()
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server._folder_listing("Empty")

    assert result["files"] == []
    assert "exists but contains no visible files" in result["hint"]


def test_list_files_in_directory_finds_first_nested_match_by_name(tmp_path, monkeypatch):
    (tmp_path / "Projects" / "folder").mkdir(parents=True)
    (tmp_path / "Projects" / "folder" / "a.md").write_text("a")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server._folder_listing("folder")["files"] == ["Projects/folder/a.md"]


def test_browse_raises_clear_error_on_a_locked_folder(tmp_path, monkeypatch):
    import os

    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    try:
        with pytest.raises(ToolError, match="Access denied"):
            server.browse(folder="locked")
    finally:
        os.chmod(locked, 0o755)


# --- search_notes ---


def _search_paths(result):
    return [match["path"] for match in result["matches"]]


def test_search_notes_finds_matching_notes_case_insensitively(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    (tmp_path / "Other.md").write_text("Nothing relevant here")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.search_notes("hello")

    assert _search_paths(result) == ["Welcome.md"]
    assert result["count"] == 1
    assert result["query"] == "hello"
    assert "hint" not in result

def test_search_notes_finds_partial_matching_notes_case_insensitively(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    (tmp_path / "Other.md").write_text("Nothing relevant here")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.search_notes("ell")) == ["Welcome.md"]

def test_search_notes_finds_tags_in_notes_case_insensitively(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    (tmp_path / "Other.md").write_text("Nothing #relevant here")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.search_notes("#relevant")) == ["Other.md"]

def test_search_notes_returns_no_matches_and_a_retry_hint_when_not_found(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.search_notes("nonexistent")

    assert result["matches"] == []
    assert result["count"] == 0
    assert "nonexistent" in result["hint"]
    assert "fewer or different words" in result["hint"]


def test_search_notes_requires_every_word_in_any_order(tmp_path, monkeypatch):
    (tmp_path / "Outage.md").write_text("The modem dropped the connection at noon")
    (tmp_path / "Modem.md").write_text("The modem is fine")
    (tmp_path / "Link.md").write_text("Only the connection is mentioned")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    # Word order in the query is the reverse of the text's.
    assert _search_paths(server.search_notes("connection modem")) == ["Outage.md"]


def test_search_notes_treats_hyphen_underscore_and_space_alike(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("Saw a PDP-drop at noon")
    (tmp_path / "b.md").write_text("PDP_drop happened again")
    (tmp_path / "c.md").write_text("PDP only")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    for query in ("PDP drop", "PDP-drop", "pdp_drop"):
        assert _search_paths(server.search_notes(query)) == ["a.md", "b.md"], query


def test_search_notes_matches_a_hyphenated_word_as_an_adjacent_pair(tmp_path, monkeypatch):
    """Dates are searched as e.g. "09-17": that must not match every note that
    merely mentions 09 and 17 somewhere (a time and a count, say)."""
    (tmp_path / "a.md").write_text("Standup on 2026-09-17")
    (tmp_path / "b.md").write_text("Call at 09:00, lead time 17 weeks")
    (tmp_path / "c.md").write_text("Moved to 09 17 in the plan")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.search_notes("09-17")) == ["a.md", "c.md"]


def test_search_notes_searches_the_filename_not_just_the_body(tmp_path, monkeypatch):
    (tmp_path / "devops").mkdir()
    (tmp_path / "devops" / "Kubernetes.md").write_text("Cluster setup notes.")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.search_notes("kubernetes")

    assert _search_paths(result) == ["devops/Kubernetes.md"]
    # Nothing in the body matched, so the snippet is just the start of the body.
    assert result["matches"][0]["snippet"] == "Cluster setup notes."


def test_search_notes_ranks_filename_and_title_hits_above_body_hits(tmp_path, monkeypatch):
    # Alphabetically "Alpha" and "Beta" come first, so only ranking puts Kubernetes.md on top.
    (tmp_path / "Alpha.md").write_text("Mentions kubernetes in passing")
    (tmp_path / "Beta.md").write_text("Also mentions kubernetes")
    (tmp_path / "Kubernetes.md").write_text("Cluster setup notes.")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Kubernetes Upgrade", author="nuru", body="Steps.")

    paths = _search_paths(server.search_notes("kubernetes"))

    assert set(paths[:2]) == {"Kubernetes.md", created["path"]}
    assert paths[2:] == ["Alpha.md", "Beta.md"]


def test_search_notes_returns_id_type_title_and_snippet_for_indexed_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Battery Life", author="nuru", body="Discussing the supercap.")

    match = server.search_notes("supercap")["matches"][0]

    assert match["path"] == created["path"]
    assert match["id"] == created["id"]
    assert match["type"] == "topic"
    assert match["title"] == "Battery Life"
    assert "supercap" in match["snippet"]


def test_search_notes_returns_null_id_and_type_for_notes_not_made_by_create_note(tmp_path, monkeypatch):
    (tmp_path / "Plain.md").write_text("Some text about routers")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    match = server.search_notes("routers")["matches"][0]

    assert match == {
        "path": "Plain.md",
        "id": None,
        "type": None,
        "title": "Plain",
        "snippet": "Some text about routers",
    }


def test_search_notes_does_not_create_the_index_file(tmp_path, monkeypatch):
    (tmp_path / "Plain.md").write_text("Some text")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    server.search_notes("text")

    assert not (tmp_path / server._INDEX_DB_NAME).exists()


def test_search_notes_snippet_is_a_window_around_the_first_hit_and_skips_frontmatter(tmp_path, monkeypatch):
    body = "x" * 300 + "\n\nthe router firmware\nis old " + "y" * 300
    (tmp_path / "Long.md").write_text(f"---\ntags:\n  - router\n---\n\n# Long\n\n{body}")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    snippet = server.search_notes("router")["matches"][0]["snippet"]

    assert "router firmware is old" in snippet  # newlines collapsed to spaces
    assert "tags:" not in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= server._SNIPPET_CHARS + 2


def test_search_notes_caps_results_and_reports_the_true_count(tmp_path, monkeypatch):
    for i in range(12):
        (tmp_path / f"note{i:02}.md").write_text("A widget note")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.search_notes("widget")

    assert len(result["matches"]) == 10
    assert result["count"] == 12
    assert "10 of 12" in result["hint"]


@pytest.mark.parametrize("query", ["", "   ", "-_ -"])
def test_search_notes_rejects_a_query_with_no_words(tmp_path, monkeypatch, query):
    (tmp_path / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="empty"):
        server.search_notes(query)


def test_search_notes_excludes_templates_folder(tmp_path, monkeypatch):
    """Regression test: search_notes("{{title}}") used to return all eleven
    generated templates, so an agent could retrieve unfilled placeholder text
    as if it were real vault content."""
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates" / "01 Profile.md").write_text("---\n---\n\n# {{title}}\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.search_notes("{{title}}")["matches"] == []


# --- read_note ---


def test_read_note_with_extension(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("Welcome.md") == "Hello World"


def test_read_note_without_extension(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("Welcome") == "Hello World"


def test_read_note_finds_nested_note_by_name(tmp_path, monkeypatch):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Nuru MCP.md").write_text("# Nuru MCP")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("Nuru MCP") == "# Nuru MCP"


def test_read_note_with_relative_subdirectory_path(tmp_path, monkeypatch):
    (tmp_path / "Learning").mkdir()
    (tmp_path / "Learning" / "Agents.md").write_text("# Agents note content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("Learning/Agents.md") == "# Agents note content"


def test_read_note_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("NoSuchNote")


def test_read_note_raises_clear_error_on_permission_denied(tmp_path, monkeypatch):
    import os

    note = tmp_path / "Welcome.md"
    note.write_text("Hello World")
    os.chmod(note, 0o000)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    try:
        with pytest.raises(ToolError, match="Access denied"):
            server.read_note("Welcome.md")
    finally:
        os.chmod(note, 0o644)


# --- list_tags ---


def test_list_tags_returns_unique_sorted_tags(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("#test #dummy-vault")
    (tmp_path / "Other.md").write_text("Nothing #relevant here, also #test again")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.list_tags() == ["dummy-vault", "relevant", "test"]


def test_list_tags_ignores_markdown_headings(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("# Welcome\n\nNo tags here.")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.list_tags() == []


def test_list_tags_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.list_tags() == []


def test_list_tags_excludes_templates_folder(tmp_path, monkeypatch):
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates" / "01 Profile.md").write_text("#profile")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.list_tags() == []


# --- count_tags ---


def test_count_tags_counts_occurrences_across_notes(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("#test #dummy-vault")
    (tmp_path / "Other.md").write_text("Nothing #relevant here, also #test again")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.count_tags() == {"dummy-vault": 1, "relevant": 1, "test": 2}


def test_count_tags_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.count_tags() == {}


def test_count_tags_excludes_templates_folder(tmp_path, monkeypatch):
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates" / "01 Profile.md").write_text("#profile")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.count_tags() == {}


# --- Vault containment: read_note ---


def test_read_note_rejects_dotdot_traversal_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "secret.md").write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("../secret")
    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("../secret.md")


def test_read_note_rejects_absolute_path_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note(str(outside))


def test_read_note_rejects_nested_dotdot_traversal(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Sub").mkdir(parents=True)
    (tmp_path / "secret.md").write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("Sub/../../secret")


def test_read_note_rejects_symlinked_note_pointing_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("leak")
    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("leak.md")


def test_read_note_rejects_symlinked_note_found_via_nested_fallback_search(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    (vault / "Sub").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    (vault / "Sub" / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    # Looked up by bare name, so it hits the recursive fallback search.
    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("leak")


def test_read_note_still_reads_legitimate_note_alongside_a_bad_symlink(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    (vault / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server.read_note("Welcome") == "Hello World"
    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("leak")


def test_read_note_allows_symlink_pointing_within_the_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Real.md").write_text("Real content")
    (vault / "Alias.md").symlink_to(vault / "Real.md")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server.read_note("Alias") == "Real content"


# --- Vault containment: _folder_listing ---


def test_list_files_in_directory_rejects_dotdot_traversal_outside_vault(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server._folder_listing("../outside")["files"] == []
    assert server._folder_listing("..")["files"] == []


def test_list_files_in_directory_rejects_absolute_path_outside_vault(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server._folder_listing(str(outside_dir))["files"] == []


def test_list_files_in_directory_rejects_nested_dotdot_traversal(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Projects").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server._folder_listing("Projects/../../outside")["files"] == []


def test_list_files_in_directory_rejects_symlinked_directory_pointing_outside(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    (vault / "link").symlink_to(outside_dir)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server._folder_listing("link")["files"] == []


def test_list_files_in_directory_rejects_symlinked_directory_found_via_fallback(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    (vault / "Projects").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    (vault / "Projects" / "link").symlink_to(outside_dir)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    # Looked up by bare name, so it hits the recursive fallback search.
    assert server._folder_listing("link")["files"] == []


def test_list_files_in_directory_excludes_symlinked_subdir_pointing_outside(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    (vault / "Projects").mkdir(parents=True)
    (vault / "Projects" / "real.md").write_text("real content")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.md").write_text("TOP SECRET")
    (vault / "Projects" / "link").symlink_to(outside_dir)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server._folder_listing("Projects")["files"] == ["Projects/real.md"]


def test_list_files_in_directory_allows_symlinked_directory_within_the_vault(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    (vault / "Real").mkdir(parents=True)
    (vault / "Real" / "note.md").write_text("content")
    (vault / "Alias").symlink_to(vault / "Real")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    # The resolved target is used, so paths come back under its real name.
    assert server._folder_listing("Alias")["files"] == ["Real/note.md"]


# --- create_note ---


def test_create_note_writes_frontmatter_heading_and_body(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="project", title="My Project", author="both", body="Body text.")

    assert result["path"] == "nuru/my-project.md"
    text = (tmp_path / "nuru" / "my-project.md").read_text()
    assert text.startswith("---\n")
    assert "\n---\n\n# My Project\n\nBody text." in text
    assert f"id: {result['id']}\n" in text


def test_create_note_stamps_the_current_frontmatter_version(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server.template, "VERSION", "9.9.9")

    result = server.create_note(type="project", title="X", author="both")

    assert "version: 9.9.9\n" in (tmp_path / result["path"]).read_text()


def test_create_note_returns_id_and_path(tmp_path, monkeypatch):
    from ulid import ULID

    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="Some Topic", author="nuru")
    assert ULID.from_str(result["id"]) is not None
    assert result["path"] == "nuru/some-topic.md"


def test_create_note_maps_type_to_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.create_note(type="directives", title="A", author="user")["path"] == "user/a.md"
    assert server.create_note(type="profile", title="B", author="nuru")["path"] == "nuru/b.md"
    assert server.create_note(type="project", title="C", author="both")["path"] == "nuru/c.md"
    assert server.create_note(type="project", title="D", author="both", folder="shared/Projects")["path"] == "shared/Projects/d.md"


def test_create_note_slugifies_title_for_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="Café & Co: A Story!", author="nuru")
    assert result["path"] == "nuru/cafe-co-a-story.md"


def test_create_note_tags_include_type_plus_extras(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="project", title="X", author="both", tags=["extra", "another"])
    text = (tmp_path / result["path"]).read_text()
    assert "tags:\n  - project\n  - extra\n  - another\n" in text


def test_create_note_aliases_are_title_then_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="project", title="X", author="both")
    text = (tmp_path / result["path"]).read_text()
    assert f"aliases:\n  - X\n  - {result['id']}\n" in text


def test_create_note_rejects_unknown_type(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="Unknown type"):
        server.create_note(type="bogus", title="X", author="both")


def test_create_note_rejects_unknown_author(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="Unknown author"):
        server.create_note(type="project", title="X", author="bogus")


def test_create_note_rejects_empty_title(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="ValueError"):
        server.create_note(type="project", title="   ", author="both")


def test_create_note_still_requires_the_type_specific_field_the_server_cannot_default(tmp_path, monkeypatch):
    """date and session default (see below); targets can't - only the caller knows what is corrected."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="expects exactly these extra fields: 'targets'"):
        server.create_note(type="correction", title="X", author="user")


def test_create_note_accepts_the_type_specific_field(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="daily", title="X", author="user", date="2026-09-14")
    text = (tmp_path / result["path"]).read_text()
    assert "date: '2026-09-14'\n" in text


def test_create_note_rejects_type_specific_field_on_wrong_type(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="ValueError"):
        server.create_note(type="project", title="X", author="both", date="2026-09-14")


def test_create_note_rejects_a_malformed_date(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="date must be 'YYYY-MM-DD'"):
        server.create_note(type="daily", title="X", author="user", date="not-a-date")


def test_create_note_rejects_a_date_that_is_not_a_real_calendar_date(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="not a valid calendar date"):
        server.create_note(type="daily", title="X", author="user", date="2026-02-30")


def test_create_note_rejects_a_malformed_session(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="session must be 'YYYY-MM-DD-NN'"):
        server.create_note(type="transcript", title="X", author="nuru", session="garbage-session-value")


def test_create_note_accepts_a_well_formed_session(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="transcript", title="X", author="nuru", session="2026-09-14-01")
    text = (tmp_path / result["path"]).read_text()
    assert "session: 2026-09-14-01\n" in text


def test_create_note_rejects_targets_referencing_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="unknown note id"):
        server.create_note(type="correction", title="X", author="user", targets=["nope1", "nope2"])


def test_create_note_accepts_targets_referencing_existing_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    original = server.create_note(type="project", title="Original", author="both")

    result = server.create_note(type="correction", title="Fix", author="user", targets=[original["id"]])
    text = (tmp_path / result["path"]).read_text()
    assert f"targets:\n  - {original['id']}\n" in text


def test_create_note_rejects_a_tag_containing_whitespace(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="whitespace"):
        server.create_note(type="project", title="X", author="both", tags=["bad\n\n author: hacked"])


def test_create_note_accepts_tags_without_whitespace(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="project", title="X", author="both", tags=["good-tag"])
    text = (tmp_path / result["path"]).read_text()
    assert "  - good-tag\n" in text


def test_create_note_transliterates_accented_latin_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="Café Story", author="nuru")
    assert result["path"] == "nuru/cafe-story.md"


def test_create_note_transliterates_a_non_latin_title_instead_of_dropping_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="日本語のノート", author="nuru")
    assert result["path"] != "nuru/.md"
    assert result["path"].startswith("nuru/")
    assert result["path"] != f"nuru/{result['id'].lower()}.md"  # real transliteration happened


def test_create_note_falls_back_to_the_id_when_transliteration_yields_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="🎉🎊✨", author="nuru")
    assert result["path"] == f"nuru/{result['id'].lower()}.md"


def test_create_note_still_rejects_a_genuinely_empty_title(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="title is empty"):
        server.create_note(type="topic", title="   ", author="nuru")


def test_create_note_preserves_unicode_in_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="topic", title="日本語のノート", author="nuru")
    text = (tmp_path / result["path"]).read_text()
    assert "日本語のノート" in text
    assert "\\u" not in text


def test_create_note_rejects_a_user_folder_type_with_a_non_user_author(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match='only author="user" may create'):
        server.create_note(type="directives", title="X", author="nuru")


def test_create_note_accepts_a_user_folder_type_with_author_user(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.create_note(type="directives", title="X", author="user")
    assert result["path"] == "user/x.md"


def test_create_note_rejects_unknown_supersedes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="supersedes"):
        server.create_note(type="project", title="X", author="both", supersedes="nonexistent")


def test_create_note_accepts_supersedes_of_an_existing_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    original = server.create_note(type="project", title="Original", author="both")
    result = server.create_note(type="project", title="Follow Up", author="both", supersedes=original["id"])
    text = (tmp_path / result["path"]).read_text()
    assert f"supersedes: {original['id']}\n" in text


def test_create_note_refuses_to_overwrite_existing_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="project", title="X", author="both")

    with pytest.raises(ToolError, match="FileExistsError"):
        server.create_note(type="project", title="X", author="both")


def test_create_note_is_atomic_against_a_racing_duplicate_create(tmp_path, monkeypatch):
    """The existence check and the write must be one atomic filesystem call.

    A check-then-write race would let two concurrent create_note calls for
    the same path both "see" no file and both succeed, one silently
    clobbering the other - exactly the scenario a client retrying after a
    server-side timeout can hit if the original, abandoned write is still
    landing. Simulated here by writing the file in between _resolve_target
    and the actual open("x") call that create_note makes internally.
    """
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    real_resolve_target = server._resolve_target

    def racing_resolve_target(path):
        target = real_resolve_target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("landed from the other call")
        return target

    monkeypatch.setattr(server, "_resolve_target", racing_resolve_target)

    with pytest.raises(ToolError, match="FileExistsError"):
        server.create_note(type="project", title="X", author="both")
    assert (tmp_path / "nuru" / "x.md").read_text() == "landed from the other call"


def test_create_note_rolls_back_the_file_if_the_index_insert_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    def failing_insert_note(*args, **kwargs):
        raise RuntimeError("index insert boom")

    monkeypatch.setattr(server.note_index, "insert_note", failing_insert_note)

    with pytest.raises(RuntimeError, match="boom"):
        server.create_note(type="project", title="X", author="both")
    assert not (tmp_path / "nuru" / "x.md").exists()


def test_create_note_reports_an_index_integrity_error_clearly(tmp_path, monkeypatch):
    """Regression test: a sqlite3.IntegrityError from the index insert used
    to propagate unwrapped - not one of _tool's anticipated error types - so
    it reached the caller as a bare, message-less "Error executing tool
    create_note" crash instead of an explainable ValueError."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    def failing_insert_note(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: notes.path")

    monkeypatch.setattr(server.note_index, "insert_note", failing_insert_note)

    with pytest.raises(ToolError, match="ValueError") as exc_info:
        server.create_note(type="project", title="X", author="both")
    assert "already has an entry" in str(exc_info.value)
    assert not (tmp_path / "nuru" / "x.md").exists()


def test_create_note_recreating_a_title_after_the_vault_was_cleared_externally(tmp_path, monkeypatch):
    """Regression test: emptying the vault outside the MCP left stale index
    rows behind (their files gone, but their paths still UNIQUE-constrained),
    so recreating a previously-used title failed - the exact path is covered
    by test_create_note_reports_an_index_integrity_error_clearly above; this
    checks reconciliation prevents the collision from happening at all."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="topic", title="Alpha Topic", author="nuru")

    (tmp_path / "nuru" / "alpha-topic.md").unlink()  # vault cleared outside the MCP

    result = server.create_note(type="topic", title="Alpha Topic", author="nuru")
    assert result["path"] == "nuru/alpha-topic.md"


# --- append_note ---


def test_append_note_appends_to_the_note_s_body(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both", body="Original.")

    path = server.write_note("More.", note=created["id"])["path"]

    assert path == created["path"]
    assert (tmp_path / path).read_text().endswith("Original.\n\nMore.")


def test_append_note_inserts_a_blank_line_without_the_caller_adding_one(tmp_path, monkeypatch):
    """Regression test: content used to be concatenated directly onto the end
    of the note's last line with no separator at all (e.g. "First real
    note.Appended line one."), corrupting structure on every append."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both", body="First real note.")

    server.write_note("Appended line one.", note=created["id"])

    text = (tmp_path / created["path"]).read_text()
    assert text.endswith("First real note.\n\nAppended line one.")
    assert "note.Appended" not in text


def test_append_note_does_not_add_a_second_blank_line_if_one_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both", body="Original.\n\n")

    server.write_note("More.", note=created["id"])

    text = (tmp_path / created["path"]).read_text()
    assert text.endswith("Original.\n\nMore.")
    assert "\n\n\n" not in text


def test_append_note_completes_a_single_trailing_newline_into_a_blank_line(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both", body="Original.\n")

    server.write_note("More.", note=created["id"])

    text = (tmp_path / created["path"]).read_text()
    assert text.endswith("Original.\n\nMore.")


def test_append_note_twice_separates_both_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both", body="First real note.")

    server.write_note("Appended line one.", note=created["id"])
    server.write_note("- second bullet", note=created["id"])

    text = (tmp_path / created["path"]).read_text()
    assert text.endswith("First real note.\n\nAppended line one.\n\n- second bullet")


def test_write_note_raises_for_an_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.write_note("content", note="01K0000000000000000000NOPE")


def test_write_note_does_not_create_a_note_for_a_typo_d_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.write_note("content", note="01K0000000000000000000TYPO", type="topic", author="nuru")
    assert _files() == []


def test_append_note_raises_if_the_indexed_file_was_deleted_outside_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both")
    (tmp_path / created["path"]).unlink()

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.write_note("content", note=created["id"])


# --- _run_with_timeout ---


def test_run_with_timeout_returns_the_function_result():
    assert server._run_with_timeout(lambda: 42) == 42


def test_run_with_timeout_propagates_the_function_s_exception():
    def _raise():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        server._run_with_timeout(_raise)


def test_run_with_timeout_raises_timeout_error_on_a_stalled_call(monkeypatch):
    import threading

    monkeypatch.setattr(server, "_IO_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()

    def _stall():
        release.wait(timeout=2)  # simulates a hung filesystem call

    try:
        with pytest.raises(TimeoutError):
            server._run_with_timeout(_stall)
    finally:
        release.set()  # let the abandoned worker thread finish so it doesn't leak



# --- rename_or_move_file ---


def test_rename_or_move_file_renames_within_same_directory(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.rename_or_move_file("Welcome.md", "Hello.md")["to"] == "Hello.md"
    assert not (tmp_path / "Welcome.md").exists()
    assert (tmp_path / "Hello.md").read_text() == "content"


def test_rename_or_move_file_moves_to_new_directory(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.rename_or_move_file("Welcome.md", "Projects/Welcome.md")["to"] == "Projects/Welcome.md"
    assert (tmp_path / "Projects" / "Welcome.md").read_text() == "content"


def test_rename_or_move_file_raises_if_source_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.rename_or_move_file("NoSuchNote.md", "Elsewhere.md")


def test_rename_or_move_file_refuses_to_overwrite_existing_destination(tmp_path, monkeypatch):
    (tmp_path / "Source.md").write_text("source content")
    (tmp_path / "Dest.md").write_text("dest content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileExistsError"):
        server.rename_or_move_file("Source.md", "Dest.md")
    assert (tmp_path / "Dest.md").read_text() == "dest content"


def test_rename_or_move_file_rejects_dotdot_traversal_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Welcome.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="ValueError"):
        server.rename_or_move_file("Welcome.md", "../escaped.md")
    assert (vault / "Welcome.md").exists()
    assert not (tmp_path / "escaped.md").exists()


def test_rename_or_move_file_rejects_symlinked_source_pointing_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="ValueError"):
        server.rename_or_move_file("leak.md", "moved.md")
    assert outside.exists()


def test_rename_or_move_file_rejects_source_inside_dot_obsidian(tmp_path, monkeypatch):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "app.json").write_text("{}")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file(".obsidian/app.json", "app.json")
    assert (tmp_path / ".obsidian" / "app.json").exists()


def test_rename_or_move_file_rejects_destination_inside_dot_obsidian(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file("Welcome.md", ".obsidian/hidden.md")


def test_rename_or_move_file_rejects_source_under_user_folder(tmp_path, monkeypatch):
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "note.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file("user/note.md", "note.md")
    assert (tmp_path / "user" / "note.md").exists()


def test_rename_or_move_file_rejects_destination_under_user_folder(tmp_path, monkeypatch):
    (tmp_path / "note.md").write_text("content")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file("note.md", "user/note.md")


def test_rename_or_move_file_keeps_an_indexed_note_reachable_by_id(tmp_path, monkeypatch):
    """Regression test: moving a note used to leave its index row pointing at
    the old path, so append_note("<id>", ...) failed with "indexed at
    <old path>, but that file no longer exists" even though the note still
    exists - just moved. This is what archive-by-move relies on."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="Move Target", author="both", body="Original.")

    new_path = server.rename_or_move_file(created["path"], "shared/moved-target.md")["to"]

    assert server.write_note("Still reachable.", note=created["id"])["path"] == new_path
    assert (tmp_path / new_path).read_text().endswith("Original.\n\nStill reachable.")


def test_rename_or_move_file_survives_index_reconciliation_racing_the_move(tmp_path, monkeypatch):
    """Regression test: _open_index() reconciles against whatever's on disk
    at the moment it's called. rename_or_move_file used to physically move
    the file first and only then open the index to repoint the row - so
    reconciliation, running after the move, saw the *old* path already gone
    and deleted the row before update_path could repoint it, silently
    dropping the note from the index instead of moving it."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both")

    server.rename_or_move_file(created["path"], "shared/moved.md")

    with server._open_index() as conn:
        assert server.note_index.note_exists(conn, created["id"]) is True
        assert server.note_index.get_path(conn, created["id"]) == "shared/moved.md"


def test_rename_or_move_file_of_a_non_indexed_file_is_a_no_op_on_the_index(tmp_path, monkeypatch):
    (tmp_path / "attachment.png").write_bytes(b"data")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    # Must not raise just because nothing in the index matches this path.
    assert server.rename_or_move_file("attachment.png", "moved.png")["to"] == "moved.png"



# --- browse(folder=...): what list_files_in_directory did ---


def _folder_vault(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "nuru/People/alice.md": "---\ntype: person\n---\n# Alice",
        "nuru/People/bob.md": "---\ntype: person\n---\n# Bob",
        "nuru/Topics/rtos.md": "---\ntype: topic\n---\n# RTOS",
        "shared/plan.md": "# Plan",
        "trash/nuru/People/gone.md": "---\ntype: person\n---\n# Gone",
    })
    (tmp_path / "nuru" / "People" / "alice.png").write_bytes(b"x")


def test_browse_a_folder_lists_its_notes_and_its_other_files(tmp_path, monkeypatch):
    _folder_vault(tmp_path, monkeypatch)

    result = server.browse(folder="nuru/People")

    assert result["folder"] == "nuru/People"
    assert sorted(_browse_paths(result)) == ["nuru/People/alice.md", "nuru/People/bob.md"]
    assert result["count"] == 2 and result["files"] == ["nuru/People/alice.png"]


def test_browse_finds_a_folder_by_name_and_takes_a_type_too(tmp_path, monkeypatch):
    _folder_vault(tmp_path, monkeypatch)

    assert sorted(_browse_paths(server.browse(folder="People"))) == ["nuru/People/alice.md", "nuru/People/bob.md"]
    assert _browse_paths(server.browse(folder="nuru", type="topic")) == ["nuru/Topics/rtos.md"]


def test_browse_never_lists_trash(tmp_path, monkeypatch):
    _folder_vault(tmp_path, monkeypatch)

    assert server.browse(folder="trash")["notes"] == []
    assert "trash/nuru/People/gone.md" not in _browse_paths(server.browse(limit=50))


def test_browse_hints_at_an_unknown_folder(tmp_path, monkeypatch):
    _folder_vault(tmp_path, monkeypatch)

    result = server.browse(folder="Recipes")

    assert result["notes"] == [] and result["files"] == []
    assert "No directory matching 'Recipes'" in result["hint"]


# --- browse_notes ---


def _backdate(created_note, created):
    """Rewrite a create_note note's index `created`, so ordering tests don't
    depend on two notes landing in different clock minutes."""
    with server._open_index() as conn:
        conn.execute("UPDATE notes SET created = ? WHERE id = ?", (created, created_note["id"]))


def _touch(path, when):
    """Set a file's modified time to `when`, a naive local datetime."""
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def _browse_paths(result):
    return [note["path"] for note in result["notes"]]


def test_browse_notes_lists_indexed_notes_newest_created_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    old = server.create_note(type="topic", title="Old", author="nuru")
    new = server.create_note(type="topic", title="New", author="nuru")
    mid = server.create_note(type="topic", title="Mid", author="nuru")
    _backdate(old, "2026-09-01T09:00")
    _backdate(mid, "2026-09-10T09:00")
    _backdate(new, "2026-09-18T09:00")

    result = server.browse()

    assert _browse_paths(result) == [new["path"], mid["path"], old["path"]]
    assert result["count"] == 3
    assert "hint" not in result


def test_browse_notes_returns_path_id_type_and_title(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="Nuru MCP", author="both")

    assert server.browse()["notes"] == [
        {"path": created["path"], "id": created["id"], "type": "project", "title": "Nuru MCP"}
    ]


def test_browse_notes_orders_notes_made_in_obsidian_by_modified_time(tmp_path, monkeypatch):
    """The index only knows create_note's notes, so the rest fall back to mtime."""
    (tmp_path / "older.md").write_text("x")
    (tmp_path / "newer.md").write_text("x")
    _touch(tmp_path / "older.md", datetime(2026, 9, 1, 9, 0))
    _touch(tmp_path / "newer.md", datetime(2026, 9, 5, 9, 0))
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _browse_paths(server.browse()) == ["newer.md", "older.md"]


def test_browse_notes_interleaves_indexed_and_obsidian_notes_by_recency(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    indexed = server.create_note(type="topic", title="Indexed", author="nuru")
    _backdate(indexed, "2026-09-10T09:00")
    (tmp_path / "before.md").write_text("x")
    (tmp_path / "after.md").write_text("x")
    _touch(tmp_path / "before.md", datetime(2026, 9, 5, 9, 0))
    _touch(tmp_path / "after.md", datetime(2026, 9, 15, 9, 0))

    assert _browse_paths(server.browse()) == ["after.md", indexed["path"], "before.md"]


def test_browse_notes_orders_an_indexed_note_by_created_not_by_a_later_append(tmp_path, monkeypatch):
    """"What's my latest daily" must not become "the daily I edited last"."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    earlier = server.create_note(type="daily", title="Daily 1", author="user", date="2026-09-01")
    later = server.create_note(type="daily", title="Daily 2", author="user", date="2026-09-02")
    _backdate(earlier, "2026-09-01T09:00")
    _backdate(later, "2026-09-02T09:00")
    server.write_note("edited just now", note=earlier["id"])

    assert _browse_paths(server.browse(type="daily"))[0] == later["path"]


def test_browse_notes_gives_a_note_made_in_obsidian_a_null_id_and_its_filename_as_title(tmp_path, monkeypatch):
    (tmp_path / "Plain.md").write_text("no frontmatter")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.browse()["notes"] == [{"path": "Plain.md", "id": None, "type": None, "title": "Plain"}]


def test_browse_notes_filters_by_type(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    daily = server.create_note(type="daily", title="Daily", author="user", date="2026-09-14")
    server.create_note(type="topic", title="Topic", author="nuru")

    result = server.browse(type="daily")

    assert _browse_paths(result) == [daily["path"]]
    assert result["count"] == 1


def test_browse_notes_type_filter_finds_a_note_made_in_obsidian_from_its_frontmatter(tmp_path, monkeypatch):
    (tmp_path / "2026-09-17.md").write_text("---\ntype: daily\ndate: 2026-09-17\n---\n\n# 2026-09-17\n")
    (tmp_path / "other.md").write_text("---\ntype: topic\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse(type="daily")

    assert result["notes"] == [{"path": "2026-09-17.md", "id": None, "type": "daily", "title": "2026-09-17"}]


def test_browse_notes_tolerates_broken_frontmatter_in_a_note_made_in_obsidian(tmp_path, monkeypatch):
    (tmp_path / "broken.md").write_text("---\ntype: [unclosed\ndate: 2026-13-45\n---\n\nbody\n")
    (tmp_path / "fine.md").write_text("---\ntype: daily\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert sorted(_browse_paths(server.browse())) == ["broken.md", "fine.md"]
    assert _browse_paths(server.browse(type="daily")) == ["fine.md"]


def test_browse_notes_treats_a_blank_type_as_no_filter(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _browse_paths(server.browse(type="  ")) == ["a.md"]


def test_browse_notes_type_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    daily = server.create_note(type="daily", title="Daily", author="user", date="2026-09-14")

    assert _browse_paths(server.browse(type="Daily")) == [daily["path"]]


def test_browse_notes_rejects_an_unknown_type(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="Unknown type 'dailies'"):
        server.browse(type="dailies")


@pytest.mark.parametrize("limit", [0, -3])
def test_browse_notes_rejects_a_limit_below_one(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="limit must be at least 1"):
        server.browse(limit=limit)


def test_browse_notes_caps_at_limit_and_reports_the_true_count(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"note{i}.md").write_text("x")
        _touch(tmp_path / f"note{i}.md", datetime(2026, 9, 1 + i, 9, 0))
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse(limit=2)

    assert _browse_paths(result) == ["note4.md", "note3.md"]
    assert result["count"] == 5
    assert "2 of 5" in result["hint"]
    assert "type" in result["hint"]


def test_browse_notes_defaults_to_ten(tmp_path, monkeypatch):
    for i in range(12):
        (tmp_path / f"note{i:02d}.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse()

    assert len(result["notes"]) == 10
    assert result["count"] == 12


def test_browse_notes_clamps_limit_to_the_maximum(tmp_path, monkeypatch):
    for i in range(server._BROWSE_MAX_LIMIT + 3):
        (tmp_path / f"note{i:03d}.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse(limit=10_000)

    assert len(result["notes"]) == server._BROWSE_MAX_LIMIT
    assert result["count"] == server._BROWSE_MAX_LIMIT + 3
    assert "Raise limit" not in result["hint"]


def test_browse_notes_hints_on_an_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse()

    assert result["count"] == 0
    assert result["notes"] == []
    assert "no notes" in result["hint"]


def test_browse_notes_hints_when_no_note_has_the_type(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.browse(type="daily")

    assert result["notes"] == []
    assert "type: daily" in result["hint"]


def test_browse_notes_excludes_templates_trash_and_hidden_folders(tmp_path, monkeypatch):
    for folder in ("Templates", "trash", ".trash", ".obsidian"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "hidden.md").write_text("x")
    (tmp_path / "kept.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _browse_paths(server.browse()) == ["kept.md"]


def test_browse_notes_excludes_a_symlink_pointing_outside_the_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: daily\n---\nTOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server.browse()["notes"] == []


def test_browse_notes_does_not_create_the_index_file(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    server.browse()

    assert not (tmp_path / server._INDEX_DB_NAME).exists()


# --- notes_with_tag / all_tags ---


def test_notes_with_tag_finds_a_note_by_its_frontmatter_tag(tmp_path, monkeypatch):
    """create_note writes tags only into frontmatter, never as #tags."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="Nuru MCP", author="both", tags=["mcp"])
    server.create_note(type="topic", title="Other", author="nuru")

    result = server.notes_with_tag("mcp")

    assert result["count"] == 1
    assert result["matches"] == [
        {"path": created["path"], "id": created["id"], "type": "project", "title": "Nuru MCP"}
    ]


def test_notes_with_tag_matches_the_type_tag_create_note_always_adds(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="X", author="both")

    assert [m["path"] for m in server.notes_with_tag("project")["matches"]] == [created["path"]]


def test_notes_with_tag_finds_an_inline_hash_tag(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("Some text #router and more")
    (tmp_path / "b.md").write_text("Nothing here")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert [m["path"] for m in server.notes_with_tag("router")["matches"]] == ["a.md"]


@pytest.mark.parametrize("query", ["#Router", "ROUTER", " router ", "#router"])
def test_notes_with_tag_ignores_case_a_leading_hash_and_whitespace(tmp_path, monkeypatch, query):
    (tmp_path / "a.md").write_text("---\ntags:\n  - Router\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.notes_with_tag(query)

    assert [m["path"] for m in result["matches"]] == ["a.md"]
    assert result["tag"] == "router"


def test_notes_with_tag_matches_nested_tags_under_a_parent(tmp_path, monkeypatch):
    (tmp_path / "nested.md").write_text("#project/alpha")
    (tmp_path / "lookalike.md").write_text("#projector")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert [m["path"] for m in server.notes_with_tag("project")["matches"]] == ["nested.md"]
    assert [m["path"] for m in server.notes_with_tag("project/alpha")["matches"]] == ["nested.md"]
    assert server.notes_with_tag("project/beta")["matches"] == []


def test_notes_with_tag_reads_a_tags_string_and_ignores_malformed_tags_values(tmp_path, monkeypatch):
    (tmp_path / "csv.md").write_text("---\ntags: alpha, beta\n---\n")
    (tmp_path / "junk.md").write_text("---\ntags: {a: 1}\n---\n")
    (tmp_path / "broken.md").write_text("---\ntags: [unclosed\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert [m["path"] for m in server.notes_with_tag("beta")["matches"]] == ["csv.md"]


def test_notes_with_tag_does_not_treat_a_heading_or_frontmatter_hash_as_a_tag(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("---\ntitle: 'Issue #123'\n---\n\n# Heading\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.all_tags()["tags"] == {}


def test_notes_with_tag_lists_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    old = server.create_note(type="topic", title="Old", author="nuru", tags=["x"])
    new = server.create_note(type="topic", title="New", author="nuru", tags=["x"])
    _backdate(old, "2026-09-01T09:00")
    _backdate(new, "2026-09-09T09:00")

    assert [m["path"] for m in server.notes_with_tag("x")["matches"]] == [new["path"], old["path"]]


def test_notes_with_tag_caps_results_and_reports_the_true_count(tmp_path, monkeypatch):
    for i in range(server._SEARCH_LIMIT + 4):
        (tmp_path / f"n{i:02d}.md").write_text("#many")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.notes_with_tag("many")

    assert len(result["matches"]) == server._SEARCH_LIMIT
    assert result["count"] == server._SEARCH_LIMIT + 4
    assert "hint" in result


def test_notes_with_tag_suggests_close_tags_on_a_miss(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("#project #router")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.notes_with_tag("projects")

    assert result["count"] == 0
    assert result["matches"] == []
    assert "Did you mean: project" in result["hint"]
    assert "Call tags with no arguments" in result["hint"]


def test_notes_with_tag_hints_without_suggestions_when_nothing_is_close(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("#router")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    hint = server.notes_with_tag("zzzzzz")["hint"]

    assert "Did you mean" not in hint
    assert "Call tags with no arguments" in hint


@pytest.mark.parametrize("query", ["", "  ", "#"])
def test_notes_with_tag_rejects_an_empty_tag(tmp_path, monkeypatch, query):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="tag is empty"):
        server.notes_with_tag(query)


def test_notes_with_tag_excludes_templates_and_trash(tmp_path, monkeypatch):
    for folder in ("Templates", "trash"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "x.md").write_text("#profile")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.notes_with_tag("profile")["matches"] == []


def test_all_tags_counts_notes_per_tag_most_used_first(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("---\ntags:\n  - project\n  - mcp\n---\n#router")
    (tmp_path / "b.md").write_text("---\ntags:\n  - project\n---\n#router #router")
    (tmp_path / "c.md").write_text("#project")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.all_tags()

    # b mentions #router twice but is still one note; project is on 3 notes.
    assert result["tags"] == {"project": 3, "router": 2, "mcp": 1}
    assert list(result["tags"]) == ["project", "router", "mcp"]
    assert result["count"] == 3


def test_all_tags_merges_case_variants(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("#Router")
    (tmp_path / "b.md").write_text("#router")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.all_tags()["tags"] == {"router": 2}


def test_all_tags_agrees_with_notes_with_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="topic", title="A", author="nuru", tags=["shared-tag"])
    server.create_note(type="topic", title="B", author="nuru", tags=["shared-tag"])

    assert server.all_tags()["tags"]["shared-tag"] == server.notes_with_tag("shared-tag")["count"] == 2


def test_all_tags_lists_the_tags_of_notes_create_note_made(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="daily", title="D", author="user", date="2026-09-14", tags=["sample"])

    assert set(server.all_tags()["tags"]) == {"daily", "sample"}


def test_all_tags_hints_on_an_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.all_tags()

    assert result["tags"] == {}
    assert result["count"] == 0
    assert "any tags" in result["hint"]


def test_all_tags_caps_the_list_and_reports_the_true_count(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text(" ".join(f"#tag{i:03d}" for i in range(server._LIST_LIMIT + 5)))
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.all_tags()

    assert len(result["tags"]) == server._LIST_LIMIT
    assert result["count"] == server._LIST_LIMIT + 5
    assert "hint" in result


def test_all_tags_excludes_templates_and_trash(tmp_path, monkeypatch):
    for folder in ("Templates", "trash"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "x.md").write_text("#profile")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.all_tags()["tags"] == {}


# --- tags: all_tags, notes_with_tag and note_tags in one tool ---


def test_tags_with_no_arguments_lists_every_tag(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a.md": "#router #mcp", "b.md": "#router"})

    assert server.tags()["tags"] == {"router": 2, "mcp": 1}


def test_tags_with_a_tag_lists_the_notes_carrying_it(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a.md": "#router #mcp", "b.md": "#router"})

    assert sorted(m["path"] for m in server.tags(tag="#Router")["matches"]) == ["a.md", "b.md"]


def test_tags_with_a_note_lists_its_tags(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a.md": "---\ntags:\n  - project\n---\n#router"})

    assert server.tags(note="a") == {"note": "a.md", "tags": ["project", "router"]}


def test_tags_refuses_a_tag_and_a_note_together(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a.md": "#router"})

    with pytest.raises(ToolError, match="Pass tag or note, not both"):
        server.tags(tag="router", note="a")


# --- note_info: get_links, note_tags and note_todos in one call ---


def test_note_info_gives_tags_links_both_ways_and_open_tasks(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "---\ntags:\n  - project\n---\n# Hub\n[[Alpha]] and [[Missing]] #router\n- [ ] call Bob\n- [x] done\nTODO: ship",
        "Alpha.md": "# Alpha",
        "fan.md": "see [[hub]]",
    })

    info = server.note_info("hub")

    assert info["note"] == "hub.md"
    assert info["tags"] == ["project", "router"]
    assert info["outgoing"] == [{"link": "Alpha", "path": "Alpha.md"}, {"link": "Missing", "path": None}]
    assert info["backlinks"] == ["fan.md"]
    assert info["todos"] == [{"line": 7, "text": "- [ ] call Bob"}, {"line": 9, "text": "TODO: ship"}]
    assert info["todo_count"] == 2
    assert "path null matches no note" in info["hint"]


def test_note_info_does_not_return_the_notes_text(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"hub.md": "# Hub\nsecret body text"})

    assert "secret body text" not in json.dumps(server.note_info("hub"))


def test_note_info_resolves_the_name_once_so_every_part_is_the_same_note(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a/notes.md": "#one\n- [ ] a", "b/notes.md": "#two\n- [ ] b"})

    info = server.note_info("notes")

    assert info["note"] == "a/notes.md" and info["tags"] == ["one"] and info["todos"][0]["text"] == "- [ ] a"


def test_note_info_on_an_unknown_note_names_the_closest(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"Plan.md": "x"})

    with pytest.raises(ToolError, match="Closest matches: Plan.md"):
        server.note_info("Plann")


# --- read_note gives the text, note_info what surrounds it ---


def test_read_note_gives_plain_markdown_not_a_json_string(tmp_path, monkeypatch):
    import asyncio

    _vault_with(tmp_path, monkeypatch, {"hub.md": 'line one\nsays "quoted"'})

    assert server.read_note("hub") == 'line one\nsays "quoted"'
    result = asyncio.run(server.mcp.call_tool("read_note", {"name": "hub"}))
    assert result.content[0].text == 'line one\nsays "quoted"'  # not a JSON string with escapes


# --- read_note: forgiving name matching ---


def test_read_note_ignores_case_in_the_filename(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("WELCOME") == "Hello"


@pytest.mark.parametrize("name", ["nuru mcp", "Nuru-MCP", "nuru_mcp.md", "NURU MCP"])
def test_read_note_matches_a_slug_of_the_filename(tmp_path, monkeypatch, name):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Nuru MCP.md").write_text("the note")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note(name) == "the note"


def test_read_note_finds_a_note_by_the_title_create_note_gave_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="project", title="Original Title", author="both", body="content here")
    server.rename_or_move_file(created["path"], "shared/renamed-since.md")

    assert "content here" in server.read_note("Original Title")
    assert "content here" in server.read_note("original title")


def test_read_note_finds_a_note_by_an_alias_in_its_frontmatter(tmp_path, monkeypatch):
    (tmp_path / "x.md").write_text("---\naliases:\n  - Fancy Name\n  - Other\n---\n\nbody")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("fancy name").endswith("body")
    assert server.read_note("other").endswith("body")


def test_read_note_finds_a_note_by_its_id_in_any_case(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Some Topic", author="nuru", body="topic body")
    server.rename_or_move_file(created["path"], "nuru/moved-away.md")

    assert "topic body" in server.read_note(created["id"])
    assert "topic body" in server.read_note(created["id"].lower())


def test_read_note_finds_a_note_by_a_frontmatter_id_the_index_does_not_know(tmp_path, monkeypatch):
    (tmp_path / "typed.md").write_text("---\nid: 01ABCDEF\n---\n\nbody")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("01abcdef").endswith("body")


def test_read_note_finds_the_right_name_under_a_wrong_folder(tmp_path, monkeypatch):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "plan.md").write_text("the plan")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("nuru/plan.md") == "the plan"


def test_read_note_prefers_an_exact_filename_over_a_forgiving_match(tmp_path, monkeypatch):
    (tmp_path / "Alpha.md").write_text("the file")
    (tmp_path / "other.md").write_text("---\naliases:\n  - Alpha\n---\n\nthe alias")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("Alpha") == "the file"


def test_read_note_prefers_a_filename_match_over_an_alias_match(tmp_path, monkeypatch):
    (tmp_path / "a-note.md").write_text("---\naliases:\n  - beta\n---\n\nthe alias")
    (tmp_path / "Beta.md").write_text("the file")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("BETA") == "the file"


def test_read_note_breaks_a_tie_by_taking_the_first_path_alphabetically(tmp_path, monkeypatch):
    (tmp_path / "b.md").write_text("---\naliases:\n  - Twin\n---\n\nfrom b")
    (tmp_path / "a.md").write_text("---\naliases:\n  - Twin\n---\n\nfrom a")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.read_note("twin").endswith("from a")


def test_read_note_forgiving_match_skips_templates(tmp_path, monkeypatch):
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Templates" / "01 Profile.md").write_text("# {{title}}")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("01-profile")


def test_read_note_forgiving_match_skips_hidden_folders(tmp_path, monkeypatch):
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "Deleted Note.md").write_text("gone")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("deleted note")


def test_read_note_forgiving_match_does_not_reach_a_symlink_pointing_outside_the_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\naliases:\n  - Leaky Alias\n---\nTOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError") as exc_info:
        server.read_note("leaky alias")
    assert "TOP SECRET" not in str(exc_info.value)
    assert "leak.md" not in str(exc_info.value)


def test_read_note_miss_names_the_closest_notes(tmp_path, monkeypatch):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Nuru MCP.md").write_text("x")
    (tmp_path / "Unrelated.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError") as exc_info:
        server.read_note("Nuru MCPP")

    message = str(exc_info.value)
    assert "No note found named 'Nuru MCPP'" in message
    assert "Projects/Nuru MCP.md" in message
    assert "Unrelated.md" not in message
    assert "Use browse or search" in message


def test_read_note_miss_names_at_most_three_candidates(tmp_path, monkeypatch):
    for suffix in "abcdef":
        (tmp_path / f"router-{suffix}.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError) as exc_info:
        server.read_note("router")

    assert str(exc_info.value).count("router-") == 3


def test_read_note_miss_suggests_a_note_when_the_name_is_a_mistyped_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Battery Life", author="nuru")
    typo = created["id"][:-1] + ("A" if created["id"][-1] != "A" else "B")

    with pytest.raises(ToolError) as exc_info:
        server.read_note(typo)

    assert created["path"] in str(exc_info.value)


def test_read_note_miss_says_so_plainly_when_nothing_is_close(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError) as exc_info:
        server.read_note("qqqqqqqqqq")

    message = str(exc_info.value)
    assert "No note found named 'qqqqqqqqqq'" in message
    assert "Closest" not in message


def test_read_note_miss_in_an_empty_vault_still_raises_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note("anything")


@pytest.mark.parametrize("name", ["", "   ", "🙂"])
def test_read_note_a_name_with_nothing_to_match_on_is_a_miss_not_a_crash(tmp_path, monkeypatch, name):
    (tmp_path / "Welcome.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.read_note(name)


# --- read_note: section ---

_SECTIONED = """\
---
type: project
---

# Title

Intro line.

## Goal

Ship it.

### Sub goal

A detail.

## Decisions

- chose A

## Blocked on

- nothing
"""


def _write_sectioned(tmp_path, monkeypatch, text=_SECTIONED):
    (tmp_path / "plan.md").write_text(text)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)


def test_read_note_section_returns_the_heading_through_to_the_next_same_level_heading(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section="Decisions") == "## Decisions\n\n- chose A"


def test_read_note_section_keeps_the_sections_own_subheadings(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section="Goal") == "## Goal\n\nShip it.\n\n### Sub goal\n\nA detail."


def test_read_note_section_runs_to_the_end_of_the_note_for_the_last_heading(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section="Blocked on") == "## Blocked on\n\n- nothing"


def test_read_note_section_ends_a_deep_section_at_a_higher_level_heading(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section="Sub goal") == "### Sub goal\n\nA detail."


def test_read_note_section_can_be_the_top_level_heading_and_take_everything_under_it(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    result = server.read_note("plan", section="Title")

    assert result.startswith("# Title")
    assert result.endswith("- nothing")


@pytest.mark.parametrize("section", ["decisions", "DECISIONS", "## Decisions", "  Decisions  ", "#Decisions"])
def test_read_note_section_ignores_case_whitespace_and_leading_hashes(tmp_path, monkeypatch, section):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section=section) == "## Decisions\n\n- chose A"


def test_read_note_section_collapses_inner_whitespace(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section="Blocked   on").startswith("## Blocked on")


def test_read_note_section_ignores_a_closing_hash_sequence_on_the_heading(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch, "## Decisions ##\n\nbody\n")

    assert server.read_note("plan", section="Decisions") == "## Decisions ##\n\nbody"


def test_read_note_section_does_not_end_at_a_hash_line_inside_a_code_block(tmp_path, monkeypatch):
    _write_sectioned(
        tmp_path, monkeypatch, "## Setup\n\n```bash\n# install it\nmake\n```\n\nafter code\n\n## Next\n\nx\n"
    )

    result = server.read_note("plan", section="Setup")

    assert "# install it" in result
    assert result.endswith("after code")


def test_read_note_section_does_not_find_a_heading_inside_a_code_block(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch, "## Real\n\n~~~\n## Fake\n~~~\n")

    with pytest.raises(ToolError, match="No section 'Fake'"):
        server.read_note("plan", section="Fake")


def test_read_note_section_does_not_find_a_heading_inside_frontmatter(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch, "---\n# comment in frontmatter\ntype: topic\n---\n\n## Real\n\nx\n")

    with pytest.raises(ToolError, match="No section 'comment in frontmatter'"):
        server.read_note("plan", section="comment in frontmatter")


def test_read_note_section_takes_the_first_of_two_headings_with_the_same_text(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch, "## Notes\n\nfirst\n\n## Notes\n\nsecond\n")

    assert server.read_note("plan", section="Notes") == "## Notes\n\nfirst"


def test_read_note_unknown_section_lists_the_notes_headings(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    with pytest.raises(ToolError, match="ValueError") as exc_info:
        server.read_note("plan", section="Nope")

    message = str(exc_info.value)
    assert "No section 'Nope' in 'plan.md'" in message
    for heading in ("'Title'", "'Goal'", "'Sub goal'", "'Decisions'", "'Blocked on'"):
        assert heading in message


def test_read_note_unknown_section_on_a_note_with_no_headings_says_so(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch, "just text\n")

    with pytest.raises(ToolError, match="no headings"):
        server.read_note("plan", section="Anything")


@pytest.mark.parametrize("section", [None, "", "   "])
def test_read_note_without_a_usable_section_returns_the_whole_note(tmp_path, monkeypatch, section):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("plan", section=section) == _SECTIONED


def test_read_note_section_works_through_a_forgiving_name(tmp_path, monkeypatch):
    _write_sectioned(tmp_path, monkeypatch)

    assert server.read_note("PLAN", section="decisions") == "## Decisions\n\n- chose A"


def test_read_note_section_is_optional_in_the_tool_schema():
    import asyncio

    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "read_note")

    assert tool.input_schema["required"] == ["name"]
    assert "section" in tool.input_schema["properties"]


# --- create_note: server-side defaults for date and session ---


def _today_is(monkeypatch, day):
    monkeypatch.setattr(server, "_today", lambda: day)


def test_create_note_defaults_a_daily_date_to_today(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="daily", title="Daily 2026-09-20", author="user")

    assert "date: '2026-09-20'\n" in (tmp_path / created["path"]).read_text()


def test_create_note_keeps_an_explicit_daily_date(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="daily", title="Backfill", author="user", date="2026-01-05")

    assert "date: '2026-01-05'\n" in (tmp_path / created["path"]).read_text()


def test_create_note_defaults_the_first_session_of_the_day_to_01(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="transcript", title="First", author="nuru")

    assert "session: 2026-09-20-01\n" in (tmp_path / created["path"]).read_text()


def test_create_note_defaults_a_later_session_to_the_next_number(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    server.create_note(type="transcript", title="First", author="nuru")
    server.create_note(type="transcript", title="Second", author="nuru")

    third = server.create_note(type="transcript", title="Third", author="nuru")

    assert "session: 2026-09-20-03\n" in (tmp_path / third["path"]).read_text()


def test_create_note_default_session_goes_one_past_the_highest_not_into_a_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    server.create_note(type="transcript", title="One", author="nuru", session="2026-09-20-01")
    server.create_note(type="transcript", title="Three", author="nuru", session="2026-09-20-03")

    created = server.create_note(type="transcript", title="Next", author="nuru")

    assert "session: 2026-09-20-04\n" in (tmp_path / created["path"]).read_text()


def test_create_note_default_session_ignores_other_days(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    server.create_note(type="transcript", title="Yesterday", author="nuru", session="2026-09-19-07")

    created = server.create_note(type="transcript", title="Today", author="nuru")

    assert "session: 2026-09-20-01\n" in (tmp_path / created["path"]).read_text()


def test_create_note_default_session_is_counted_per_type(tmp_path, monkeypatch):
    """A transcript and the session note of one conversation share an id."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    server.create_note(type="transcript", title="Transcript", author="nuru")

    created = server.create_note(type="session", title="Session note", author="nuru")

    assert "session: 2026-09-20-01\n" in (tmp_path / created["path"]).read_text()


def test_create_note_default_session_counts_notes_made_in_obsidian(tmp_path, monkeypatch):
    (tmp_path / "typed.md").write_text("---\ntype: session\nsession: 2026-09-20-02\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="session", title="Next", author="nuru")

    assert "session: 2026-09-20-03\n" in (tmp_path / created["path"]).read_text()


def test_create_note_default_session_counts_an_archived_note(tmp_path, monkeypatch):
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "old.md").write_text("---\ntype: session\nsession: 2026-09-20-01\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="session", title="Next", author="nuru")

    assert "session: 2026-09-20-02\n" in (tmp_path / created["path"]).read_text()


def test_create_note_default_session_refuses_a_hundredth_session_in_a_day(tmp_path, monkeypatch):
    (tmp_path / "typed.md").write_text("---\ntype: session\nsession: 2026-09-20-99\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    with pytest.raises(ToolError, match="99"):
        server.create_note(type="session", title="Too many", author="nuru")


def test_create_note_keeps_an_explicit_session(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    created = server.create_note(type="transcript", title="Old", author="nuru", session="2026-09-01-05")

    assert "session: 2026-09-01-05\n" in (tmp_path / created["path"]).read_text()


def test_create_note_wrong_extra_field_error_does_not_show_a_defaulted_value(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    with pytest.raises(ToolError, match=r"expects exactly these extra fields: 'date'; got \['session'\]"):
        server.create_note(type="daily", title="X", author="user", session="2026-09-20-01")


def test_create_note_defaults_do_not_apply_to_types_that_take_no_extra_field(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    created = server.create_note(type="project", title="Plain", author="both")

    text = (tmp_path / created["path"]).read_text()
    assert "date:" not in text
    assert "session:" not in text


def test_write_note_defaults_leave_the_schema_required_set_unchanged():
    import asyncio

    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "write_note")

    assert tool.input_schema.get("required", []) == []
    assert tool.input_schema["properties"]["mode"]["default"] == "append"


# --- write_note: create or append ---


def test_write_note_creates_a_note_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.write_note("first words", note="Garden Plan", type="topic", author="nuru", tags=["garden"])

    assert result["created"] is True and result["path"] == "nuru/garden-plan.md"
    text = (tmp_path / result["path"]).read_text()
    assert "# Garden Plan\n\nfirst words" in text and "  - garden\n" in text


def test_write_note_appends_to_a_note_found_by_its_title(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Garden Plan", author="nuru", body="first")

    result = server.write_note("second", note="garden plan", type="topic", author="nuru")

    assert _no_done(result) == {"path": created["path"], "id": created["id"], "created": False}
    assert (tmp_path / created["path"]).read_text().endswith("first\n\nsecond")
    assert _notes() == ["nuru/garden-plan.md"]


def test_write_note_appends_to_a_note_made_in_obsidian_by_its_path(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"Projects/Plan.md": "# Plan"})

    result = server.write_note("more", note="Projects/Plan.md")

    assert _no_done(result) == {"path": "Projects/Plan.md", "id": None, "created": False}


def test_write_note_refuses_a_name_that_matches_several_notes(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a/notes.md": "1", "b/notes.md": "2"})

    with pytest.raises(ToolError, match="matches 2 notes: a/notes.md, b/notes.md"):
        server.write_note("more", note="notes")
    assert (tmp_path / "a/notes.md").read_text() == "1" and (tmp_path / "b/notes.md").read_text() == "2"


def test_write_note_refuses_a_path_that_matches_nothing_instead_of_creating_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="person", title="Priya Raman", author="nuru")

    with pytest.raises(ToolError, match="nuru/priya-raman.md has nearly the same name"):
        server.write_note("more", note="nuru/People/priya-ramen.md", type="person", author="nuru")
    assert _notes() == ["nuru/priya-raman.md"]


def test_a_new_note_defaults_to_a_topic_by_nuru_in_nuru(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.write_note("more", note="New Thing")

    assert result["path"] == "nuru/new-thing.md" and result["done"] == "Created the topic note 'New Thing' at nuru/new-thing.md."
    text = (tmp_path / result["path"]).read_text()
    assert "type: topic\n" in text and "author: nuru\n" in text


def test_a_correction_never_appends_to_the_note_it_corrects(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    wrong = server.create_note(type="topic", title="Lead Time", author="nuru", body="26 weeks")

    with pytest.raises(ToolError, match="nuru/lead-time.md' already exists"):
        server.write_note("20 weeks", note="Lead Time", type="correction", author="nuru", targets=[wrong["id"]])
    assert (tmp_path / wrong["path"]).read_text().endswith("26 weeks")


def test_write_note_refuses_supersedes_on_an_existing_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    old = server.create_note(type="topic", title="Old", author="nuru")
    server.create_note(type="topic", title="New", author="nuru", body="x")

    with pytest.raises(ToolError, match="supersedes and targets only apply to a new note"):
        server.write_note("more", note="New", supersedes=old["id"])


# --- write_note: modes, empty notes and what the result says ---


def test_replace_rewrites_the_body_and_keeps_the_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = server.create_note(type="topic", title="Plan", author="nuru", body="old line")
    before = (tmp_path / made["path"]).read_text()
    frontmatter = before[: before.index("---\n", 4) + 4]

    result = server.write_note("# Plan\n\nnew line", note="Plan", mode="replace")

    assert result["done"] == ("Replaced the text of the existing note 'Plan' at nuru/plan.md (its frontmatter kept; "
                              "the old version is in trash/).")
    assert (tmp_path / made["path"]).read_text() == frontmatter + "\n# Plan\n\nnew line"
    assert server.read_note(made["id"]).endswith("new line")


def test_replace_with_nothing_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="topic", title="Plan", author="nuru", body="keep me")

    with pytest.raises(ToolError, match="nothing to add.*trash"):
        server.write_note("", note="Plan", mode="replace")
    assert server.read_note("Plan").endswith("keep me")


def test_an_unknown_mode_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="Unknown mode 'overwrite'"):
        server.write_note("x", note="Plan", mode="overwrite", type="topic", author="nuru")


def test_replace_obeys_the_same_guards_as_append(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server, "_PROTECT_USER_AUTHORED", True)
    server.create_note(type="topic", title="Mine", author="user", body="original")
    _vault_with(tmp_path, monkeypatch, {"archive/nuru/old.md": "---\nauthor: nuru\n---\nold"})

    with pytest.raises(ToolError, match="written by the user"):
        server.write_note("gone", note="Mine", mode="replace")
    with pytest.raises(ToolError, match="is archived"):
        server.write_note("gone", note="old", mode="replace")


def test_a_new_note_can_be_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.write_note(note="Reading List", type="topic", author="nuru")

    assert result["created"] is True
    assert result["done"] == "Created the topic note 'Reading List' at nuru/reading-list.md."
    assert (tmp_path / result["path"]).read_text().endswith("# Reading List\n\n")


def test_an_append_says_which_note_and_where(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="person", title="Alice Chen", author="nuru")

    result = server.write_note("New number: 555-0102", note="Alice Chen", type="person", author="nuru")

    assert result["done"] == "Added to the end of the existing note 'Alice Chen' at nuru/alice-chen.md."


def test_a_move_says_what_happened(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"nuru/a.md": "a", "nuru/b.md": "b", "nuru/c.md": "c"})

    assert server.rename_or_move_file("nuru/a.md", "nuru/z.md")["done"] == "Moved 'z' from nuru/a.md to nuru/z.md."
    assert server.rename_or_move_file("nuru/b.md", "archive")["done"].startswith("Archived 'b': moved nuru/b.md to archive/nuru/b.md.")
    assert server.rename_or_move_file("nuru/c.md", "trash")["done"].startswith("Deleted 'c': moved nuru/c.md to trash/nuru/c.md.")


def test_a_missing_note_says_how_to_name_one(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match='note is missing - pass note="<a short title>"'):
        server.write_note("Flights: 400 EUR", type="topic", author="nuru")


# --- write_note: free paths under nuru/ and shared/, and the guards that come with them ---


def test_a_new_note_goes_where_its_path_says_under_nuru_or_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    a = server.write_note("hi", note="nuru/People/Dana Lee", type="person")
    b = server.write_note("plan", note="shared/Garden/Raised Beds.md", type="project", author="both")

    assert a["path"] == "nuru/People/dana-lee.md" and "type: person\n" in (tmp_path / a["path"]).read_text()
    assert b["path"] == "shared/Garden/raised-beds.md"
    assert server.read_note("Dana Lee").endswith("hi")


@pytest.mark.parametrize("path", ["Recipes/Pancakes", "archive/nuru/Old", "trash/nuru/Gone", "Templates/X", ".obsidian/x"])
def test_a_new_note_outside_nuru_and_shared_is_refused(tmp_path, monkeypatch, path):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="new notes go in a folder under nuru/ or shared/"):
        server.write_note("x", note=path)
    assert _files() == []


def test_a_name_differing_only_in_digits_is_a_new_note_not_a_typo(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.write_note("x", note="Standup 2026-10-02", type="session")

    assert server.write_note("y", note="Standup 2026-10-03", type="session")["created"] is True


def test_a_near_name_is_refused_as_a_likely_typo(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.write_note("x", note="Alice Chen", type="person")

    with pytest.raises(ToolError, match="nuru/alice-chen.md has nearly the same name"):
        server.write_note("y", note="Alice Chenn", type="person")


def test_an_append_that_repeats_the_note_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = server.create_note(type="topic", title="Snippets", author="nuru",
                              body="First snippet explains the ring buffer.\n\nSecond snippet is the ISR.")
    before = (tmp_path / made["path"]).read_text()

    with pytest.raises(ToolError, match='mode="replace" and the complete new text'):
        server.write_note("# Snippets\n\nFirst snippet explains the ring buffer.\n\nSecond snippet is the ISR.\n\nNew line",
                          note="Snippets")
    assert (tmp_path / made["path"]).read_text() == before
    assert server.write_note("New line", note="Snippets")["created"] is False


def test_a_type_given_for_an_existing_note_is_ignored_unless_it_is_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.write_note("x", note="Garden", type="topic")

    assert server.write_note("y", note="Garden", type="project")["created"] is False
    assert "type: topic\n" in server.read_note("Garden")
    with pytest.raises(ToolError, match="not a correction"):
        server.write_note("z", note="Garden", type="correction")


# --- corrections: Nuru's own notes are fixed in place, the rest get an addendum ---


def test_a_replace_keeps_the_old_version_in_trash_where_no_tool_sees_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = server.create_note(type="topic", title="Plan", author="nuru", body="old line")
    old = (tmp_path / made["path"]).read_text()

    server.write_note("# Plan\n\nnew line", note="Plan", mode="replace")

    [copy] = (tmp_path / "trash" / "nuru").glob("plan.*.md")
    assert copy.read_text() == old
    assert _notes() == ["nuru/plan.md"] and server.search("old line")["exact_count"] == 0


def test_a_replace_with_the_same_text_changes_nothing_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = server.create_note(type="topic", title="Plan", author="nuru", body="keep me")
    body = (tmp_path / made["path"]).read_text().split("---\n", 2)[2]

    result = server.write_note(body, note="Plan", mode="replace")

    assert result["done"].startswith("Nothing changed: the new text for 'Plan'")
    assert not (tmp_path / "trash").exists()


def test_only_notes_in_nuru_can_be_replaced_on_nurus_server(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server, "_PROTECT_USER_AUTHORED", True)
    server.write_note("joint plan", note="shared/Garden Plan", type="project", author="both")

    with pytest.raises(ToolError, match="isn't in nuru/, so it can't be replaced"):
        server.write_note("rewritten", note="shared/garden-plan.md", mode="replace")
    assert server.write_note("one more line", note="shared/garden-plan.md")["created"] is False


def test_a_correction_finds_the_notes_it_corrects_from_its_links(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    wrong = server.create_note(type="topic", title="FreeRTOS Task Management", author="user", body="U = 0.58")
    _vault_with(tmp_path, monkeypatch, {"nuru/obsidian-made.md": "---\nid: 01OBSIDIANMADEID000000000AB\n---\n# X"})

    result = server.write_note("[[FreeRTOS Task Management]] says 0.58; the table adds up to 0.36. Also [[obsidian-made]].",
                               note="Correction: FreeRTOS utilisation", type="correction")

    text = (tmp_path / result["path"]).read_text()
    assert f"Corrects: [[{wrong['id']}]], [[01OBSIDIANMADEID000000000AB]]" in text
    assert result["path"] in server.note_info(wrong["id"])["backlinks"]


def test_a_correction_that_links_no_note_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="A correction must link the note it corrects"):
        server.write_note("the number is wrong", note="Correction: something", type="correction")
    assert _files() == []


# --- write_note(type="daily"): Nuru's own daily, in nuru/ ---


def test_add_to_daily_creates_todays_daily_when_there_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    result = server.write_note("- 09:00 Coffee", type="daily")

    assert result["created"] is True
    assert result["path"] == "nuru/daily-2026-09-20.md"
    text = (tmp_path / result["path"]).read_text()
    assert "type: daily\n" in text
    assert "date: '2026-09-20'\n" in text
    assert "author: nuru\n" in text
    assert "# Daily 2026-09-20\n\n- 09:00 Coffee" in text


def test_add_to_daily_returns_the_id_of_the_note_it_created(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    result = server.write_note("x", type="daily")

    assert server.write_note("y", note=result["id"])["path"] == result["path"]


def test_add_to_daily_appends_to_an_existing_daily_instead_of_making_another(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    first = server.write_note("first", type="daily")

    second = server.write_note("second", type="daily")

    assert _no_done(second) == {"path": first["path"], "id": first["id"], "created": False}
    assert (tmp_path / first["path"]).read_text().endswith("first\n\nsecond")
    assert len(list((tmp_path / "nuru").glob("*.md"))) == 1


def test_add_to_daily_finds_a_daily_create_note_made_under_any_title(tmp_path, monkeypatch):
    """The real vault has a daily titled just "Daily Note" - so no filename guess finds it."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    made = server.create_note(type="daily", title="Daily Note", author="user", date="2026-09-20", body="earlier")

    result = server.write_note("later", type="daily")

    assert _no_done(result) == {"path": made["path"], "id": made["id"], "created": False}
    assert (tmp_path / made["path"]).read_text().endswith("earlier\n\nlater")


def test_add_to_daily_ignores_a_daily_for_another_date(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    yesterday = server.create_note(type="daily", title="Yesterday", author="user", date="2026-09-19")

    result = server.write_note("today's entry", type="daily")

    assert result["created"] is True
    assert result["path"] != yesterday["path"]
    assert "today's entry" not in (tmp_path / yesterday["path"]).read_text()


def test_add_to_daily_ignores_a_non_daily_note_that_mentions_todays_date(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")
    server.create_note(type="project", title="Launch 2026-09-20", author="both", body="due 2026-09-20")

    assert server.write_note("x", type="daily")["created"] is True


def test_add_to_daily_appends_to_a_daily_made_in_obsidian_with_an_unquoted_date(tmp_path, monkeypatch):
    (tmp_path / "nuru").mkdir()
    (tmp_path / "nuru" / "2026-09-20.md").write_text("---\ntype: daily\ndate: 2026-09-20\n---\n\n# 2026-09-20\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    result = server.write_note("typed elsewhere", type="daily")

    assert _no_done(result) == {"path": "nuru/2026-09-20.md", "id": None, "created": False}
    assert (tmp_path / "nuru" / "2026-09-20.md").read_text().endswith("# 2026-09-20\n\ntyped elsewhere")
    assert not (tmp_path / "nuru" / "daily-2026-09-20.md").exists()


def test_the_users_own_daily_in_user_is_never_written(tmp_path, monkeypatch):
    (tmp_path / "user").mkdir()
    users = tmp_path / "user" / "2026-09-20.md"
    users.write_text("---\ntype: daily\ndate: 2026-09-20\nauthor: user\n---\n\n# 2026-09-20\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    result = server.write_note("mine", type="daily")

    assert result["created"] is True and result["path"] == "nuru/daily-2026-09-20.md"
    assert users.read_text().endswith("# 2026-09-20\n")


def test_add_to_daily_survives_a_note_with_broken_frontmatter_mentioning_the_date(tmp_path, monkeypatch):
    (tmp_path / "broken.md").write_text("---\ntype: [oops\ndate: 2026-09-20\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    assert server.write_note("x", type="daily")["created"] is True


def test_add_to_daily_uses_one_date_for_the_whole_call(tmp_path, monkeypatch):
    """A call that starts before midnight must not stamp two different dates."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    days = iter(["2026-09-20", "2026-09-21", "2026-09-21"])
    monkeypatch.setattr(server, "_today", lambda: next(days))

    result = server.write_note("x", type="daily")

    assert result["path"] == "nuru/daily-2026-09-20.md"
    assert "date: '2026-09-20'\n" in (tmp_path / result["path"]).read_text()


def test_an_empty_daily_can_be_created_but_not_added_to(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    made = server.write_note(type="daily")

    assert made["created"] is True and (tmp_path / made["path"]).read_text().endswith("# Daily 2026-09-20\n\n")
    with pytest.raises(ToolError, match="content is empty - nothing to add"):
        server.write_note("  ", type="daily")


def test_add_to_daily_reports_a_filename_clash_with_a_note_it_could_not_recognise_as_a_daily(tmp_path, monkeypatch):
    (tmp_path / "nuru").mkdir()
    (tmp_path / "nuru" / "daily-2026-09-20.md").write_text("no frontmatter at all")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    with pytest.raises(ToolError, match="FileExistsError"):
        server.write_note("x", type="daily")
    assert (tmp_path / "nuru" / "daily-2026-09-20.md").read_text() == "no frontmatter at all"


def test_add_to_daily_is_callable_through_the_protocol(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _today_is(monkeypatch, "2026-09-20")

    result = asyncio.run(server.mcp.call_tool("write_note", {"type": "daily", "content": "hello"}))

    assert result.is_error is not True
    assert (tmp_path / "nuru" / "daily-2026-09-20.md").exists()


# --- get_links ---


def _vault_with(tmp_path, monkeypatch, notes):
    """Write {relative path: text} into tmp_path and point the server at it."""
    for rel, text in notes.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)


def test_get_links_resolves_outgoing_links_to_paths(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "See [[Alpha]] and [[Beta]].",
        "notes/Alpha.md": "a",
        "notes/Beta.md": "b",
    })

    assert server.get_links("hub")["outgoing"] == [
        {"link": "Alpha", "path": "notes/Alpha.md"},
        {"link": "Beta", "path": "notes/Beta.md"},
    ]


@pytest.mark.parametrize(
    "written",
    ["[[Alpha]]", "[[Alpha|shown text]]", "[[Alpha#Some heading]]", "[[Alpha#^blockid]]", "[[Alpha#H|shown]]", "![[Alpha]]"],
)
def test_get_links_reduces_every_wikilink_form_to_the_note_name(tmp_path, monkeypatch, written):
    _vault_with(tmp_path, monkeypatch, {"hub.md": f"x {written} y", "Alpha.md": "a"})

    assert server.get_links("hub")["outgoing"] == [{"link": "Alpha", "path": "Alpha.md"}]


def test_get_links_lists_a_repeated_link_once_in_order_of_first_appearance(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "[[B]] then [[A]] then [[B]] again and [[A|x]]",
        "A.md": "a",
        "B.md": "b",
    })

    assert [o["link"] for o in server.get_links("hub")["outgoing"]] == ["B", "A"]


def test_get_links_resolves_links_forgivingly_like_read_note(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "[[nuru mcp]] [[Fancy Name]] [[shared/plan]] [[wrong-folder/plan]]",
        "projects/Nuru MCP.md": "a",
        "aliased.md": "---\naliases:\n  - Fancy Name\n---\n",
        "shared/plan.md": "p",
    })

    paths = {o["link"]: o["path"] for o in server.get_links("hub")["outgoing"]}

    assert paths == {
        "nuru mcp": "projects/Nuru MCP.md",
        "Fancy Name": "aliased.md",
        "shared/plan": "shared/plan.md",
        "wrong-folder/plan": "shared/plan.md",
    }


def test_get_links_resolves_a_link_written_as_a_note_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    target = server.create_note(type="topic", title="Target Topic", author="nuru")
    source = server.create_note(type="project", title="Source", author="both", body=f"see [[{target['id']}|Target Topic]]")

    assert server.get_links(source["path"])["outgoing"] == [{"link": target["id"], "path": target["path"]}]


def test_get_links_marks_a_link_to_no_note_with_a_null_path_and_says_why(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"hub.md": "[[Real]] [[Ghost]]", "Real.md": "r"})

    result = server.get_links("hub")

    assert result["outgoing"] == [{"link": "Real", "path": "Real.md"}, {"link": "Ghost", "path": None}]
    assert "null" in result["hint"]


def test_find_broken_links_lists_every_unresolved_link_with_its_note_and_line(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "intro\nSee [[Real]] and [[Ghost]].\n[[Ghost|again]] and [[Other Ghost#h]]\n",
        "sub/b.md": "[[real]] [[Missing Note]]",
        "Real.md": "r",
    })

    result = server.find_broken_links()

    assert result == {"count": 3, "links": [
        {"path": "hub.md", "link": "Ghost", "line": 2},
        {"path": "hub.md", "link": "Other Ghost", "line": 3},
        {"path": "sub/b.md", "link": "Missing Note", "line": 1},
    ]}


def test_find_broken_links_agrees_with_get_links_resolution(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "[[nuru mcp]] [[Fancy Name]] [[wrong-folder/plan]] [[Nope]]",
        "projects/Nuru MCP.md": "a",
        "aliased.md": "---\naliases:\n  - Fancy Name\n---\n",
        "shared/plan.md": "p",
    })

    null_in_get_links = [o["link"] for o in server.get_links("hub")["outgoing"] if o["path"] is None]

    assert [b["link"] for b in server.find_broken_links()["links"]] == null_in_get_links == ["Nope"]


def test_find_broken_links_skips_attachments_that_exist_and_notes_outside_the_working_rules(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "hub.md": "![[diagram.png]] ![[gone.png]]",
        "assets/diagram.png": "png",
        "Templates/t.md": "[[{{placeholder}}]]",
    })

    assert [b["link"] for b in server.find_broken_links()["links"]] == ["gone.png"]


def test_find_broken_links_says_when_none_are_broken_and_when_the_list_is_cut(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"hub.md": "[[Real]]", "Real.md": "r"})
    assert server.find_broken_links()["count"] == 0 and "resolves" in server.find_broken_links()["hint"]

    (tmp_path / "many.md").write_text("\n".join(f"[[Ghost {i}]]" for i in range(30)))
    result = server.find_broken_links()
    assert result["count"] == 30 and len(result["links"]) == 25 and "25 of 30" in result["hint"]


def test_get_links_omits_the_null_hint_when_every_link_resolves(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"hub.md": "[[Real]]", "Real.md": "r"})

    assert "hint" not in server.get_links("hub")


def test_get_links_finds_backlinks_however_the_link_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    target = server.create_note(type="topic", title="Target Topic", author="nuru")
    (tmp_path / "by-name.md").write_text("[[target-topic]]")
    (tmp_path / "by-title.md").write_text("[[Target Topic|the topic]]")
    (tmp_path / "by-id.md").write_text(f"[[{target['id']}]]")
    (tmp_path / "by-heading.md").write_text("[[Target Topic#Facts]]")
    (tmp_path / "unrelated.md").write_text("[[Somewhere Else]]")

    assert server.get_links(target["path"])["backlinks"] == [
        "by-heading.md", "by-id.md", "by-name.md", "by-title.md",
    ]


def test_get_links_backlinks_do_not_include_the_note_itself(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"A.md": "I am [[A]] and see [[#Heading]]\n\n## Heading", "B.md": "[[A]]"})

    result = server.get_links("A")

    assert result["backlinks"] == ["B.md"]
    assert result["outgoing"] == [{"link": "A", "path": "A.md"}]


def test_get_links_lists_a_note_that_links_here_only_once(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"A.md": "a", "B.md": "[[A]] [[A]] [[a]]"})

    assert server.get_links("A")["backlinks"] == ["B.md"]


def test_get_links_backlinks_come_from_archived_notes_but_not_templates_or_hidden_folders(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "A.md": "a",
        "archive/old.md": "[[A]]",
        "Templates/t.md": "[[A]]",
        ".trash/gone.md": "[[A]]",
    })

    assert server.get_links("A")["backlinks"] == ["archive/old.md"]


def test_get_links_resolves_the_note_forgivingly(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"projects/Nuru MCP.md": "[[Other]]", "Other.md": "o"})

    assert server.get_links("nuru mcp")["note"] == "projects/Nuru MCP.md"


def test_get_links_on_an_unknown_note_names_the_closest(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"Nuru MCP.md": "x"})

    with pytest.raises(ToolError, match="FileNotFoundError") as exc_info:
        server.get_links("Nuru MCPP")

    assert "Nuru MCP.md" in str(exc_info.value)


def test_get_links_hints_when_a_note_has_no_links_either_way(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"lonely.md": "no links here", "other.md": "[[nobody]]"})

    result = server.get_links("lonely")

    assert result["outgoing"] == []
    assert result["backlinks"] == []
    assert "no [[links]]" in result["hint"]


def test_get_links_caps_each_list_and_says_so(tmp_path, monkeypatch):
    extra = server._LIST_LIMIT + 3
    notes = {"hub.md": " ".join(f"[[t{i:02d}]]" for i in range(extra))}
    notes.update({f"t{i:02d}.md": "[[hub]]" for i in range(extra)})
    _vault_with(tmp_path, monkeypatch, notes)

    result = server.get_links("hub")

    assert len(result["outgoing"]) == server._LIST_LIMIT
    assert len(result["backlinks"]) == server._LIST_LIMIT
    assert f"{server._LIST_LIMIT} of {extra} outgoing" in result["hint"]
    assert f"{server._LIST_LIMIT} of {extra} backlinks" in result["hint"]


def test_get_links_does_not_reach_a_symlink_pointing_outside_the_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("[[A]]")
    (vault / "A.md").write_text("a")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    assert server.get_links("A")["backlinks"] == []


def test_get_links_survives_a_note_with_broken_frontmatter(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"A.md": "a", "broken.md": "---\naliases: [oops\n---\n[[A]]"})

    assert server.get_links("A")["backlinks"] == ["broken.md"]


# --- archive_note ---


def test_archive_note_moves_a_note_under_archive_keeping_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Old Idea", author="nuru", body="content")

    result = server.rename_or_move_file("Old Idea", "archive")

    assert _no_done(result) == {"from": "nuru/old-idea.md", "to": "archive/nuru/old-idea.md"}
    assert not (tmp_path / "nuru" / "old-idea.md").exists()
    assert "content" in (tmp_path / "archive" / "nuru" / "old-idea.md").read_text()
    assert created["path"] == result["from"]


def test_archive_note_keeps_the_note_reachable_by_id(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Old Idea", author="nuru", body="content")

    server.rename_or_move_file(created["id"], "archive")

    assert server.read_note(created["id"]).endswith("content")
    with pytest.raises(ToolError, match="is archived and can't be written to"):
        server.write_note("still here", note=created["id"])


def test_archive_note_is_reversible_by_moving_the_file_back(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Old Idea", author="nuru", body="content")
    before = (tmp_path / created["path"]).read_text()

    result = server.rename_or_move_file("Old Idea", "archive")
    server.rename_or_move_file(result["to"], result["from"])

    assert (tmp_path / created["path"]).read_text() == before
    assert server.write_note("x", note=created["id"])["path"] == created["path"]


def test_archive_note_accepts_a_forgiving_name(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"projects/Nuru MCP.md": "x"})

    assert _no_done(server.rename_or_move_file("nuru mcp", "archive")) == {
        "from": "projects/Nuru MCP.md",
        "to": "archive/projects/Nuru MCP.md",
    }


def test_archive_note_archives_a_note_made_in_obsidian(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"Plain.md": "typed by hand"})

    server.rename_or_move_file("Plain", "archive")

    assert (tmp_path / "archive" / "Plain.md").read_text() == "typed by hand"


def test_archive_note_keeps_links_to_the_note_resolving(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"projects/Target.md": "t", "hub.md": "[[Target]]"})

    server.rename_or_move_file("Target", "archive")

    assert server.get_links("hub")["outgoing"] == [{"link": "Target", "path": "archive/projects/Target.md"}]


def test_an_archived_note_stays_visible_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    server.create_note(type="topic", title="Old Idea", author="nuru", body="findable words", tags=["zzz"])
    server.create_note(type="topic", title="Keeper", author="nuru")

    server.rename_or_move_file("Old Idea", "archive")

    assert sorted(n["title"] for n in server.browse()["notes"]) == ["Keeper", "Old Idea"]
    assert [m["path"] for m in server.notes_with_tag("zzz")["matches"]] == ["archive/nuru/old-idea.md"]
    assert "zzz" in server.all_tags()["tags"]
    assert "findable words" in server.read_note("Old Idea")
    assert server.search_notes("findable")["count"] == 1
    assert "archive/nuru/old-idea.md" in _notes()


def test_an_archived_note_cannot_be_written_until_it_is_moved_out(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Old Idea", author="nuru", body="content")
    server.rename_or_move_file("Old Idea", "archive")

    with pytest.raises(ToolError, match="rename_or_move_file\\('archive/nuru/old-idea.md', 'nuru/old-idea.md'\\)"):
        server.write_note("more", note="Old Idea")
    assert (tmp_path / "archive/nuru/old-idea.md").read_text().endswith("content")

    assert server.rename_or_move_file("archive/nuru/old-idea.md", "nuru/old-idea.md")["to"] == "nuru/old-idea.md"
    assert _no_done(server.write_note("more", note=created["id"])) == {"path": created["path"], "id": created["id"], "created": False}


# --- trash: rename_or_move_file(x, "trash") is a delete as far as any tool can tell ---


def _trash(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    gone = server.create_note(type="topic", title="Gone", author="nuru", body="findable words [[Keeper]]", tags=["zzz"])
    server.create_note(type="topic", title="Keeper", author="nuru", body="links to [[Gone]]")
    (tmp_path / "nuru" / "pic.png").write_bytes(b"x")
    assert _no_done(server.rename_or_move_file("Gone", "trash")) == {"from": "nuru/gone.md", "to": "trash/nuru/gone.md"}
    server.rename_or_move_file("nuru/pic.png", "trash")
    return gone


def test_a_trashed_note_keeps_its_folder_path_under_trash(tmp_path, monkeypatch):
    _trash(tmp_path, monkeypatch)

    assert (tmp_path / "trash" / "nuru" / "gone.md").is_file()
    assert (tmp_path / "trash" / "nuru" / "pic.png").is_file()


def test_a_trashed_note_is_invisible_to_every_read_tool(tmp_path, monkeypatch):
    gone = _trash(tmp_path, monkeypatch)

    assert [n["title"] for n in server.browse()["notes"]] == ["Keeper"]
    assert _notes() == ["nuru/keeper.md"]
    assert _files() == ["nuru/keeper.md"]
    assert server._folder_listing("trash")["files"] == []
    assert server.search_notes("findable")["count"] == 0
    assert server.notes_with_tag("zzz")["matches"] == []
    assert "zzz" not in server.all_tags()["tags"]
    assert server.get_links("Keeper")["outgoing"] == [{"link": "Gone", "path": None}]
    assert server.find_broken_links()["count"] == 1
    for name in ("Gone", gone["id"], "trash/nuru/gone.md"):
        with pytest.raises(ToolError, match="FileNotFoundError"):
            server.read_note(name)


def test_nothing_comes_back_out_of_trash(tmp_path, monkeypatch):
    _trash(tmp_path, monkeypatch)

    for destination in ("nuru/gone.md", "archive"):
        with pytest.raises(ToolError, match="nothing can be moved out of trash"):
            server.rename_or_move_file("trash/nuru/gone.md", destination)
    with pytest.raises(ToolError, match="FileNotFoundError"):
        server.rename_or_move_file("Gone", "nuru/gone.md")
    assert (tmp_path / "trash" / "nuru" / "gone.md").is_file()


def test_writing_to_a_trashed_notes_title_makes_a_new_note(tmp_path, monkeypatch):
    gone = _trash(tmp_path, monkeypatch)

    result = server.write_note("fresh start", note="Gone", type="topic", author="nuru")

    assert result["created"] is True and result["path"] == "nuru/gone.md" and result["id"] != gone["id"]
    assert (tmp_path / "trash" / "nuru" / "gone.md").read_text().endswith("findable words [[Keeper]]")


def test_an_archived_note_goes_to_trash_without_its_archive_prefix(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"archive/nuru/old.md": "x"})

    assert server.rename_or_move_file("old", "trash")["to"] == "trash/nuru/old.md"


@pytest.mark.parametrize("type_,extra", [("directives", {})])
def test_archive_note_refuses_a_note_under_user(tmp_path, monkeypatch, type_, extra):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type=type_, title="Protected", author="user", **extra)

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file("Protected", "archive")
    assert (tmp_path / created["path"]).exists()
    assert not (tmp_path / "archive").exists()


def test_archive_note_refuses_a_note_in_a_hidden_folder(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {".trash/gone.md": "x"})

    with pytest.raises(ToolError, match="PermissionError"):
        server.rename_or_move_file(".trash/gone.md", "archive")
    assert (tmp_path / ".trash" / "gone.md").exists()


def test_archive_note_refuses_a_note_that_is_already_archived(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"archive/old.md": "x"})

    with pytest.raises(ToolError, match="already archived"):
        server.rename_or_move_file("old", "archive")
    assert (tmp_path / "archive" / "old.md").exists()
    assert not (tmp_path / "archive" / "archive").exists()


def test_archive_note_refuses_a_name_that_matches_several_notes_and_moves_none(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a/notes.md": "1", "b/notes.md": "2"})

    with pytest.raises(ToolError, match="ValueError") as exc_info:
        server.rename_or_move_file("notes", "archive")

    assert "a/notes.md" in str(exc_info.value)
    assert "b/notes.md" in str(exc_info.value)
    assert (tmp_path / "a" / "notes.md").exists()
    assert (tmp_path / "b" / "notes.md").exists()
    assert not (tmp_path / "archive").exists()


def test_archive_note_takes_the_path_when_a_name_is_ambiguous(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"a/notes.md": "1", "b/notes.md": "2"})

    server.rename_or_move_file("b/notes.md", "archive")

    assert (tmp_path / "archive" / "b" / "notes.md").exists()
    assert (tmp_path / "a" / "notes.md").exists()


def test_archive_note_refuses_a_name_that_shares_an_alias_across_notes(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {
        "a.md": "---\naliases:\n  - Twin\n---\n",
        "b.md": "---\naliases:\n  - Twin\n---\n",
    })

    with pytest.raises(ToolError, match="matches 2 notes"):
        server.rename_or_move_file("twin", "archive")


def test_archive_note_on_an_unknown_note_names_the_closest_and_moves_nothing(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"Nuru MCP.md": "x"})

    with pytest.raises(ToolError, match="FileNotFoundError") as exc_info:
        server.rename_or_move_file("Nuru MCPP", "archive")

    assert "Nuru MCP.md" in str(exc_info.value)
    assert (tmp_path / "Nuru MCP.md").exists()


def test_archive_note_refuses_to_overwrite_an_existing_archived_note(tmp_path, monkeypatch):
    _vault_with(tmp_path, monkeypatch, {"x.md": "new", "archive/x.md": "older"})

    with pytest.raises(ToolError, match="FileExistsError"):
        server.rename_or_move_file("x.md", "archive")
    assert (tmp_path / "x.md").read_text() == "new"
    assert (tmp_path / "archive" / "x.md").read_text() == "older"


def test_archive_note_does_not_reach_a_symlink_pointing_outside_the_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError):
        server.rename_or_move_file("leak", "archive")
    assert outside.exists()


def test_a_note_that_answers_to_one_name_several_ways_is_still_a_single_match(tmp_path, monkeypatch):
    """create_note's id is in the index, the frontmatter `id` and `aliases` at once."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    created = server.create_note(type="topic", title="Old Idea", author="nuru")

    assert server._NoteNames().resolve(created["id"]) == [tmp_path / created["path"]]


# --- browse_notes: tie-breaking, and ordering by a note's own date/session ---


def test_browse_notes_orders_notes_made_in_the_same_minute_by_when_they_were_made(tmp_path, monkeypatch):
    """`created` only goes to the minute; the ULID id orders within it."""
    import time

    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = []
    for title in ("Zed", "Mid", "Alpha"):  # deliberately not in path order
        made.append(server.create_note(type="topic", title=title, author="nuru"))
        _backdate(made[-1], "2026-09-20T09:00")
        time.sleep(0.005)

    assert _browse_paths(server.browse()) == [n["path"] for n in reversed(made)]


def test_browse_notes_type_daily_orders_by_the_notes_date_not_by_when_it_was_made(tmp_path, monkeypatch):
    """"Latest daily" is the latest date. A backfilled daily is made today."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    real = server.create_note(type="daily", title="Real", author="user", date="2026-09-20")
    backfilled = server.create_note(type="daily", title="Backfilled", author="user", date="2026-09-10")
    _backdate(real, "2026-09-20T08:00")
    _backdate(backfilled, "2026-09-20T18:00")  # made later, dated earlier

    assert _browse_paths(server.browse(type="daily")) == [real["path"], backfilled["path"]]
    # With no type the mixed list is still newest-made-first.
    assert _browse_paths(server.browse()) == [backfilled["path"], real["path"]]


def test_browse_notes_type_daily_orders_indexed_and_obsidian_dailies_by_date_together(tmp_path, monkeypatch):
    (tmp_path / "typed.md").write_text("---\ntype: daily\ndate: 2026-09-19\n---\n")  # unquoted date
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    older = server.create_note(type="daily", title="Older", author="user", date="2026-09-18")
    newer = server.create_note(type="daily", title="Newer", author="user", date="2026-09-20")
    _touch(tmp_path / "typed.md", datetime(2026, 1, 1, 9, 0))  # edited long ago: mtime would put it last

    assert _browse_paths(server.browse(type="daily")) == [newer["path"], "typed.md", older["path"]]


def test_browse_notes_type_daily_puts_a_daily_with_no_date_after_the_dated_ones(tmp_path, monkeypatch):
    (tmp_path / "undated.md").write_text("---\ntype: daily\n---\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    dated = server.create_note(type="daily", title="Dated", author="user", date="2026-09-01")
    _touch(tmp_path / "undated.md", datetime(2030, 1, 1, 9, 0))

    assert _browse_paths(server.browse(type="daily")) == [dated["path"], "undated.md"]


def test_browse_notes_type_session_orders_by_session_number(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    first = server.create_note(type="session", title="First", author="nuru", session="2026-09-20-01")
    third = server.create_note(type="session", title="Third", author="nuru", session="2026-09-20-03")
    second = server.create_note(type="session", title="Second", author="nuru", session="2026-09-20-02")
    yesterday = server.create_note(type="session", title="Y", author="nuru", session="2026-09-19-09")

    assert _browse_paths(server.browse(type="session")) == [
        third["path"], second["path"], first["path"], yesterday["path"],
    ]


def test_browse_notes_type_without_a_date_or_session_field_keeps_created_order(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    old = server.create_note(type="topic", title="Old", author="nuru")
    new = server.create_note(type="topic", title="New", author="nuru")
    _backdate(old, "2026-09-01T09:00")
    _backdate(new, "2026-09-09T09:00")

    assert _browse_paths(server.browse(type="topic")) == [new["path"], old["path"]]


def test_notes_with_tag_orders_same_minute_notes_by_when_they_were_made(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    made = []
    for title in ("Zed", "Alpha"):
        made.append(server.create_note(type="topic", title=title, author="nuru", tags=["t"]))
        _backdate(made[-1], "2026-09-20T09:00")
        time.sleep(0.005)

    assert [m["path"] for m in server.notes_with_tag("t")["matches"]] == [n["path"] for n in reversed(made)]


# --- find_in_notes ---
#
# Most of these run against a stand-in for the rg binary that prints the --json
# events real ripgrep would (paths absolute, since the search root is passed
# absolute; offsets in bytes), so they run with or without ripgrep installed.
# The last few, below the stand-in ones, run the real thing and are skipped
# where it isn't.


def _rg_match(path, line, text, needle):
    """One `rg --json` match event; `needle` is the part of `text` that matched."""
    start = text.encode().index(needle.encode())
    return {
        "type": "match",
        "data": {
            "path": {"text": str(path)},
            "lines": {"text": text + "\n"},
            "line_number": line,
            "absolute_offset": 0,
            "submatches": [{"match": {"text": needle}, "start": start, "end": start + len(needle.encode())}],
        },
    }


def _fake_rg(monkeypatch, *events, returncode=0, stderr=""):
    """Stand in for the rg binary: it prints `events` (then the closing summary
    real ripgrep ends with) and exits with `returncode`. Returns the list each
    call's (command, kwargs) is appended to."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        stdout = "".join(json.dumps(event) + "\n" for event in [*events, {"type": "summary", "data": {}}])
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(server.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(server.subprocess, "run", run)
    return calls


def test_find_in_notes_returns_each_matching_note_with_its_lines(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("first todo\nnothing here\nAnother TODO\n")
    (tmp_path / "sub" / "b.md").write_text("TODO in sub\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    # rg reports notes in whatever order its threads finish, so sub/b.md can come first.
    _fake_rg(
        monkeypatch,
        _rg_match(tmp_path / "sub" / "b.md", 1, "TODO in sub", "TODO"),
        _rg_match(tmp_path / "a.md", 1, "first todo", "todo"),
        _rg_match(tmp_path / "a.md", 3, "Another TODO", "TODO"),
    )

    assert server.find_in_notes("todo") == {
        "pattern": "todo",
        "count": 2,
        "matches": [
            {"path": "a.md", "lines": [{"line": 1, "text": "first todo"}, {"line": 3, "text": "Another TODO"}]},
            {"path": "sub/b.md", "lines": [{"line": 1, "text": "TODO in sub"}]},
        ],
    }


def test_find_in_notes_returns_no_matches_and_a_retry_hint_when_not_found(tmp_path, monkeypatch):
    (tmp_path / "Welcome.md").write_text("Hello World")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, returncode=1)  # rg exits 1 when nothing matched

    result = server.find_in_notes("nonexistent")

    assert result["matches"] == []
    assert result["count"] == 0
    assert "nonexistent" in result["hint"]
    assert "use search to find" in result["hint"]


def test_find_in_notes_only_reports_notes_the_other_tools_can_see(tmp_path, monkeypatch):
    """The rules for what counts as a note come from _all_notes, not from
    ripgrep flags, so they can't drift from the other tools'. Only the vault's
    top-level Templates/ is placeholder text - Projects/Templates/ is real notes."""
    vault = tmp_path / "vault"
    outside = tmp_path / "outside.md"
    outside.write_text("needle")
    reported = [
        "Real.md",
        "Templates/01 Profile.md",
        ".obsidian/plugin.md",
        "archive/old.md",
        "Projects/Templates/n.md",
    ]
    for rel in reported:
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text("needle")
    (vault / "leak.md").symlink_to(outside)
    monkeypatch.setattr(server, "VAULT_PATH", vault)
    _fake_rg(monkeypatch, *(_rg_match(vault / rel, 1, "needle", "needle") for rel in [*reported, "leak.md"]))

    result = server.find_in_notes("needle")

    assert _search_paths(result) == ["Projects/Templates/n.md", "Real.md", "archive/old.md"]
    assert result["count"] == 3


def test_find_in_notes_caps_notes_and_reports_the_true_count(tmp_path, monkeypatch):
    events = []
    for i in range(12):
        (tmp_path / f"note{i:02}.md").write_text("A widget note")
        events.append(_rg_match(tmp_path / f"note{i:02}.md", 1, "A widget note", "widget"))
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, *events)

    result = server.find_in_notes("widget")

    assert _search_paths(result) == [f"note{i:02}.md" for i in range(10)]
    assert result["count"] == 12
    assert "10 of 12" in result["hint"]


@pytest.mark.parametrize(("matching_lines", "cut_off"), [(5, False), (6, True)])
def test_find_in_notes_shows_five_lines_per_note_and_says_when_there_are_more(
    tmp_path, monkeypatch, matching_lines, cut_off
):
    """rg is asked for one line more than is shown (--max-count 6), which is how
    a note with six matching lines is told from one with exactly five."""
    (tmp_path / "Busy.md").write_text("hit\n" * matching_lines)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(
        monkeypatch, *(_rg_match(tmp_path / "Busy.md", n, "hit", "hit") for n in range(1, matching_lines + 1))
    )

    result = server.find_in_notes("hit")

    command = calls[0][0]
    assert command[command.index("--max-count") + 1] == "6"
    assert [line["line"] for line in result["matches"][0]["lines"]] == [1, 2, 3, 4, 5]
    assert ("hint" in result) is cut_off
    if cut_off:
        assert "first 5" in result["hint"]


def test_find_in_notes_cuts_a_long_line_to_a_window_around_the_match(tmp_path, monkeypatch):
    line = "x" * 300 + " NEEDLE " + "y" * 300
    (tmp_path / "Long.md").write_text(line)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, _rg_match(tmp_path / "Long.md", 1, line, "NEEDLE"))

    text = server.find_in_notes("needle")["matches"][0]["lines"][0]["text"]

    assert "NEEDLE" in text
    assert text.startswith("…") and text.endswith("…")
    assert len(text) <= server._SNIPPET_CHARS + 2


def test_find_in_notes_windows_by_characters_when_ripgreps_offset_is_in_bytes(tmp_path, monkeypatch):
    """200 accented letters put the match 200 bytes further along than its
    character position; cutting at the byte offset would miss it entirely."""
    line = "é" * 200 + " NEEDLE " + "y" * 300
    (tmp_path / "Long.md").write_text(line)
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, _rg_match(tmp_path / "Long.md", 1, line, "NEEDLE"))

    text = server.find_in_notes("needle")["matches"][0]["lines"][0]["text"]

    assert "NEEDLE" in text


def test_find_in_notes_survives_a_line_that_is_not_utf8(tmp_path, monkeypatch):
    """A note saved as Latin-1 arrives from ripgrep as base64 "bytes", not "text"."""
    (tmp_path / "Old.md").write_bytes(b"caf\xe9 TODO\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    event = _rg_match(tmp_path / "Old.md", 1, "cafe TODO", "TODO")
    event["data"]["lines"] = {"bytes": base64.b64encode(b"caf\xe9 TODO\n").decode()}
    event["data"]["submatches"][0].update(start=5, end=9)
    _fake_rg(monkeypatch, event)

    assert server.find_in_notes("todo")["matches"] == [
        {"path": "Old.md", "lines": [{"line": 1, "text": "caf� TODO"}]}
    ]


@pytest.mark.parametrize("pattern", ["", "   "])
def test_find_in_notes_rejects_an_empty_pattern_without_running_ripgrep(tmp_path, monkeypatch, pattern):
    """An empty regex matches every line of every note."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(monkeypatch)

    with pytest.raises(ToolError, match="empty"):
        server.find_in_notes(pattern)

    assert calls == []


def test_find_in_notes_passes_the_pattern_as_a_value_never_as_a_flag(tmp_path, monkeypatch):
    """The pattern comes from a model. Read as an option, ripgrep's --pre would
    run a program of its choosing on every note."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(monkeypatch, returncode=1)

    server.find_in_notes("--pre=evil")

    command = calls[0][0]
    assert command[command.index("-e") + 1] == "--pre=evil"
    assert command.count("--pre=evil") == 1


def test_find_in_notes_runs_rg_without_stdin_and_with_an_explicit_search_root(tmp_path, monkeypatch):
    """Given no path and a piped stdin, rg searches stdin - and in the server
    that is the MCP transport."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(monkeypatch, returncode=1)

    server.find_in_notes("todo")

    command, kwargs = calls[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert command[-1] == str(tmp_path)


def test_find_in_notes_ignores_ignore_files_and_the_users_ripgrep_config(tmp_path, monkeypatch):
    """A .gitignore in a vault kept under git would hide notes from the search,
    and a RIPGREP_CONFIG_PATH file could change the output that is parsed."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(monkeypatch, returncode=1)

    server.find_in_notes("todo")

    assert {"--no-ignore", "--no-config"} <= set(calls[0][0])


def test_find_in_notes_relays_ripgreps_message_for_a_bad_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, returncode=2, stderr="rg: regex parse error:\n    (\n    ^\nerror: unclosed group\n")

    with pytest.raises(ToolError, match="ValueError") as exc_info:
        server.find_in_notes("(")

    assert "unclosed group" in str(exc_info.value)
    assert "escape" in str(exc_info.value)


def test_find_in_notes_literal_searches_plain_text(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = _fake_rg(monkeypatch, returncode=1)

    server.find_in_notes("[[", literal=True)
    server.find_in_notes("todo")

    assert "--fixed-strings" in calls[0][0] and "--fixed-strings" not in calls[1][0]


def test_find_in_notes_searches_an_invalid_regex_as_plain_text_and_says_why(tmp_path, monkeypatch):
    (tmp_path / "Hub.md").write_text("See [[Alpha]]\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    calls = []
    match = json.dumps(_rg_match(tmp_path / "Hub.md", 1, "See [[Alpha]]", "[[")) + "\n"

    def run(command, **kwargs):
        calls.append(command)
        if "--fixed-strings" in command:
            return subprocess.CompletedProcess(command, 0, match, "")
        return subprocess.CompletedProcess(command, 2, "", "rg: regex parse error:\n error: unclosed character class\n")

    monkeypatch.setattr(server.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(server.subprocess, "run", run)

    result = server.find_in_notes("[[")

    assert len(calls) == 2 and _search_paths(result) == ["Hub.md"]
    assert "not a valid regular expression" in result["hint"] and "unclosed character class" in result["hint"]


def test_find_in_notes_says_a_double_backslash_matches_a_backslash(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    _fake_rg(monkeypatch, returncode=1)

    assert "double backslash" in server.find_in_notes(r"\\[\\[.*\\]\\]")["hint"]
    assert "double backslash" not in server.find_in_notes(r"\[\[.*\]\]")["hint"]


def test_find_in_notes_says_how_to_recover_when_ripgrep_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)

    with pytest.raises(ToolError, match="FileNotFoundError") as exc_info:
        server.find_in_notes("todo")

    assert "ripgrep" in str(exc_info.value)
    assert "Use search instead" in str(exc_info.value)


def test_find_in_notes_times_out_instead_of_hanging(tmp_path, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/fake/rg")
    monkeypatch.setattr(server.subprocess, "run", run)

    with pytest.raises(ToolError, match="TimeoutError"):
        server.find_in_notes("todo")


requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed")


@requires_rg
def test_find_in_notes_with_real_ripgrep_returns_paths_and_lines(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("first todo\nnothing here\nAnother TODO: café ☕ done\n")
    (tmp_path / "sub" / "b.md").write_text("TODO in sub\n")
    (tmp_path / "plain.txt").write_text("TODO in a text file\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.find_in_notes("todo") == {
        "pattern": "todo",
        "count": 2,
        "matches": [
            {
                "path": "a.md",
                "lines": [{"line": 1, "text": "first todo"}, {"line": 3, "text": "Another TODO: café ☕ done"}],
            },
            {"path": "sub/b.md", "lines": [{"line": 1, "text": "TODO in sub"}]},
        ],
    }


@requires_rg
def test_find_in_notes_with_real_ripgrep_finds_brackets_literally_or_after_a_bad_regex(tmp_path, monkeypatch):
    (tmp_path / "Hub.md").write_text("See [[Alpha]]\nno links here\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.find_in_notes("[[", literal=True)) == ["Hub.md"]
    fallback = server.find_in_notes("[[")
    assert _search_paths(fallback) == ["Hub.md"] and "plain text" in fallback["hint"]


@requires_rg
def test_find_in_notes_with_real_ripgrep_still_searches_what_an_ignore_file_lists(tmp_path, monkeypatch):
    (tmp_path / ".ignore").write_text("drafts/\n")
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "Idea.md").write_text("a needle\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.find_in_notes("needle")) == ["drafts/Idea.md"]


@requires_rg
def test_find_in_notes_with_real_ripgrep_finds_a_pattern_that_starts_with_a_dash(tmp_path, monkeypatch):
    (tmp_path / "Dash.md").write_text("-foo starts with a dash\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert _search_paths(server.find_in_notes("-foo")) == ["Dash.md"]


@requires_rg
def test_find_in_notes_with_real_ripgrep_finds_an_invalid_regex_as_text_or_once_escaped(tmp_path, monkeypatch):
    (tmp_path / "A.md").write_text("call f(x) here\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    unescaped = server.find_in_notes("f(x")
    assert _search_paths(unescaped) == ["A.md"] and "not a valid regular expression" in unescaped["hint"]
    assert _search_paths(server.find_in_notes(r"f\(x")) == ["A.md"]


@requires_rg
def test_find_in_notes_with_real_ripgrep_survives_a_note_that_is_not_utf8(tmp_path, monkeypatch):
    (tmp_path / "Old.md").write_bytes(b"caf\xe9 TODO\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    line = server.find_in_notes("todo")["matches"][0]["lines"][0]

    assert line["line"] == 1
    assert "TODO" in line["text"]


# --- note_tags ---


def test_note_tags_reads_frontmatter_and_inline_tags(tmp_path, monkeypatch):
    (tmp_path / "Trip.md").write_text("---\ntags:\n  - Travel\n  - project/alpha\n---\n\n# Trip\n\nPacked. #Todo #travel\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.note_tags("Trip") == {"note": "Trip.md", "tags": ["project/alpha", "todo", "travel"]}


def test_note_tags_of_a_missing_note_names_the_closest(tmp_path, monkeypatch):
    (tmp_path / "Trip.md").write_text("x")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    with pytest.raises(ToolError, match="FileNotFoundError.*Trip.md"):
        server.note_tags("Tripp")


# --- note_todos ---


def test_note_todos_finds_unticked_boxes_and_todo_lines_with_line_numbers(tmp_path, monkeypatch):
    (tmp_path / "Plan.md").write_text(
        "---\ntags:\n  - todo\n---\n"  # a 'todo' tag in frontmatter is not a task
        "# Plan\n"
        "- [ ] Book time with Alice\n"
        "- [x] Complain about Mondays\n"
        "* [ ] Pin intermediate CA\n"
        "1. [ ] Numbered task\n"
        "- TODO(clarence): ask Dana about the APN\n"
        "```c\n// TODO: not mine\n- [ ] not a task either\n```\n"
        "Plain line with todo in lowercase.\n"
    )
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    assert server.note_todos("Plan") == {
        "note": "Plan.md",
        "count": 4,
        "todos": [
            {"line": 6, "text": "- [ ] Book time with Alice"},
            {"line": 8, "text": "* [ ] Pin intermediate CA"},
            {"line": 9, "text": "1. [ ] Numbered task"},
            {"line": 10, "text": "- TODO(clarence): ask Dana about the APN"},
        ],
    }


def test_note_todos_hints_when_there_are_none(tmp_path, monkeypatch):
    (tmp_path / "Done.md").write_text("# Done\n\n- [x] All finished\n")
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.note_todos("Done")

    assert (result["count"], result["todos"]) == (0, [])
    assert "no unticked checkboxes" in result["hint"]


def test_note_todos_caps_a_long_list(tmp_path, monkeypatch):
    (tmp_path / "Big.md").write_text("\n".join(f"- [ ] task {n}" for n in range(30)))
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)

    result = server.note_todos("Big")

    assert result["count"] == 30 and len(result["todos"]) == 25
    assert "first 25 of 30" in result["hint"]



# --- corrections are Nuru's too (template 0.2.0) ---


def test_a_correction_lives_in_nuru_and_links_what_it_corrects(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    first = server.create_note(type="project", title="Area Monitor", author="both")
    second = server.create_note(type="topic", title="Vendor email", author="user")

    result = server.create_note(type="correction", title="SARA lead time", author="nuru",
                                targets=[first["id"], second["id"]], body="Lead time is 20 weeks, not 26.")

    assert result["path"] == "nuru/sara-lead-time.md"
    text = (tmp_path / result["path"]).read_text()
    assert f"Corrects: [[{first['id']}]], [[{second['id']}]]\n\nLead time is 20 weeks, not 26." in text
    assert "version: 0.4.0\n" in text and "author: nuru\n" in text
    backlinks = server.get_links(first["id"])["backlinks"]
    assert result["path"] in backlinks


# --- notes the user wrote, outside user/, when the server runs for Nuru ---


@pytest.fixture
def protected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server, "_PROTECT_USER_AUTHORED", True)
    return tmp_path


def test_a_user_authored_note_outside_user_cannot_be_appended_to(protected):
    note = server.create_note(type="topic", title="Vendor email", author="user", body="original")
    with pytest.raises(ToolError, match="written by the user"):
        server.write_note("more", note=note["id"])
    assert (protected / note["path"]).read_text().endswith("original")


@pytest.mark.parametrize("change", [
    lambda path: server.rename_or_move_file(path, "archive"),
    lambda path: server.rename_or_move_file(path, "nuru/elsewhere.md"),
    lambda path: server.rename_or_move_file(path, "trash"),
])
def test_a_user_authored_note_outside_user_cannot_be_moved_archived_or_trashed(protected, change):
    note = server.create_note(type="topic", title="Vendor email", author="user")
    with pytest.raises(ToolError, match="written by the user"):
        change(note["path"])
    assert (protected / note["path"]).is_file()


def test_nuru_and_shared_notes_stay_writable(protected):
    mine = server.create_note(type="topic", title="Mine", author="nuru")
    ours = server.create_note(type="project", title="Ours", author="both")
    server.write_note("more", note=mine["id"])
    server.write_note("more", note=ours["id"])
    assert server.rename_or_move_file(mine["path"], "archive")["to"] == "archive/nuru/mine.md"


def test_nurus_daily_takes_additions_but_nothing_in_user_is_written(protected):
    assert server.write_note("gym at six", type="daily")["path"].startswith("nuru/")
    (protected / "user").mkdir()
    (protected / "user" / "plan.md").write_text("---\nauthor: user\n---\n# Plan\n")
    (protected / "user" / "untyped.md").write_text("# no frontmatter\n")
    for name in ("user/plan.md", "user/untyped.md"):
        with pytest.raises(ToolError, match="the user's own folder, and Nuru never writes there"):
            server.write_note("more", note=name)
    with pytest.raises(ToolError, match="the user's own folder"):
        server.write_note("rule", note="New Directives", type="directives", author="user")
    assert (protected / "user" / "untyped.md").read_text() == "# no frontmatter\n"


def test_without_the_flag_other_clients_can_still_change_user_authored_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(server, "_PROTECT_USER_AUTHORED", False)
    note = server.create_note(type="topic", title="Vendor email", author="user")
    server.write_note("more", note=note["id"])
