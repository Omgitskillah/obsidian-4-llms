import asyncio
import contextlib
import io
import json
import os
import re
import sys
import urllib.error

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from obsidian_mcp import index as note_index
from obsidian_mcp import semantic, server

# A stand-in for nomic-embed-text: each dimension is one concept, and a text scores on it for
# every word it contains from that concept's word list. Crude, but it has the property that
# matters - different words for the same thing land close together - without Ollama.
CONCEPTS = [
    {"car", "automobile", "vehicle", "engine", "oil", "tyres", "mechanic"},
    {"garden", "tomato", "tomatoes", "seedlings", "greenhouse", "plants", "watering"},
    {"bread", "sourdough", "loaf", "bake", "dough", "flour", "oven"},
    {"budget", "money", "savings", "spending"},
]


class FakeEmbed:
    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            words = re.findall(r"[a-z]+", text.lower())
            vectors.append([float(sum(w in concept for w in words)) for concept in CONCEPTS] + [0.05])
        return vectors

    @property
    def embedded(self):
        return [text for call in self.calls for text in call]


# No note in here contains the letters "car" - so a keyword search for "car" finds nothing,
# while the note about servicing the automobile is what a person asking about a car wants.
NOTES = {
    "Garage/Vehicle upkeep.md": "Took the automobile to the mechanic. The engine needed fresh oil and new tyres.\n",
    "Garden/Tomatoes.md": "The tomato seedlings went into the greenhouse. The plants need watering every morning.\n",
    "Kitchen/Sourdough.md": (
        "---\nid: 01JKITCHENID\ntype: topic\n---\n\n# Sourdough\n\n"
        "Mix flour and water into a dough, then bake the loaf in a hot oven.\n"
    ),
    "user/Budget.md": "Monthly spending plan: put money into savings first.\n",
    "trash/Old engine.md": "An old automobile note about the engine and its oil.\n",
    "Templates/Topic.md": "---\ntype: topic\n---\n\n# {{title}}\n\nautomobile engine oil\n",
    ".obsidian/hidden.md": "automobile engine oil\n",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # server's import ran load_dotenv(): a developer's own settings mustn't point these tests
    # at their real index or model.
    for name in ("OBSIDIAN_EMBED_MODEL", "OBSIDIAN_CHUNK_CHARS", "OBSIDIAN_SEMANTIC_DB", "OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vault(tmp_path):
    for rel, text in NOTES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


@pytest.fixture
def embed():
    return FakeEmbed()


def refresh(vault, embed, **env):
    config = semantic.load_config(vault, env)
    with contextlib.closing(semantic.connect(config)) as conn:
        return semantic.refresh(conn, config, embed)


def search(vault, embed, query, top_k=5, **env):
    config = semantic.load_config(vault, env)
    with contextlib.closing(semantic.connect(config)) as conn:
        return semantic.search(conn, config, embed, query, top_k)


def touch_later(path):
    """Bump a file's modified time without relying on the clock having moved on."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _run(coro):
    return asyncio.run(coro)


# --- chunk_note ---


def test_chunk_note_drops_frontmatter_and_labels_each_chunk_with_title_and_section():
    text = "---\nid: X\ntags:\n  - secret-tag\n---\n\n# Trip\n\nIntro line.\n\n## Packing\n\nBoots and a map.\n"

    chunks = semantic.chunk_note("Trip", text, 1500)

    assert chunks == ["Trip\n\nIntro line.", "Trip > Packing\n\nBoots and a map."]
    assert not any("secret-tag" in c for c in chunks)


def test_chunk_note_packs_small_paragraphs_together_and_splits_a_long_one_between_sentences():
    small = "\n\n".join(f"Short paragraph {n}." for n in range(3))
    long = " ".join(f"Sentence number {n} is here." for n in range(20))  # ~540 chars

    packed = semantic.chunk_note("Note", small, 200)
    split = semantic.chunk_note("Note", long, 200)

    assert packed == ["Note\n\nShort paragraph 0.\n\nShort paragraph 1.\n\nShort paragraph 2."]
    assert len(split) == 3
    for chunk in split:
        passage = chunk.removeprefix("Note\n\n")
        assert len(passage) <= 200
        assert passage.startswith("Sentence") and passage.endswith(".")
    assert " ".join(c.removeprefix("Note\n\n") for c in split) == long


def test_chunk_note_cuts_a_sentence_longer_than_a_chunk():
    chunks = semantic.chunk_note("N", "x" * 450, 200)

    assert [len(c.removeprefix("N\n\n")) for c in chunks] == [200, 200, 50]


def test_chunk_note_keeps_a_fenced_code_block_whole_and_its_hash_lines_are_not_headings():
    text = "# Setup\n\n```bash\n# install it\n\nuv sync\n```\n"

    assert semantic.chunk_note("Setup", text, 1500) == ["Setup\n\n```bash\n# install it\n\nuv sync\n```"]


def test_chunk_note_skips_headings_with_nothing_under_them():
    assert semantic.chunk_note("Daily", "# Daily\n\n## Tasks\n\n## Notes\n\nCalled the mechanic.\n", 1500) == [
        "Daily > Notes\n\nCalled the mechanic."
    ]


# --- vault_notes ---


def test_vault_notes_skip_templates_trash_hidden_folders_and_symlinks_out_but_keep_user(vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "leak.md"
    outside.write_text("secret")
    (vault / "leak.md").symlink_to(outside)

    paths = [str(p.relative_to(vault)) for p in semantic.vault_notes(vault)]

    assert paths == ["Garage/Vehicle upkeep.md", "Garden/Tomatoes.md", "Kitchen/Sourdough.md", "user/Budget.md"]


def test_vault_notes_follow_whatever_folders_the_vault_has(tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "deep.md").write_text("deep")
    (tmp_path / "top.md").write_text("top")

    assert [str(p.relative_to(tmp_path)) for p in semantic.vault_notes(tmp_path)] == ["a/b/c/deep.md", "top.md"]


# --- load_config ---


def test_load_config_defaults(tmp_path):
    config = semantic.load_config(tmp_path, {})

    assert config.model == "nomic-embed-text:v1.5"
    assert config.chunk_chars == 1500
    assert config.db_path == tmp_path / ".obsidian-mcp-semantic.sqlite3"
    assert config.ollama_host == "http://localhost:11434"


def test_load_config_reads_overrides_from_the_environment(tmp_path):
    config = semantic.load_config(
        tmp_path,
        {
            "OBSIDIAN_EMBED_MODEL": "other-embedder",
            "OBSIDIAN_CHUNK_CHARS": "800",
            "OBSIDIAN_SEMANTIC_DB": str(tmp_path / "elsewhere.sqlite3"),
            "OLLAMA_HOST": "127.0.0.1:9999/",
        },
    )

    assert (config.model, config.chunk_chars) == ("other-embedder", 800)
    assert config.db_path == tmp_path / "elsewhere.sqlite3"
    assert config.ollama_host == "http://127.0.0.1:9999"


@pytest.mark.parametrize("value", ["lots", "50"])
def test_load_config_rejects_a_bad_chunk_size(tmp_path, value):
    with pytest.raises(ValueError, match="OBSIDIAN_CHUNK_CHARS"):
        semantic.load_config(tmp_path, {"OBSIDIAN_CHUNK_CHARS": value})


# --- refresh ---


def test_refresh_embeds_every_indexable_note_as_a_search_document(vault, embed):
    result = refresh(vault, embed)

    assert result.embedded == ["Garage/Vehicle upkeep.md", "Garden/Tomatoes.md", "Kitchen/Sourdough.md", "user/Budget.md"]
    assert all(text.startswith("search_document: ") for text in embed.embedded)
    assert not any("{{title}}" in text or "Old engine" in text for text in embed.embedded)


def test_refresh_again_embeds_nothing_when_nothing_changed(vault, embed):
    refresh(vault, embed)
    embed.calls.clear()

    result = refresh(vault, embed)

    assert (result.embedded, result.moved, result.removed) == ([], [], [])
    assert embed.calls == []


def test_refresh_does_not_re_embed_a_note_that_was_only_touched(vault, embed):
    refresh(vault, embed)
    embed.calls.clear()
    touch_later(vault / "Garden/Tomatoes.md")

    assert refresh(vault, embed).embedded == []
    assert embed.calls == []


def test_refresh_re_embeds_only_the_note_that_changed(vault, embed):
    refresh(vault, embed)
    embed.calls.clear()
    note = vault / "Garden/Tomatoes.md"
    note.write_text("Swapped the greenhouse for raised beds.\n")
    touch_later(note)

    assert refresh(vault, embed).embedded == ["Garden/Tomatoes.md"]
    assert len(embed.calls) == 1
    texts = [m["text"] for m in search(vault, embed, "greenhouse", top_k=10) if m["path"] == "Garden/Tomatoes.md"]
    assert texts == ["Tomatoes\n\nSwapped the greenhouse for raised beds."]


def test_refresh_drops_a_deleted_note(vault, embed):
    refresh(vault, embed)
    (vault / "Garage/Vehicle upkeep.md").unlink()

    assert refresh(vault, embed).removed == ["Garage/Vehicle upkeep.md"]
    assert "Garage/Vehicle upkeep.md" not in {m["path"] for m in search(vault, embed, "car", top_k=10)}


def test_refresh_follows_a_note_to_a_new_folder_without_re_embedding_it(vault, embed):
    refresh(vault, embed)
    embed.calls.clear()
    (vault / "Vehicles").mkdir()
    (vault / "Garage/Vehicle upkeep.md").rename(vault / "Vehicles/Vehicle upkeep.md")

    result = refresh(vault, embed)

    assert result.moved == [("Garage/Vehicle upkeep.md", "Vehicles/Vehicle upkeep.md")]
    assert (result.embedded, result.removed) == ([], [])
    assert embed.calls == []
    assert search(vault, embed, "car", top_k=1)[0]["path"] == "Vehicles/Vehicle upkeep.md"


def test_refresh_re_embeds_a_renamed_note_since_its_title_is_part_of_every_chunk(vault, embed):
    refresh(vault, embed)
    (vault / "Garage/Vehicle upkeep.md").rename(vault / "Garage/Servicing.md")

    result = refresh(vault, embed)

    assert (result.embedded, result.removed, result.moved) == (["Garage/Servicing.md"], ["Garage/Vehicle upkeep.md"], [])
    assert search(vault, embed, "car", top_k=1)[0]["text"].startswith("Servicing\n\n")


def test_refresh_drops_a_note_once_it_is_trashed(vault, embed):
    refresh(vault, embed)
    (vault / "trash/Garden").mkdir(parents=True)
    (vault / "Garden/Tomatoes.md").rename(vault / "trash/Garden/Tomatoes.md")

    assert refresh(vault, embed).removed == ["Garden/Tomatoes.md"]


def test_vault_notes_keep_archived_notes(tmp_path):
    (tmp_path / "archive" / "nuru").mkdir(parents=True)
    (tmp_path / "archive" / "nuru" / "old.md").write_text("old")

    assert [str(p.relative_to(tmp_path)) for p in semantic.vault_notes(tmp_path)] == ["archive/nuru/old.md"]


def test_refresh_reports_how_many_notes_are_left_and_can_be_stopped_between_notes(vault, embed):
    config = semantic.load_config(vault, {})
    progress = []
    with contextlib.closing(semantic.connect(config)) as conn:
        result = semantic.refresh(conn, config, embed, on_progress=progress.append, should_stop=lambda: len(progress) >= 2)

    assert progress == [4, 3, 0]
    assert result.embedded == ["Garage/Vehicle upkeep.md", "Garden/Tomatoes.md"]
    # The next refresh picks up where that one stopped.
    assert refresh(vault, embed).embedded == ["Kitchen/Sourdough.md", "user/Budget.md"]


def test_refresh_refuses_an_index_built_with_another_model_until_it_is_rebuilt(vault, embed):
    refresh(vault, embed)

    with pytest.raises(ValueError, match="built with model 'nomic-embed-text:v1.5'.*--rebuild"):
        refresh(vault, embed, OBSIDIAN_EMBED_MODEL="other-embedder")
    with pytest.raises(ValueError, match="built with model"):
        search(vault, embed, "car", OBSIDIAN_EMBED_MODEL="other-embedder")

    config = semantic.load_config(vault, {"OBSIDIAN_EMBED_MODEL": "other-embedder"})
    with contextlib.closing(semantic.connect(config)) as conn:
        semantic.reset(conn)
        assert len(semantic.refresh(conn, config, embed).embedded) == 4
    assert search(vault, embed, "car", OBSIDIAN_EMBED_MODEL="other-embedder")


def test_refresh_refuses_an_index_built_with_another_chunk_size(vault, embed):
    refresh(vault, embed)

    with pytest.raises(ValueError, match="chunk_chars '1500'.*OBSIDIAN_CHUNK_CHARS"):
        refresh(vault, embed, OBSIDIAN_CHUNK_CHARS="800")


def test_refresh_stores_each_notes_id_from_the_note_index_or_its_frontmatter(vault, embed):
    with contextlib.closing(note_index.connect(vault / ".obsidian-mcp-index.sqlite3")) as conn:
        note_index.insert_note(
            conn, id="01JGARDENID", path="Garden/Tomatoes.md", type="topic", title="Tomatoes",
            author="nuru", created="2026-09-27T10:00", supersedes=None,
        )
    refresh(vault, embed)

    ids = {m["path"]: m["id"] for m in search(vault, embed, "anything", top_k=10)}

    assert ids == {
        "Garden/Tomatoes.md": "01JGARDENID",
        "Kitchen/Sourdough.md": "01JKITCHENID",
        "Garage/Vehicle upkeep.md": None,
        "user/Budget.md": None,
    }


# --- search ---


def test_search_embeds_the_query_as_a_search_query_and_ranks_by_similarity(vault, embed):
    refresh(vault, embed)
    embed.calls.clear()

    matches = search(vault, embed, "what shall I bake", top_k=3)

    assert embed.calls == [["search_query: what shall I bake"]]
    assert len(matches) == 3
    assert matches[0]["path"] == "Kitchen/Sourdough.md"
    assert set(matches[0]) == {"path", "id", "text", "score"}
    assert matches[0]["text"].startswith("Sourdough\n\nMix flour")
    scores = [m["score"] for m in matches]
    assert scores == sorted(scores, reverse=True)
    assert 0.9 < scores[0] <= 1.0


def test_search_of_an_index_with_nothing_embedded_is_empty(tmp_path, embed):
    assert search(tmp_path, embed, "car") == []
    assert embed.calls == []


# --- ollama_embedder ---


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ollama_embedder_posts_to_the_local_ollama_with_truncation_off(tmp_path, monkeypatch):
    sent = {}

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        return FakeResponse(json.dumps({"embeddings": [[0.1, 0.2], [0.3, 0.4]]}).encode())

    monkeypatch.setattr(semantic.urllib.request, "urlopen", fake_urlopen)
    embed = semantic.ollama_embedder(semantic.load_config(tmp_path, {}))

    assert embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert sent["url"] == "http://localhost:11434/api/embed"
    assert sent["body"] == {"model": "nomic-embed-text:v1.5", "input": ["a", "b"], "truncate": False}


def test_ollama_embedder_explains_a_missing_model(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))

    monkeypatch.setattr(semantic.urllib.request, "urlopen", fake_urlopen)
    embed = semantic.ollama_embedder(semantic.load_config(tmp_path, {}))

    with pytest.raises(ValueError, match="model not found.*ollama pull nomic-embed-text:v1.5"):
        embed(["a"])


def test_ollama_embedder_says_when_ollama_is_not_running(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(semantic.urllib.request, "urlopen", fake_urlopen)
    embed = semantic.ollama_embedder(semantic.load_config(tmp_path, {}))

    with pytest.raises(ConnectionError, match="Can't reach Ollama at http://localhost:11434"):
        embed(["a"])


# --- the semantic_search tool ---


@pytest.fixture
def indexed_vault(vault, embed, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", vault)
    monkeypatch.setattr(semantic, "ollama_embedder", lambda config: embed)
    refresh(vault, embed)
    return vault


def test_semantic_search_finds_a_note_by_meaning_where_keyword_search_finds_nothing(indexed_vault):
    assert server.search_notes("car")["count"] == 0

    result = server.semantic_search("car")

    assert result["query"] == "car"
    assert result["matches"][0]["path"] == "Garage/Vehicle upkeep.md"
    assert "automobile" in result["matches"][0]["text"]
    assert "hint" not in result


def test_semantic_search_returns_top_k_and_caps_it(indexed_vault, embed):
    for n in range(12):
        (indexed_vault / f"Garage/Log {n}.md").write_text(f"Entry {n}: the automobile again.\n")
    refresh(indexed_vault, embed)

    assert len(server.semantic_search("car")["matches"]) == 5
    assert len(server.semantic_search("car", top_k=2)["matches"]) == 2
    assert len(server.semantic_search("car", top_k=500)["matches"]) == 10


@pytest.mark.parametrize(("args", "message"), [({"query": "  "}, "query is empty"), ({"query": "car", "top_k": 0}, "top_k")])
def test_semantic_search_rejects_bad_arguments(indexed_vault, args, message):
    with pytest.raises(ToolError, match=f"ValueError: {message}"):
        server.semantic_search(**args)


def test_semantic_search_says_how_to_build_the_index_when_there_is_none(vault, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", vault)

    with pytest.raises(ToolError, match="FileNotFoundError: The semantic index hasn't been built yet.*obsidian-mcp-index"):
        server.semantic_search("car")
    assert not (vault / semantic.DB_NAME).exists()


def test_semantic_search_hints_when_the_index_is_empty(tmp_path, monkeypatch, embed):
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(semantic, "ollama_embedder", lambda config: embed)
    refresh(tmp_path, embed)

    result = server.semantic_search("car")

    assert result["matches"] == []
    assert "Use search to find" in result["hint"]


def test_semantic_search_passes_on_that_ollama_is_not_running(vault, embed, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", vault)
    refresh(vault, embed)

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(semantic.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ToolError, match="ConnectionError: Can't reach Ollama"):
        server.semantic_search("car")


def test_semantic_search_refuses_an_index_from_another_model(indexed_vault, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_EMBED_MODEL", "other-embedder")

    with pytest.raises(ToolError, match="ValueError: The semantic index was built with model"):
        server.semantic_search("car")


def test_search_is_registered_with_only_query_and_replaces_both_searches(indexed_vault):
    tools = {t.name: t for t in _run(server.mcp.list_tools())}

    assert tools["search"].input_schema["required"] == ["query"]
    assert set(tools["search"].input_schema["properties"]) == {"query"}
    assert "search_notes" not in tools and "semantic_search" not in tools

    result = _run(server.mcp.call_tool("search", {"query": "car"}))
    assert "Garage/Vehicle upkeep.md" in str(result)


# --- search: exact matches first, then matches by meaning ---


def test_search_lists_exact_matches_first_then_meaning_without_repeating_a_note(indexed_vault):
    # "engine" is a word in the vehicle note and the trashed one; by meaning, the vehicle note
    # is also the nearest - it must appear once, as an exact match.
    result = server.search("engine")

    assert result["exact_count"] == 1
    assert [(m["path"], m["match"]) for m in result["matches"]][:1] == [("Garage/Vehicle upkeep.md", "exact")]
    paths = [m["path"] for m in result["matches"]]
    assert len(paths) == len(set(paths))
    assert all(m["match"] == "meaning" for m in result["matches"][1:])
    assert "snippet" in result["matches"][0] and "score" in result["matches"][1]


def test_search_finds_by_meaning_when_no_note_has_the_words(indexed_vault):
    result = server.search("car")

    assert result["exact_count"] == 0
    assert result["matches"][0] == {**result["matches"][0], "path": "Garage/Vehicle upkeep.md", "match": "meaning"}
    assert "automobile" in result["matches"][0]["text"]
    assert "all matches are by meaning" in result["hint"]


def test_search_never_returns_trash_or_templates(indexed_vault):
    paths = [m["path"] for m in server.search("automobile engine oil")["matches"]]

    assert not any(p.startswith(("trash/", "Templates/", ".obsidian/")) for p in paths)


def test_search_keeps_its_exact_matches_when_meaning_is_unavailable(vault, monkeypatch):
    monkeypatch.setattr(server, "VAULT_PATH", vault)  # no index built

    result = server.search("engine")

    assert [m["path"] for m in result["matches"]] == ["Garage/Vehicle upkeep.md"]
    assert "Search by meaning is unavailable" in result["hint"] and "obsidian-mcp-index" in result["hint"]


def test_search_cuts_a_long_passage(indexed_vault, monkeypatch):
    monkeypatch.setattr(server, "_MEANING_TEXT_CHARS", 20)

    meaning = [m for m in server.search("car")["matches"] if m["match"] == "meaning"]

    assert all(len(m["text"]) <= 21 for m in meaning) and meaning[0]["text"].endswith("…")


# --- obsidian-mcp-index ---


def test_the_index_command_builds_the_index_and_rebuilds_it_for_a_new_model(vault, embed, monkeypatch, capsys):
    monkeypatch.setattr(semantic, "load_dotenv", lambda path=None: None)
    monkeypatch.setattr(semantic, "ollama_embedder", lambda config: embed)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    monkeypatch.setattr(sys, "argv", ["obsidian-mcp-index"])
    semantic.main()
    assert "Embedded 4 notes" in capsys.readouterr().out

    monkeypatch.setenv("OBSIDIAN_EMBED_MODEL", "other-embedder")
    with pytest.raises(SystemExit, match="--rebuild"):
        semantic.main()

    monkeypatch.setattr(sys, "argv", ["obsidian-mcp-index", "--rebuild"])
    semantic.main()
    assert "Embedded 4 notes" in capsys.readouterr().out


# --- against the real model (skipped unless asked for) ---


@pytest.mark.skipif(
    not os.environ.get("OBSIDIAN_SEMANTIC_LIVE"),
    reason="needs Ollama with nomic-embed-text:v1.5 - set OBSIDIAN_SEMANTIC_LIVE=1 to run",
)
def test_nomic_embed_text_finds_notes_by_meaning(vault):
    config = semantic.load_config(vault, {})
    embed = semantic.ollama_embedder(config)
    with contextlib.closing(semantic.connect(config)) as conn:
        semantic.refresh(conn, config, embed)
        for query, expected in [
            ("how do I look after my car?", "Garage/Vehicle upkeep.md"),
            ("growing vegetables", "Garden/Tomatoes.md"),
            ("what am I baking", "Kitchen/Sourdough.md"),
            ("how much should I save each month", "user/Budget.md"),
        ]:
            assert semantic.search(conn, config, embed, query, 1)[0]["path"] == expected, query
