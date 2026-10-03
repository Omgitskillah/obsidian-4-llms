"""Semantic (vector) search over the vault: chunking, local embeddings via Ollama, and a
sqlite-vec store.

The lexical tools (search_notes, find_in_notes) match words; this matches meaning, so a
note about an "automobile" answers a question about a "car". Each note is split into
passage-sized chunks, each chunk is embedded with nomic-embed-text v1.5 served by the
local Ollama, and the vectors live in one SQLite file next to the note index. Nothing
leaves the machine.

The index is kept current by refresh(), which only re-embeds notes whose content
changed - Nuru runs it in a background thread while it's open, and `uv run
obsidian-mcp-index` runs it by hand. The server's semantic_search tool only reads it.

Two knobs are worth tuning once a first index exists (both are env vars, see
load_config): OBSIDIAN_EMBED_MODEL and OBSIDIAN_CHUNK_CHARS. The index records which
of each built it and refuses to mix: vectors from different embedders aren't
comparable, and chunks of different sizes would make scores uneven. Change either and
rebuild with `uv run obsidian-mcp-index --rebuild`.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlite_vec
import yaml
from dotenv import load_dotenv

from obsidian_mcp import index as note_index

# The obsidian server's settings (VAULT_PATH, ...) live in the repo root's .env.obsidian.
ENV_FILE = Path(__file__).resolve().parents[4] / ".env.obsidian"

# v1.5, not v2: v1.5 takes a whole passage (Ollama serves it with num_ctx 8192) where
# v2's 512-token limit would force much smaller chunks. The tag is explicit because
# plain "nomic-embed-text" means ":latest", which isn't necessarily v1.5.
DEFAULT_MODEL = "nomic-embed-text:v1.5"
# About 350-400 tokens of English: well inside the embedder's context, and small enough
# that five results fit comfortably in a small model's prompt.
DEFAULT_CHUNK_CHARS = 1500
_MIN_CHUNK_CHARS = 200
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
# A dotfile inside the vault, like the note index, so the listing tools never show it.
DB_NAME = ".obsidian-mcp-semantic.sqlite3"

# nomic-embed-text is trained with these task prefixes; leaving them off noticeably
# worsens retrieval. Part of the model's contract, so not configurable.
_DOCUMENT_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "

# The same notes server._all_notes() works on: no Templates/ (placeholder skeletons), no
# trash/ (deleted, as far as any tool is concerned), no hidden folders, no symlinks out of the
# vault. archive/ is embedded: archived notes stay findable.
# Repeated here rather than imported because importing server reads VAULT_PATH and builds
# the MCP server, which Nuru's indexer (running in Nuru's own process) doesn't want.
_SKIPPED_FOLDERS = ("Templates", "trash")
# server._INDEX_DB_NAME: create_note's id/title index, read here for each note's id.
_NOTE_INDEX_NAME = ".obsidian-mcp-index.sqlite3"

_EMBED_TIMEOUT_SECONDS = 120.0

_FRONTMATTER = re.compile(r"\A---\n(?:.*?\n)?---\n", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")
_CODE_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    note_id TEXT,
    title TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks (path);
"""

Embed = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Config:
    vault: Path
    db_path: Path
    model: str
    chunk_chars: int
    ollama_host: str


def load_config(vault: Path, env: Mapping[str, str]) -> Config:
    """The semantic index's settings, from environment variables.

    OBSIDIAN_EMBED_MODEL (default nomic-embed-text:v1.5), OBSIDIAN_CHUNK_CHARS (default
    1500), OBSIDIAN_SEMANTIC_DB (default <vault>/.obsidian-mcp-semantic.sqlite3) and
    OLLAMA_HOST (default http://localhost:11434, Ollama's own variable).
    """
    raw_chunk_chars = env.get("OBSIDIAN_CHUNK_CHARS") or str(DEFAULT_CHUNK_CHARS)
    try:
        chunk_chars = int(raw_chunk_chars)
    except ValueError as e:
        raise ValueError(f"OBSIDIAN_CHUNK_CHARS must be a whole number, got '{raw_chunk_chars}'.") from e
    if chunk_chars < _MIN_CHUNK_CHARS:
        raise ValueError(f"OBSIDIAN_CHUNK_CHARS must be at least {_MIN_CHUNK_CHARS}, got {chunk_chars}.")
    db = env.get("OBSIDIAN_SEMANTIC_DB")
    host = (env.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
    return Config(
        vault=vault,
        db_path=Path(db).expanduser() if db else vault / DB_NAME,
        model=env.get("OBSIDIAN_EMBED_MODEL") or DEFAULT_MODEL,
        chunk_chars=chunk_chars,
        # OLLAMA_HOST is often written without a scheme ("127.0.0.1:11434"), as the ollama CLI accepts.
        ollama_host=host if "://" in host else f"http://{host}",
    )


def ollama_embedder(config: Config) -> Embed:
    """An Embed that calls the local Ollama's /api/embed with config.model.

    truncate is off: a chunk longer than the model's context fails loudly instead of
    being silently cut, which would index a passage by only its first part.
    """

    def embed(texts: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            f"{config.ollama_host}/api/embed",
            data=json.dumps({"model": config.model, "input": texts, "truncate": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_EMBED_TIMEOUT_SECONDS) as response:
                return json.load(response)["embeddings"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace").strip()
            raise ValueError(
                f"Ollama could not embed with '{config.model}' ({e.code}: {detail}). "
                f"If the model is missing, run `ollama pull {config.model}`."
            ) from e
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Can't reach Ollama at {config.ollama_host} ({e.reason}). Is Ollama running?"
            ) from e

    return embed


def vault_notes(vault: Path) -> list[Path]:
    """Every note the semantic index covers, sorted by path (see _SKIPPED_FOLDERS)."""

    def indexable(path: Path) -> bool:
        parts = path.relative_to(vault).parts
        if parts[0] in _SKIPPED_FOLDERS or any(part.startswith(".") for part in parts):
            return False
        try:
            return path.resolve().is_relative_to(vault)
        except OSError:
            return False

    return sorted(p for p in vault.rglob("*.md") if indexable(p))


def _sections(body: str) -> list[tuple[str | None, list[str]]]:
    """(heading, paragraphs) for each part of a note body, in order.

    A paragraph is a run of lines between blank lines; a fenced code block stays one
    paragraph, and a '#' line inside it is a comment, not a heading. Text before the
    first heading has heading None.
    """
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    lines: list[str] = []
    fence: str | None = None

    def end_paragraph() -> None:
        if paragraph := "\n".join(lines).strip():
            sections[-1][1].append(paragraph)
        lines.clear()

    for line in body.splitlines():
        if opening := _CODE_FENCE.match(line):
            if fence is None:
                fence = opening.group(1)
            elif opening.group(1) == fence:
                fence = None
        elif fence is None and (heading := _HEADING.match(line)):
            end_paragraph()
            sections.append((heading.group(1).strip(), []))
            continue
        elif fence is None and not line.strip():
            end_paragraph()
            continue
        lines.append(line)
    end_paragraph()
    return sections


def _pieces(paragraph: str, limit: int) -> list[str]:
    """A paragraph as pieces of at most `limit` characters: whole if it fits, else split
    between sentences, and a sentence longer than `limit` cut into `limit`-sized slices."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        pieces.extend(sentence[i : i + limit] for i in range(0, len(sentence), limit))
    return _pack(pieces, limit, " ")


def _pack(pieces: list[str], limit: int, joiner: str) -> list[str]:
    """Greedily join consecutive pieces while the result stays within `limit` characters."""
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + len(joiner) + len(piece) <= limit:
            packed[-1] += joiner + piece
        else:
            packed.append(piece)
    return packed


def chunk_note(title: str, text: str, chunk_chars: int) -> list[str]:
    """A note as passage-level chunks, frontmatter left out.

    Each heading starts a new chunk, so a chunk is (part of) one section; within a
    section, paragraphs are packed together up to `chunk_chars`, and a longer paragraph
    is split between sentences. Every chunk starts with a label line - the note's title,
    and its section heading - so it still says what it's about once it's read on its own,
    by the embedder or by the model.
    """
    body = _FRONTMATTER.sub("", text, count=1)
    chunks: list[str] = []
    for heading, paragraphs in _sections(body):
        label = title if heading is None or heading.casefold() == title.casefold() else f"{title} > {heading}"
        pieces = [piece for paragraph in paragraphs for piece in _pieces(paragraph, chunk_chars)]
        chunks.extend(f"{label}\n\n{passage}" for passage in _pack(pieces, chunk_chars, "\n\n"))
    return chunks


def connect(config: Config) -> sqlite3.Connection:
    """Open the semantic index, creating it if it doesn't exist.

    WAL mode, so the server can search while Nuru's indexer writes.
    """
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path, timeout=30)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def reset(conn: sqlite3.Connection) -> None:
    """Empty the index, including which model and chunk size built it - for a rebuild."""
    with conn:
        conn.execute("DROP TABLE IF EXISTS vectors")
        for table in ("chunks", "notes", "meta"):
            conn.execute(f"DELETE FROM {table}")


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def _check_compatible(conn: sqlite3.Connection, config: Config) -> None:
    """Refuse an index built with a different embedding model or chunk size."""
    meta = _meta(conn)
    for key, wanted, env_var in (
        ("model", config.model, "OBSIDIAN_EMBED_MODEL"),
        ("chunk_chars", str(config.chunk_chars), "OBSIDIAN_CHUNK_CHARS"),
    ):
        if key in meta and meta[key] != wanted:
            raise ValueError(
                f"The semantic index was built with {key} '{meta[key]}', but {env_var} is now "
                f"'{wanted}'. Rebuild it with `uv run obsidian-mcp-index --rebuild` (in "
                f"servers/obsidian), or set {env_var} back."
            )


def _read_ids(vault: Path) -> dict[str, dict[str, str]]:
    """create_note's index (id, title, ...) by vault-relative path - {} if there is none yet."""
    db = vault / _NOTE_INDEX_NAME
    if not db.is_file():
        return {}
    with contextlib.closing(note_index.connect(db)) as conn:
        return note_index.all_by_path(conn)


def _frontmatter_id(text: str) -> str | None:
    """The `id` a note's own frontmatter gives, if any (a note made from a template, say)."""
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    try:
        data: Any = yaml.safe_load(match.group(0)[4:-4])
    except (yaml.YAMLError, ValueError):
        return None
    note_id = data.get("id") if isinstance(data, dict) else None
    return str(note_id) if isinstance(note_id, (str, int)) and str(note_id).strip() else None


@dataclass
class _Note:
    path: str
    title: str
    note_id: str | None
    mtime_ns: int
    hash: str
    text: str


@dataclass
class RefreshResult:
    embedded: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _delete_note(conn: sqlite3.Connection, path: str, has_vectors: bool) -> None:
    if has_vectors:
        conn.execute("DELETE FROM vectors WHERE rowid IN (SELECT id FROM chunks WHERE path = ?)", (path,))
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
    conn.execute("DELETE FROM notes WHERE path = ?", (path,))


def _ensure_vectors(conn: sqlite3.Connection, dims: int) -> None:
    """Create the vector table the first time vectors arrive - their size depends on the model."""
    stored = _meta(conn).get("dims")
    if stored is None:
        conn.execute(f"CREATE VIRTUAL TABLE vectors USING vec0(embedding float[{dims}] distance_metric=cosine)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('dims', ?)", (str(dims),))
    elif int(stored) != dims:
        raise ValueError(f"The embedder returned {dims}-dimensional vectors, but the index holds {stored}.")


def refresh(
    conn: sqlite3.Connection,
    config: Config,
    embed: Embed,
    on_progress: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RefreshResult:
    """Bring the index up to date with the vault, embedding only what changed.

    A note whose modified time is unchanged isn't read; one whose time changed but whose
    content hash didn't isn't re-embedded. A note that disappeared from one path and
    appeared, unchanged, at another (a folder reorganised, a note archived and restored)
    keeps its vectors under the new path, unless its title changed with it - the title is
    part of every chunk. Each note's chunks are replaced in one transaction, so a search
    never sees half a note, and an interrupted refresh resumes where it stopped.

    on_progress(n) is called with the number of notes still to embed, ending with 0.
    should_stop() is checked between notes, to end a long refresh early.
    """
    _check_compatible(conn, config)
    with conn:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('model', ?)", (config.model,))
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('chunk_chars', ?)", (str(config.chunk_chars),))
    has_vectors = "dims" in _meta(conn)
    known = {row[0]: row for row in conn.execute("SELECT path, title, mtime_ns, hash FROM notes")}
    ids = _read_ids(config.vault)
    result = RefreshResult()

    changed: list[_Note] = []
    seen: set[str] = set()
    for path in vault_notes(config.vault):
        rel = str(path.relative_to(config.vault))
        seen.add(rel)
        mtime_ns = path.stat().st_mtime_ns
        old = known.get(rel)
        if old is not None and old[2] == mtime_ns:
            continue
        text = path.read_text(errors="ignore")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if old is not None and old[3] == digest:
            with conn:
                conn.execute("UPDATE notes SET mtime_ns = ? WHERE path = ?", (mtime_ns, rel))
            continue
        info = ids.get(rel, {})
        note_id = info.get("id") or _frontmatter_id(text)
        changed.append(_Note(rel, info.get("title") or path.stem, note_id, mtime_ns, digest, text))

    gone = {rel: row for rel, row in known.items() if rel not in seen}
    for note in list(changed):
        if note.path in known:
            continue
        source = next((rel for rel, row in gone.items() if row[3] == note.hash and row[1] == note.title), None)
        if source is None:
            continue
        with conn:
            conn.execute(
                "UPDATE notes SET path = ?, note_id = ?, mtime_ns = ? WHERE path = ?",
                (note.path, note.note_id, note.mtime_ns, source),
            )
            conn.execute("UPDATE chunks SET path = ? WHERE path = ?", (note.path, source))
        del gone[source]
        changed.remove(note)
        result.moved.append((source, note.path))

    with conn:
        for rel in gone:
            _delete_note(conn, rel, has_vectors)
    result.removed = sorted(gone)

    for done, note in enumerate(changed):
        if should_stop is not None and should_stop():
            break
        if on_progress is not None:
            on_progress(len(changed) - done)
        chunks = chunk_note(note.title, note.text, config.chunk_chars)
        vectors = embed([_DOCUMENT_PREFIX + chunk for chunk in chunks]) if chunks else []
        if len(vectors) != len(chunks):
            raise ValueError(f"The embedder returned {len(vectors)} vectors for {len(chunks)} chunks.")
        with conn:
            if vectors:
                _ensure_vectors(conn, len(vectors[0]))
                has_vectors = True
            _delete_note(conn, note.path, has_vectors)
            conn.execute(
                "INSERT INTO notes (path, note_id, title, mtime_ns, hash) VALUES (?, ?, ?, ?, ?)",
                (note.path, note.note_id, note.title, note.mtime_ns, note.hash),
            )
            for chunk, vector in zip(chunks, vectors):
                chunk_id = conn.execute("INSERT INTO chunks (path, text) VALUES (?, ?)", (note.path, chunk)).lastrowid
                conn.execute(
                    "INSERT INTO vectors (rowid, embedding) VALUES (?, ?)",
                    (chunk_id, sqlite_vec.serialize_float32(vector)),
                )
        result.embedded.append(note.path)
    if on_progress is not None:
        on_progress(0)
    return result


def search(conn: sqlite3.Connection, config: Config, embed: Embed, query: str, top_k: int) -> list[dict[str, Any]]:
    """The top_k chunks nearest in meaning to `query`, best first.

    Each is {"path", "id", "text", "score"}: score is cosine similarity, 1 for the same
    direction, rounded to 4 places. [] if nothing has been embedded yet.
    """
    _check_compatible(conn, config)
    if "dims" not in _meta(conn):
        return []
    [vector] = embed([_QUERY_PREFIX + query])
    rows = conn.execute(
        """
        WITH nearest AS (
            SELECT rowid, distance FROM vectors WHERE embedding MATCH ? AND k = ?
        )
        SELECT chunks.path, notes.note_id, chunks.text, nearest.distance
        FROM nearest
        JOIN chunks ON chunks.id = nearest.rowid
        JOIN notes ON notes.path = chunks.path
        ORDER BY nearest.distance
        """,
        (sqlite_vec.serialize_float32(vector), top_k),
    ).fetchall()
    return [
        {"path": path, "id": note_id, "text": text, "score": round(1 - distance, 4)}
        for path, note_id, text, distance in rows
    ]


def main() -> None:
    """`uv run obsidian-mcp-index`: build or update the semantic index by hand."""
    parser = argparse.ArgumentParser(
        prog="obsidian-mcp-index",
        description="Embed the vault's changed notes into the semantic index (all of them, the first time).",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="start from an empty index - needed after changing OBSIDIAN_EMBED_MODEL or OBSIDIAN_CHUNK_CHARS",
    )
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    if not os.environ.get("VAULT_PATH"):
        sys.exit("obsidian-mcp-index: VAULT_PATH is not set (see .env.obsidian.example in the repo root).")
    try:
        config = load_config(Path(os.environ["VAULT_PATH"]).expanduser().resolve(), os.environ)
        with contextlib.closing(connect(config)) as conn:
            if args.rebuild:
                reset(conn)
            result = refresh(
                conn,
                config,
                ollama_embedder(config),
                on_progress=lambda left: print(f"\r{left} notes left to embed ", end="", flush=True),
            )
    except (ValueError, ConnectionError) as e:
        sys.exit(f"\nobsidian-mcp-index: {e}")
    print(
        f"\rEmbedded {len(result.embedded)} notes, moved {len(result.moved)}, removed {len(result.removed)} "
        f"- index at {config.db_path} ({config.model}, {config.chunk_chars} chars per chunk)."
    )
