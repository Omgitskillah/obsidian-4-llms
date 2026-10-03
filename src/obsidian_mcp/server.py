"""MCP server exposing tools to read an Obsidian vault.

Vault location is read from the VAULT_PATH env var (see .env.obsidian / .env.obsidian.example in the repo root).
Run standalone via stdio with: uv run obsidian-mcp
"""

import base64
import concurrent.futures
import contextlib
import difflib
import functools
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar, cast

import yaml
from access_logging import AccessLogMiddleware
from unidecode import unidecode
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from ulid import ULID

from obsidian_mcp import frontmatter_template as template
from obsidian_mcp import index as note_index
from obsidian_mcp import semantic

load_dotenv(semantic.ENV_FILE)

VAULT_PATH = Path(os.environ["VAULT_PATH"]).expanduser().resolve()
TAG_PATTERN = re.compile(r"#([A-Za-z0-9_][A-Za-z0-9_/-]*)")

# id -> path/type/title/author/created/supersedes for every note create_note
# has made. Lives inside the vault as a dotfile so list_files/list_notes
# (which skip dotfiles) never surface it.
_INDEX_DB_NAME = ".obsidian-mcp-index.sqlite3"

# Obsidian's template skeletons live here. Placeholder skeletons, not vault content -
# note-content-scanning tools (list_notes, search_notes, list_tags,
# count_tags) exclude it, or an agent could retrieve "{{title}}" and
# unfilled placeholder text as if it were something a person actually wrote.
_TEMPLATES_FOLDER = "Templates"

# rename_or_move_file(x, "archive") moves a note here, keeping its folder path underneath
# (nuru/x.md -> archive/nuru/x.md): the note's filename never changes, so [[links]] to it
# still resolve, and moving it back is just stripping "archive/". Archived notes stay
# visible - listed, searched, read, embedded - but write_note refuses them: move one out
# first to change it.
_ARCHIVE_FOLDER = "archive"

# rename_or_move_file(x, "trash") moves a note here, the same way. Trash is where a delete
# goes: no tool lists, searches, reads, resolves or embeds anything in it, and nothing can be
# moved out of it again - a person empties or restores it in Obsidian.
_TRASH_FOLDER = "trash"

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")

# search_notes returns at most this many hits, best first - a bounded result
# keeps a broad query from flooding a small model's context window. The other
# tools that return a list of notes (notes_with_tag, get_links, all_tags,
# browse_notes) bound theirs the same way, for the same reason.
_SEARCH_LIMIT = 10
_LIST_LIMIT = 25
_BROWSE_MAX_LIMIT = 50
_SNIPPET_CHARS = 120
# find_in_notes shows this many matching lines of a note (of at most _SEARCH_LIMIT
# notes), each cut to _SNIPPET_CHARS - the same bound, for the same reason.
_FIND_LINES_PER_NOTE = 5

# Inside one query word, a run of these matches any run of '-', '_' or
# whitespace in a note: "PDP-drop" finds "PDP_drop" and "PDP drop", yet "09-17"
# still finds only that adjacent pair, not every note mentioning 09 and 17
# separately - which is how dates get searched.
_WORD_JOINERS = re.compile(r"[-_]+")
_FRONTMATTER = re.compile(r"\A---\n(?:.*?\n)?---\n", re.DOTALL)

_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_SESSION_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})$")

# [[Note]], [[Note|shown as]], [[Note#Heading]], [[Note#^block]] -> "Note".
# Obsidian forbids # ^ [ ] | in a filename, so those end the target. A bare
# [[#Heading]] (a link within the same note) has no target and doesn't match.
_WIKILINK = re.compile(r"\[\[([^\[\]|#^]+?)(?:[#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_CODE_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _validate_calendar_date(value: str, field_name: str) -> None:
    """Check value is 'YYYY-MM-DD' and names a real calendar date."""
    if not _DATE_PATTERN.match(value):
        raise ValueError(f"{field_name} must be 'YYYY-MM-DD', got '{value}'.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"{field_name} '{value}' is not a valid calendar date.") from e


def _validate_session(value: str) -> None:
    """Check value is 'YYYY-MM-DD-NN' with a real calendar date."""
    match = _SESSION_PATTERN.match(value)
    if not match:
        raise ValueError(f"session must be 'YYYY-MM-DD-NN', got '{value}'.")
    _validate_calendar_date(match.group(1), "session")


class _FrontmatterDumper(yaml.SafeDumper):
    """Indents block-list items under their key (`tags:\\n  - x`), which
    PyYAML's default SafeDumper does not do."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _represent_none_as_blank(dumper: yaml.Dumper, _: None) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


yaml.add_representer(type(None), _represent_none_as_blank, Dumper=_FrontmatterDumper)


# Every session's access is logged under here, independent of which client
# connects (Inspector, Claude Desktop, or anything else) - see tools/logging.
LOG_DIR = Path(os.environ.get("OBSIDIAN_MCP_LOG_DIR", Path(__file__).resolve().parents[2] / ".logs"))

mcp = MCPServer("obsidian-mcp", middleware=[AccessLogMiddleware(LOG_DIR)])

# Filesystem-mutating tools run their I/O through this so a stalled write
# surfaces as a clear, fast TimeoutError instead of the caller hanging for
# minutes. Small pool: these are local-disk writes, not a workload that needs
# concurrency.
_IO_TIMEOUT_SECONDS = 10.0
_io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="obsidian-mcp-io")

_T = TypeVar("_T")


def _run_with_timeout(fn: Callable[[], _T]) -> _T:
    """Run fn() with a hard deadline instead of letting it block forever.

    Python has no safe way to forcibly kill a thread stuck in a blocking
    syscall, so on timeout the underlying thread is abandoned rather than
    cancelled - it may still complete (and write to disk) after this raises.
    Callers must use atomic filesystem operations (exclusive create, rename)
    so a late write from an abandoned thread can't silently clobber a
    successful retry.
    """
    future = _io_executor.submit(fn)
    try:
        return future.result(timeout=_IO_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError as e:
        raise TimeoutError(
            f"Vault filesystem operation timed out after {_IO_TIMEOUT_SECONDS:.0f}s. "
            "VAULT_PATH may be on a stalled or unavailable filesystem."
        ) from e


# Every exception type a tool below deliberately raises for an anticipated
# failure (bad input, not found, already exists, permission denied, a timed
# -out write, Ollama not reachable for semantic_search) - never a genuine bug. The MCP framework only forwards a raised
# exception's own message to the client for `ToolError` (or a subclass); any
# other exception is treated as a crash and replaced with a bare
# "Error executing tool <name>", discarding the message and type. @_tool
# converts exactly this set into ToolError, so callers can actually see what
# went wrong; anything NOT in this set (a real bug) still crashes generically,
# which is the right behavior for an unanticipated failure.
_ANTICIPATED_ERRORS = (ValueError, FileNotFoundError, FileExistsError, PermissionError, TimeoutError, ConnectionError)


def _tool(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap a tool body so its own ValueError/FileNotFoundError/FileExistsError/
    PermissionError/TimeoutError/ConnectionError reach the caller with their message and type
    intact, instead of the framework's generic "Error executing tool <name>".
    """

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> _T:
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED_ERRORS as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    return wrapper


def _check_accessible(path: Path) -> None:
    try:
        # rglob() silently skips directories it can't scan (since Python 3.13),
        # so check accessibility explicitly rather than relying on it to raise.
        next(path.iterdir(), None)
    except PermissionError as e:
        raise PermissionError(
            f"Access denied: '{path}'"
        ) from e


@mcp.tool(
    description="List notes newest first - to get oriented, see what's in a folder, or answer \"what's my "
    "latest daily\". folder: optional, a folder's path or name (e.g. 'nuru/People' or 'People'). type: "
    f"optional, one of {', '.join(sorted(template.TYPE_FOLDER))}. limit: how many notes (default 10, max "
    f"{_BROWSE_MAX_LIMIT}). Returns {{folder, count, notes, files, hint}}: notes are {{path, id, type, title}}, "
    "count is the total that match; with a folder, files lists its other files (images, PDFs). Newest = when "
    "write_note made it, or last-modified for notes made in Obsidian; with type daily, transcript or session "
    "it is the latest date / session number instead. Excludes Templates/ and trash/. Pass path to read_note "
    "or write_note."
)
@_tool
def browse(type: str | None = None, limit: int = 10, folder: str | None = None) -> dict[str, Any]:
    """List notes newest first, optionally only one type, only one folder, or both.

    Replaces list_notes, list_files, list_files_in_directory and browse_notes:
    a bounded, ordered slice instead of every path in the vault.

    Args:
        type: Only notes of this type (one of template.TYPE_FOLDER's keys).
            A note create_note made has its type in the index; any other note
            counts as the type its own frontmatter declares, so a note made in
            Obsidian from a Templates/ file is found too. Blank means no filter.
        limit: How many notes to return, at least 1; capped at
            _BROWSE_MAX_LIMIT so this can't turn back into a whole-vault dump.
        folder: Only notes under this folder (recursively) - a vault-relative
            path, or a folder name found anywhere in the vault, as
            _folder_listing resolves it. Its other files (attachments) come
            back too, under "files", at most _LIST_LIMIT.

    Returns {"count", "notes"} (and "folder", "files" with a folder): "notes" holds at most `limit` of
    {"path", "id", "type", "title"}, newest first; "count" is how many notes
    match in total. Ordered by `created` for a note in the index, else by the
    file's modified time - except when `type` is daily, transcript or session
    (the types with a `date` or `session` field), which order by that field,
    so "the latest daily" is the latest date even if an earlier day's daily
    was written more recently. A "hint" explains an empty result, or that results
    were cut off - a bare empty list would reach the model as a blank tool
    result it can't interpret.
    Excludes Templates/ and trash/.
    """
    _check_accessible(VAULT_PATH)
    wanted = (type or "").strip().lower() or None
    if wanted is not None and wanted not in template.TYPE_FOLDER:
        raise ValueError(f"Unknown type '{type}'. Valid types: {', '.join(sorted(template.TYPE_FOLDER))}.")
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}.")
    limit = min(limit, _BROWSE_MAX_LIMIT)

    listing = None
    notes = _all_notes()
    if folder is not None and folder.strip().strip("/"):
        listing = _folder_listing(folder.strip())
        inside = set(listing["files"])
        notes = [p for p in notes if _rel(p) in inside]

    indexed = _indexed_notes()
    newest_first = sorted(notes, key=lambda p: _newest_key(p, indexed), reverse=True)
    rows = (_note_row(path, indexed) for path in newest_first)
    matches = [row for row in rows if wanted is None or row["type"] == wanted]
    field = template.TYPE_FIELD.get(wanted) if wanted else None
    if field in ("date", "session"):
        # "Latest daily" means the latest *date*, not the daily most recently made -
        # a backfilled one is made today. list.sort is stable, so notes with the
        # same date/session (or none) keep the newest-made-first order from above.
        matches.sort(key=lambda row: _sort_field(VAULT_PATH / row["path"], field), reverse=True)

    result: dict[str, Any] = {"count": len(matches), "notes": matches[:limit]}
    if listing is not None:
        attachments = [f for f in listing["files"] if not f.endswith(".md")]
        result = {"folder": listing["directory"], **result, "files": attachments[:_LIST_LIMIT]}
    if listing is not None and "hint" in listing:
        result["hint"] = listing["hint"]
    elif not matches:
        where = f" in '{folder}'" if listing is not None else ""
        result["hint"] = (
            f"No notes of type '{wanted}' found{where}. A note made in Obsidian only counts if its "
            f"frontmatter says 'type: {wanted}'. Call browse without a type, or use search."
            if wanted
            else f"No notes{where}." if listing is not None else "The vault has no notes yet."
        )
    elif len(matches) > limit:
        hint = f"Showing the newest {limit} of {len(matches)} notes."
        if limit < _BROWSE_MAX_LIMIT:
            hint += " Raise limit to see more."
        if wanted is None:
            hint += " Pass type to narrow."
        result["hint"] = hint
    return result


def _is_visible(path: Path) -> bool:
    return not any(part.startswith(".") for part in path.relative_to(VAULT_PATH).parts)


def _is_in_templates(path: Path) -> bool:
    parts = path.relative_to(VAULT_PATH).parts
    return bool(parts) and parts[0] == _TEMPLATES_FOLDER


def _is_within_vault(path: Path) -> bool:
    """Check that path, once symlinks are resolved, is actually inside the vault.

    Guards against a symlink stored inside the vault (e.g. VAULT_PATH/leak.md)
    that points outside of it, which a lexical relative_to() check would miss.
    """
    try:
        return path.resolve().is_relative_to(VAULT_PATH)
    except OSError:
        return False


def _rel(path: Path) -> str:
    return str(path.relative_to(VAULT_PATH))


def _is_archived(path: Path) -> bool:
    parts = path.relative_to(VAULT_PATH).parts
    return bool(parts) and parts[0] == _ARCHIVE_FOLDER


def _is_trashed(path: Path) -> bool:
    parts = path.relative_to(VAULT_PATH).parts
    return bool(parts) and parts[0] == _TRASH_FOLDER


def _all_notes() -> list[Path]:
    """Every real note in the vault, sorted by path.

    Skips Templates/ (placeholder skeletons), trash/, hidden folders (.obsidian,
    Obsidian's .trash) and any symlink that resolves outside the vault - the
    notes the tools below are allowed to read the contents of and name.
    """
    return sorted(
        p
        for p in VAULT_PATH.rglob("*.md")
        if _is_visible(p) and not _is_in_templates(p) and not _is_trashed(p) and _is_within_vault(p)
    )


def _folder_listing(directory: str) -> dict[str, Any]:
    """Every visible file under a folder of the vault, as paths relative to the vault root - browse's folder.

    Args:
        directory: Path to the subdirectory, relative to the vault root. If it
            isn't found directly under the vault root, the vault is searched
            recursively for a directory with that name, and the first match
            (alphabetically, by path) is used.

    Returns {"directory", "count", "files"}. When "files" is empty a "hint"
    says whether no matching directory exists anywhere in the vault or the
    directory exists but holds no visible files - a bare empty list would
    reach the model as a blank tool result it can't interpret.
    """
    _check_accessible(VAULT_PATH)

    target = (VAULT_PATH / directory).resolve()
    if not target.is_relative_to(VAULT_PATH) or not target.is_dir() or _is_trashed(target):
        name = Path(directory).name
        matches = sorted(
            p
            for p in VAULT_PATH.rglob("*")
            if p.is_dir() and p.name == name and not _is_trashed(p) and _is_within_vault(p)
        )
        if not matches:
            return {
                "directory": directory,
                "count": 0,
                "files": [],
                "hint": f"No directory matching '{directory}' exists in the vault. "
                "Call browse without a folder, or search, to find the real paths.",
            }
        target = matches[0]

    _check_accessible(target)

    files = sorted(
        str(path.relative_to(VAULT_PATH))
        for path in target.rglob("*")
        if path.is_file() and _is_visible(path) and not _is_trashed(path) and _is_within_vault(path)
    )
    result: dict[str, Any] = {"directory": directory, "count": len(files), "files": files}
    if not files:
        result["hint"] = f"The directory '{directory}' exists but contains no visible files."
    return result


def _indexed_notes() -> dict[str, dict[str, str]]:
    """id/type/title/created of every note create_note has made, keyed by vault-relative path.

    Empty if the index file doesn't exist yet, rather than opening it: that
    would create it (or fail, on a read-only vault) just to learn nothing is
    indexed.
    """
    if not (VAULT_PATH / _INDEX_DB_NAME).is_file():
        return {}
    with contextlib.closing(_open_index()) as conn:
        return note_index.all_by_path(conn)


def _read_frontmatter(text: str) -> dict[str, Any]:
    """A note's YAML frontmatter as a dict - {} if it has none or it isn't a
    valid YAML mapping. Notes typed in Obsidian can have anything up there
    (a hand-written date PyYAML can't parse raises ValueError, not YAMLError),
    and one bad note must not break a tool that scans the whole vault.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    try:
        data: Any = yaml.safe_load(match.group(0)[4:-4])  # between the two "---\n" fences
    except (yaml.YAMLError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _date_str(value: object) -> str | None:
    """A frontmatter date as 'YYYY-MM-DD'. create_note quotes its dates, so
    they read back as str, but an unquoted `date: 2026-09-20` typed in
    Obsidian reads back as a date object."""
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value.strip() if isinstance(value, str) else None


def _note_tags(text: str) -> set[str]:
    """Every tag on a note, lowercased and without the '#'.

    Both the frontmatter `tags` list - the only place create_note puts tags,
    so the #tag regex alone would find none of the notes this server made -
    and inline #tags in the body. Lowercased because Obsidian treats tags
    case-insensitively.
    """
    raw: Any = _read_frontmatter(text).get("tags")
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    items = cast("list[object]", raw) if isinstance(raw, list) else []
    body = _FRONTMATTER.sub("", text, count=1)
    tags = {t.lstrip("#") for t in items if isinstance(t, str)} | set(TAG_PATTERN.findall(body))
    return {t.lower() for t in tags if t}


def _note_row(path: Path, indexed: dict[str, dict[str, str]], text: str | None = None) -> dict[str, Any]:
    """{path, id, type, title} for a note, in the shape search_notes uses.

    A note create_note made gets all four from the index. Any other note has
    no id (append_note can't target it) and its filename as title; its type
    is whatever its own frontmatter says, which is what lets a note made from
    a Templates/ file in Obsidian show up under browse(type=...). Pass
    `text` if the caller already read the note; it's only read here if needed.
    """
    rel = _rel(path)
    info = indexed.get(rel)
    if info:
        return {"path": rel, "id": info["id"], "type": info["type"], "title": info["title"]}
    note_type = _read_frontmatter(path.read_text(errors="ignore") if text is None else text).get("type")
    return {"path": rel, "id": None, "type": note_type if isinstance(note_type, str) else None, "title": path.stem}


def _recency(path: Path, indexed: dict[str, dict[str, str]]) -> str:
    """'YYYY-MM-DDTHH:MM' to order notes by, newest last.

    create_note's own `created` for a note the index knows; the file's
    modified time for the rest (made or last saved in Obsidian, so it has no
    `created` the server can trust). Both are local time in the same format,
    so they sort together.
    """
    info = indexed.get(_rel(path))
    if info:
        return info["created"]
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M")


def _newest_key(path: Path, indexed: dict[str, dict[str, str]]) -> tuple[str, str, str]:
    """Sort key for newest-first: _recency, then the note's id, then its path.

    `created` only goes to the minute, so notes made in the same minute tie on
    it; a ULID sorts by creation time to the millisecond, which orders them
    the way they were actually made. (A note with no id sorts before one that
    has it, then by path - an arbitrary but stable order.)
    """
    rel = _rel(path)
    return (_recency(path, indexed), indexed.get(rel, {}).get("id", ""), rel)


def _sort_field(path: Path, field: str) -> str:
    """A note's own `date` or `session` frontmatter as a string, "" if it has none."""
    return _date_str(_read_frontmatter(path.read_text(errors="ignore")).get(field)) or ""


def _snippet(text: str, patterns: list[re.Pattern[str]]) -> str:
    """About _SNIPPET_CHARS of a note's body around the earliest query-word hit.

    Frontmatter is skipped (a hit there is a tag or alias, not readable
    context). If no word appears in the body - the note matched on its
    filename or title - this is just the start of the body.
    """
    body = _FRONTMATTER.sub("", text, count=1)
    hits = [m.start() for p in patterns if (m := p.search(body))]
    start = max(0, min(hits) - _SNIPPET_CHARS // 3) if hits else 0
    end = start + _SNIPPET_CHARS
    snippet = " ".join(body[start:end].split())
    return f"{'…' if start else ''}{snippet}{'…' if end < len(body) else ''}"


@_tool
def search_notes(query: str) -> dict[str, Any]:
    """Search notes (Markdown files) by content, title and filename.

    Args:
        query: One or more whitespace-separated words. Every word must
            appear somewhere in the note's content, title or filename, in any
            order, ignoring case. Inside a word, "-" and "_" also match each
            other and a space, and the parts must sit side by side:
            "PDP-drop" finds "PDP_drop" and "PDP drop", and "09-17" finds
            "2026-09-17" but not a note that only mentions 09 and 17
            separately (written "09 17", those are two words and may be
            anywhere).

    Returns {"query", "count", "matches"}. "matches" holds at most
    _SEARCH_LIMIT hits, best first - notes with more query words in their
    filename or title rank above notes that only match in the body, ties
    broken by path - each {"path", "id", "type", "title", "snippet"}. "count"
    is the total number of matching notes, which can exceed len(matches).
    "id", "type" and "title" come from the index, so they're null (title:
    the filename) for a note create_note didn't make; "snippet" is a short
    window of the body around the first hit. A "hint" explains how to retry
    when nothing matches, or how to narrow when results were cut off - a bare
    empty list would reach the model as a blank tool result it can't
    interpret, and small models fill that gap with invented notes.
    Excludes Templates/ - those are placeholder skeletons, not notes.
    """
    _check_accessible(VAULT_PATH)
    patterns: list[re.Pattern[str]] = []
    for word in query.split():
        parts = [re.escape(part) for part in _WORD_JOINERS.split(word) if part]
        if parts:
            patterns.append(re.compile(r"[-_\s]+".join(parts), re.IGNORECASE))
    if not patterns:
        raise ValueError("query is empty - give at least one word to search for.")
    indexed = _indexed_notes()

    hits: list[tuple[int, dict[str, Any]]] = []
    for path in _all_notes():
        rel_path = str(path.relative_to(VAULT_PATH))
        info: dict[str, str] = indexed.get(rel_path, {})
        title = info.get("title", path.stem)
        name = f"{path.stem} {title}"
        text = path.read_text(errors="ignore")
        if not all(p.search(name) or p.search(text) for p in patterns):
            continue
        rank = sum(1 for p in patterns if p.search(name))
        match = {
            "path": rel_path,
            "id": info.get("id"),
            "type": info.get("type"),
            "title": title,
            "snippet": _snippet(text, patterns),
        }
        hits.append((rank, match))

    hits.sort(key=lambda hit: (-hit[0], hit[1]["path"]))
    result: dict[str, Any] = {
        "query": query,
        "count": len(hits),
        "matches": [match for _, match in hits[:_SEARCH_LIMIT]],
    }
    if not hits:
        result["hint"] = (
            f"No note contains all of the words in '{query}'. search_notes matches each word "
            "case-insensitively, in any order: retry with fewer or different words, or a different "
            "format (e.g. '2026-09-17' instead of 'September 17')."
        )
    elif len(hits) > _SEARCH_LIMIT:
        result["hint"] = (
            f"Showing the top {_SEARCH_LIMIT} of {len(hits)} matches - add another word to narrow "
            "the search."
        )
    return result


# A passage found by meaning is cut to this many characters in search's result: up to
# _SEARCH_LIMIT of them reach a small model's context at once, and read_note has the rest.
_MEANING_TEXT_CHARS = 400


@mcp.tool(
    description="Find notes. query: words, a name, a date, or a plain description of what you want. "
    "Returns {query, exact_count, matches, hint}, at most 10 matches: first every note containing all the "
    "words (match: exact, with a snippet), then notes found by meaning (match: meaning, with the passage "
    "and a score; 1 is closest). Meaning matches always appear, even when nothing in the vault is relevant: "
    "check each one really answers the request before using it. Pass path to read_note or write_note. For "
    "an exact phrase, number or code use find_in_notes. Excludes Templates/ and trash/."
)
@_tool
def search(query: str) -> dict[str, Any]:
    """search_notes and semantic_search in one call: exact word matches first, then
    matches by meaning, one entry per note.

    The exact matches keep search_notes' order (query words in the filename or
    title first). The rest of the _SEARCH_LIMIT slots go to semantic_search's
    passages, best score first, skipping any note already listed and keeping only
    the best passage of each note. Every match says how it was found, since a
    meaning match is only ever "nearest", never "contains".

    If the meaning half can't run (no index yet, an index from another model,
    Ollama not running), the exact matches still come back, with a hint saying
    why the rest are missing.

    Returns {"query", "exact_count", "matches", "hint"?}: "exact_count" is how many
    notes contain every word, which can exceed the exact matches shown.
    """
    exact = search_notes(query)  # validates the query and raises ValueError on an empty one
    matches = [{**m, "match": "exact"} for m in exact["matches"]]
    seen = {m["path"] for m in matches}
    unavailable = None
    room = _SEARCH_LIMIT - len(matches)
    if room > 0:
        try:
            # More passages than slots: several can come from one note, or from one already listed.
            passages = semantic_search(query, _SEARCH_LIMIT)["matches"]
        except ToolError as e:
            unavailable = str(e)
            passages = []
        indexed = _indexed_notes()
        for passage in passages:
            if len(matches) >= _SEARCH_LIMIT:
                break
            if passage["path"] in seen:
                continue
            seen.add(passage["path"])
            info = indexed.get(passage["path"], {})
            text = passage["text"]
            matches.append({
                "path": passage["path"],
                "id": passage["id"],
                "type": info.get("type"),
                "title": info.get("title", Path(passage["path"]).stem),
                "match": "meaning",
                "score": passage["score"],
                "text": text if len(text) <= _MEANING_TEXT_CHARS else text[:_MEANING_TEXT_CHARS] + "…",
            })

    result: dict[str, Any] = {"query": query, "exact_count": exact["count"], "matches": matches}
    hints = []
    if not matches:
        hints.append(
            f"Nothing found for '{query}'. Retry with fewer or different words, or a different format "
            "(e.g. '2026-09-17' instead of 'September 17')."
        )
    elif not exact["matches"]:
        hints.append(
            f"No note contains every word of '{query}'; all matches are by meaning. Check each passage "
            "really answers the request - if none does, say nothing was found."
        )
    elif exact["count"] > len(exact["matches"]):
        hints.append(f"Showing the top {len(exact['matches'])} of {exact['count']} exact matches - add a word to narrow.")
    if unavailable:
        hints.append(f"Search by meaning is unavailable, so only exact matches are shown ({unavailable}).")
    if hints:
        result["hint"] = " ".join(hints)
    return result


def _rg_bytes(field: dict[str, str]) -> bytes:
    """A path or line from ripgrep's --json output, as bytes: {"text": ...}, or
    {"bytes": <base64>} when it isn't valid UTF-8 (a note saved as Latin-1, say)."""
    return field["text"].encode() if "text" in field else base64.b64decode(field["bytes"])


def _line_snippet(line: str, hit: int) -> str:
    """A matching line, cut to about _SNIPPET_CHARS around character offset
    `hit` if it is longer - a paragraph is one line in Markdown, and the match
    can be anywhere in it."""
    start = max(0, hit - _SNIPPET_CHARS // 3)
    end = start + _SNIPPET_CHARS
    return f"{'…' if start else ''}{line[start:end].strip()}{'…' if end < len(line) else ''}"


@mcp.tool(
    description="Find the lines in notes that match a regular expression, like grep - for an exact phrase, "
    "number, id or code, where search matches words. pattern: a regex (ripgrep syntax), ignoring "
    "case; escape ( ) [ ] { } . * + ? ^ $ | \\ with ONE backslash to match them literally, or set "
    "literal=true to match pattern as plain text with nothing to escape (e.g. pattern '[[' with "
    "literal=true). A pattern that isn't a valid regex is searched as plain text. Returns "
    "{pattern, count, matches, hint}: up to 10 matches, each {path, lines: [{line, text}]} with up to 5 "
    "matching lines per note; count is the total number of matching notes. Pass path to read_note. "
    "Excludes Templates/."
)
@_tool
def find_in_notes(pattern: str, literal: bool = False) -> dict[str, Any]:
    """Find the lines of the vault's notes that match a regular expression, using ripgrep.

    The line-level counterpart of search_notes: that ranks notes that contain
    every word somewhere, this shows the exact lines a pattern hits - for an
    id, a number, a phrase, a snippet of code.

    Args:
        pattern: A regular expression in ripgrep's syntax, matched against one
            line at a time, ignoring case, frontmatter lines included. A
            special character to be matched literally needs a backslash.
        literal: Match `pattern` as plain text (ripgrep --fixed-strings), so
            nothing needs escaping - small models escape badly.

    Returns {"pattern", "count", "matches"}. "matches" holds at most
    _SEARCH_LIMIT notes, in path order, each {"path", "lines"}; "lines" holds
    at most _FIND_LINES_PER_NOTE of {"line", "text"}: the 1-based line number,
    and the line itself, cut to _SNIPPET_CHARS around the hit if it is longer.
    "count" is the total number of matching notes, which can exceed
    len(matches). A "hint" explains how to retry when nothing matches, or how
    to narrow when results were cut off - a bare empty list would reach the
    model as a blank tool result it can't interpret.

    Only notes _all_notes() allows are reported, so Templates/, hidden folders
    and symlinks out of the vault never show up; archive/ does, as in
    search_notes. Needs the `rg` binary on PATH: without it this raises
    FileNotFoundError saying so. A pattern ripgrep rejects as a regex (an
    unclosed bracket, say) is searched again as plain text, and the hint says
    so with ripgrep's own explanation; only if that fails too does this raise
    ValueError.
    """
    _check_accessible(VAULT_PATH)
    if not pattern.strip():
        raise ValueError("pattern is empty - give a regular expression to search for.")
    rg = shutil.which("rg")
    if rg is None:
        raise FileNotFoundError(
            "ripgrep ('rg') is not installed or not on PATH, and find_in_notes needs it. "
            "Use search instead."
        )

    done = _run_rg(rg, pattern, literal)
    rejected = None  # ripgrep's reason, when the pattern wasn't a valid regex and was searched as plain text
    if done.returncode == 2 and not literal:
        rejected = " ".join(done.stderr.split())
        done = _run_rg(rg, pattern, literal=True)
    # 0: matches, 1: none, 2: an error.
    if done.returncode not in (0, 1):
        raise ValueError(
            f"ripgrep failed: {done.stderr.strip()} If the pattern is meant literally, escape "
            "( ) [ ] { } . * + ? ^ $ | \\ with a backslash."
        )

    lines_by_note: dict[str, list[dict[str, Any]]] = {}
    for event in map(json.loads, done.stdout.splitlines()):
        if event["type"] != "match":
            continue
        data = event["data"]
        raw = _rg_bytes(data["lines"])
        line = raw.decode(errors="replace").rstrip("\r\n")
        # A submatch's offset is in bytes; the window is cut in characters.
        hit = len(raw[: data["submatches"][0]["start"]].decode(errors="replace"))
        rel = _rel(VAULT_PATH / os.fsdecode(_rg_bytes(data["path"])))
        lines_by_note.setdefault(rel, []).append({"line": data["line_number"], "text": _line_snippet(line, hit)})

    searchable = {_rel(path) for path in _all_notes()}
    notes = [(rel, lines) for rel, lines in sorted(lines_by_note.items()) if rel in searchable]
    shown = notes[:_SEARCH_LIMIT]
    result: dict[str, Any] = {
        "pattern": pattern,
        "count": len(notes),
        "matches": [{"path": rel, "lines": lines[:_FIND_LINES_PER_NOTE]} for rel, lines in shown],
    }
    hints: list[str] = []
    if rejected:
        hints.append(f"'{pattern}' is not a valid regular expression ({rejected}), so it was searched for as plain text.")
    if not notes:
        hints.append(
            f"No line in any note matches '{pattern}'. find_in_notes matches a regular expression "
            "against one line at a time, ignoring case: retry with a shorter or looser pattern, or "
            "use search to find notes containing words in any order."
        )
        if "\\\\" in pattern and not literal and not rejected:
            # e.g. \\[\\[ - a literal backslash before each bracket, where \[\[ was meant
            hints.append("The pattern has a double backslash, which matches a backslash character: to match "
                         "[ or ] write \\[ with one backslash, or pass literal=true and write [ as it is.")
    else:
        cut = [f"Showing the first {_SEARCH_LIMIT} of {len(notes)} matching notes."] if len(notes) > _SEARCH_LIMIT else []
        if any(len(lines) > _FIND_LINES_PER_NOTE for _, lines in shown):
            cut.append(f"A note shows only its first {_FIND_LINES_PER_NOTE} matching lines.")
        if cut:
            hints += [*cut, "Make the pattern more specific to narrow it down."]
    if hints:
        result["hint"] = " ".join(hints)
    return result


def _run_rg(rg: str, pattern: str, literal: bool) -> subprocess.CompletedProcess[str]:
    """One ripgrep search of the vault's notes, as --json events; literal is --fixed-strings.

    -e keeps a pattern that starts with '-' from being read as a flag. --no-ignore and
    --no-config: else a .gitignore in the vault would hide notes from the search, and a
    user's ripgreprc could change the output this parses. --max-count is one more than is
    shown, which tells a note with more matching lines than that from one with exactly that
    many. stdin is closed because rg searches it when it thinks it is being piped to - and
    here that is the MCP transport.
    """
    try:
        return subprocess.run(
            [rg, "--json", "--no-config", "--no-ignore", "--ignore-case", "--glob", "*.md",
             *(["--fixed-strings"] if literal else []),
             "--max-count", str(_FIND_LINES_PER_NOTE + 1), "-e", pattern, str(VAULT_PATH)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            # Same deadline as the writers: a stalled vault filesystem stalls a search too.
            timeout=_IO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(
            f"ripgrep did not finish within {_IO_TIMEOUT_SECONDS:.0f}s. "
            "VAULT_PATH may be on a stalled or unavailable filesystem."
        ) from e


@_tool
def semantic_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """The passages of the vault's notes nearest in meaning to `query` (see semantic.py).

    Only reads the semantic index - Nuru keeps it current in the background, and
    `uv run obsidian-mcp-index` builds or updates it by hand - so a note changed in the
    last few moments may still be found by its old text.

    Args:
        query: What to look for, in plain language. Embedded with the same model that
            built the index; an index built with a different model (or chunk size) is
            refused with ValueError, since its vectors can't be compared.
        top_k: How many passages to return, at least 1; capped at _SEARCH_LIMIT to keep
            a small model's context small.

    Returns {"query", "matches"}: "matches" holds at most top_k of {"path", "id", "text",
    "score"}, best first. A "hint" explains an empty result - a bare empty list would
    reach the model as a blank tool result it can't interpret. Raises FileNotFoundError
    if no index has been built yet, and ConnectionError if Ollama isn't running.
    """
    _check_accessible(VAULT_PATH)
    if not query.strip():
        raise ValueError("query is empty - describe what you're looking for.")
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, got {top_k}.")
    top_k = min(top_k, _SEARCH_LIMIT)

    config = semantic.load_config(VAULT_PATH, os.environ)
    if not config.db_path.is_file():
        raise FileNotFoundError(
            "The semantic index hasn't been built yet: Nuru builds it in the background while it runs, or run "
            "`uv run obsidian-mcp-index` in servers/obsidian."
        )
    with contextlib.closing(semantic.connect(config)) as conn:
        matches = semantic.search(conn, config, semantic.ollama_embedder(config), query, top_k)

    result: dict[str, Any] = {"query": query, "matches": matches}
    if not matches:
        result["hint"] = (
            "The semantic index has no notes in it yet - it may still be building. Use search to find "
            "notes by their words meanwhile."
        )
    return result


@mcp.tool(
    description="Tags. No arguments: every tag in use and how many notes carry it, most-used first. tag: the "
    "notes carrying that tag, newest first (with or without '#', any case; a parent tag matches nested ones). "
    "note: that note's tags (a path, title or id, as for read_note). Pass tag or note, not both. Reads "
    "frontmatter tags and inline #tags, lowercased. Excludes Templates/ and trash/."
)
@_tool
def tags(tag: str | None = None, note: str | None = None) -> dict[str, Any]:
    """all_tags, notes_with_tag and note_tags in one tool: which one runs depends on
    which argument is given."""
    tag = tag.strip() if tag else None
    note = note.strip() if note else None
    if tag and note:
        raise ValueError("Pass tag or note, not both: tag lists the notes carrying a tag, note lists a note's tags.")
    if tag:
        return notes_with_tag(tag)
    if note:
        return note_tags(note)
    return all_tags()


@_tool
def all_tags() -> dict[str, Any]:
    """Every tag in use across the vault's notes, with the number of notes carrying each.

    The tool counterpart of the vault://tags and vault://tags/counts
    resources, which a client that only calls tools (the local model's) can't
    reach. Unlike them it also reads frontmatter `tags`, where create_note puts
    every tag - so it lists the tags of notes the server made - and counts
    notes rather than occurrences, matching what notes_with_tag returns for the
    same tag.

    Returns {"count", "tags"}: "tags" maps tag -> number of notes, most-used
    first (ties alphabetical), at most _LIST_LIMIT of them; "count" is the
    total number of distinct tags. A "hint" explains an empty or cut-off result.
    Tags are lowercased, without the '#'. Excludes Templates/ and trash/.
    """
    _check_accessible(VAULT_PATH)
    counts: Counter[str] = Counter()
    for path in _all_notes():
        counts.update(_note_tags(path.read_text(errors="ignore")))

    most_used = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    result: dict[str, Any] = {"count": len(counts), "tags": dict(most_used[:_LIST_LIMIT])}
    if not counts:
        result["hint"] = "No note has any tags."
    elif len(counts) > _LIST_LIMIT:
        result["hint"] = (
            f"Showing the {_LIST_LIMIT} most-used of {len(counts)} tags. "
            "Pass a tag you expect as tags(tag=...) to check it directly."
        )
    return result


@_tool
def notes_with_tag(tag: str) -> dict[str, Any]:
    """List the notes carrying a tag, newest first (same order as browse).

    Args:
        tag: The tag, with or without a leading '#', in any case. A parent tag
            also matches its nested tags, as in Obsidian: "project" finds
            notes tagged "project/alpha" too.

    Returns {"tag", "count", "matches"}: "matches" holds at most
    _SEARCH_LIMIT of {"path", "id", "type", "title"}; "count" is the total. A
    "hint" says how to narrow when results were cut off, and when nothing
    matches suggests the closest tags actually in use (a typo, a plural) -
    a bare empty list would reach the model as a blank tool result it can't
    interpret. Excludes Templates/ and trash/.
    """
    _check_accessible(VAULT_PATH)
    wanted = tag.strip().lstrip("#").lower()
    if not wanted:
        raise ValueError("tag is empty - give a tag such as 'project'. Call tags with no arguments to see the tags in use.")

    indexed = _indexed_notes()
    in_use: set[str] = set()
    hits: list[tuple[tuple[str, str, str], Path, str]] = []
    for path in _all_notes():
        text = path.read_text(errors="ignore")
        tags = _note_tags(text)
        in_use |= tags
        if any(t == wanted or t.startswith(f"{wanted}/") for t in tags):
            hits.append((_newest_key(path, indexed), path, text))

    hits.sort(key=lambda hit: hit[0], reverse=True)
    result: dict[str, Any] = {
        "tag": wanted,
        "count": len(hits),
        "matches": [_note_row(path, indexed, text) for _, path, text in hits[:_SEARCH_LIMIT]],
    }
    if not hits:
        hint = f"No note is tagged '{wanted}'."
        if close := difflib.get_close_matches(wanted, sorted(in_use), n=3, cutoff=0.6):
            hint += f" Did you mean: {', '.join(close)}?"
        result["hint"] = f"{hint} Call tags with no arguments to see every tag in use."
    elif len(hits) > _SEARCH_LIMIT:
        result["hint"] = f"Showing the newest {_SEARCH_LIMIT} of {len(hits)} notes tagged '{wanted}'."
    return result


@mcp.resource("vault://tags", name="tags", description="All unique tags (e.g. 'project' for '#project') used across notes in the vault.")
def list_tags() -> list[str]:
    """List all unique tags (e.g. 'project' for '#project') used across notes in the vault.

    Excludes Templates/ - those are placeholder skeletons, not notes.
    """
    _check_accessible(VAULT_PATH)
    tags: set[str] = set()
    for path in _all_notes():
        tags.update(TAG_PATTERN.findall(path.read_text(errors="ignore")))
    return sorted(tags)


@mcp.resource("vault://tags/counts", name="tag_counts", description="Occurrence counts for each unique tag (e.g. 'project' for '#project') across notes in the vault.")
def count_tags() -> dict[str, int]:
    """Count occurrences of each unique tag (e.g. 'project' for '#project') across notes in the vault.

    Excludes Templates/ - those are placeholder skeletons, not notes.
    """
    _check_accessible(VAULT_PATH)
    counts: Counter[str] = Counter()
    for path in _all_notes():
        counts.update(TAG_PATTERN.findall(path.read_text(errors="ignore")))
    return dict(sorted(counts.items()))


class _NoteNames:
    """Every name a note answers to, so a loosely written one - a title, an id,
    a lowercase slug, a [[wikilink]] target - resolves to the note it means.

    Built once per tool call (one walk of the vault, one frontmatter parse per
    note), so resolving many names, as get_links does, is dict lookups. Names
    are compared as slugs, which makes them case- and punctuation-insensitive.
    Each table maps slug -> notes, in path order, so a tie goes to the first
    path alphabetically, as everywhere else in this module.

    Tried in this order: the note's whole vault-relative path, its filename,
    then its titles/aliases/ids (the index's title and id, and the
    frontmatter's `aliases` and `id` - which is where create_note writes its
    title and id, so a title or an id finds the note).
    """

    def __init__(self) -> None:
        indexed = _indexed_notes()
        self.notes = _all_notes()
        self._by_path: dict[str, list[Path]] = {}
        self._by_stem: dict[str, list[Path]] = {}
        self._by_alias: dict[str, list[Path]] = {}
        for path in self.notes:
            rel = _rel(path)
            info = indexed.get(rel, {})
            frontmatter = _read_frontmatter(path.read_text(errors="ignore"))
            aliases = frontmatter.get("aliases")
            self._add(self._by_path, rel.removesuffix(".md"), path)
            self._add(self._by_stem, path.stem, path)
            for name in (
                info.get("title"),
                info.get("id"),
                frontmatter.get("id"),
                *(aliases if isinstance(aliases, list) else []),
            ):
                self._add(self._by_alias, name, path)

    @staticmethod
    def _add(table: dict[str, list[Path]], name: object, path: Path) -> None:
        # A name with nothing left after slugifying (empty, all emoji) can't be
        # matched on, and would otherwise match every other such name.
        if isinstance(name, str) and (key := _slugify(name)):
            # A note that answers to one name several ways (create_note's id is in
            # the index, the frontmatter `id` and the `aliases`) is still one match.
            paths = table.setdefault(key, [])
            if path not in paths:
                paths.append(path)

    def resolve(self, name: str) -> list[Path]:
        """The notes `name` could mean from the best table that has any - [] if none."""
        stem = name.strip().removesuffix(".md")
        last = Path(stem).name  # a wrong or missing folder in front of the right name still finds it
        for table, key in (
            (self._by_path, _slugify(stem)),
            (self._by_stem, _slugify(last)),
            (self._by_alias, _slugify(last)),
        ):
            if key and key in table:
                return table[key]
        return []

    def closest(self, name: str, n: int = 3) -> list[Path]:
        """Up to n notes whose filename or title looks most like `name`, best first."""
        query = _slugify(Path(name.strip().removesuffix(".md")).name)
        if not query:
            return []
        names = sorted(self._by_stem.keys() | self._by_alias.keys())
        found: list[Path] = []
        # Ask for more keys than notes wanted: one note can own several close keys.
        for key in difflib.get_close_matches(query, names, n=n * 3, cutoff=0.4):
            for path in self._by_stem.get(key, []) + self._by_alias.get(key, []):
                if path not in found:
                    found.append(path)
        return found[:n]


def _matching_notes(name: str) -> list[Path]:
    """Every note `name` could mean, best match first - [] if none.

    Exact first, as read_note always did: `name` as a vault-relative path,
    then a note with that filename anywhere in the vault (sorted by path).
    Failing both, forgiving (see _NoteNames): case, slug, title, alias and id.
    More than one is only returned when a name genuinely matches several -
    the same filename in two folders, two notes sharing an alias. Nothing in
    trash/ ever matches. The forgiving step reads every note, so it only runs
    when the exact ones find nothing.
    """
    filename = name if name.endswith(".md") else f"{name}.md"

    target = (VAULT_PATH / filename).resolve()
    if target.is_relative_to(VAULT_PATH) and target.is_file() and not _is_trashed(target):
        return [target]
    exact = sorted(
        p for p in VAULT_PATH.rglob("*.md") if p.name == filename and not _is_trashed(p) and _is_within_vault(p)
    )
    if exact:
        return exact
    return _NoteNames().resolve(name)


def _resolve_notes(name: str) -> list[Path]:
    """_matching_notes(name), raising FileNotFoundError, naming the closest notes, if there is none."""
    if found := _matching_notes(name):
        return found
    message = f"No note found named '{name}'."
    if close := _NoteNames().closest(name):
        message += f" Closest matches: {', '.join(_rel(p) for p in close)}."
    raise FileNotFoundError(f"{message} Use browse or search to find the right note.")


def _resolve_note(name: str) -> Path:
    """The first of _resolve_notes(name) - for a tool where a tie doesn't matter."""
    return _resolve_notes(name)[0]


def _extract_section(text: str, section: str, rel_path: str) -> str:
    """The part of a note under one heading: the heading line through to just
    before the next heading of the same or a higher level (so its own
    subheadings stay in). `section` matches a heading's text ignoring case,
    extra whitespace and any leading '#'s. A '#' line inside frontmatter or a
    fenced code block is a comment or a shell prompt, not a heading. If the
    same heading appears twice, the first is used.
    """
    lines = text.splitlines()
    frontmatter = _FRONTMATTER.match(text)
    headings: list[tuple[int, int, str]] = []  # (line, level, title)
    fence: str | None = None
    for i in range(frontmatter.group(0).count("\n") if frontmatter else 0, len(lines)):
        if opening := _CODE_FENCE.match(lines[i]):
            if fence is None:
                fence = opening.group(1)
            elif opening.group(1) == fence:
                fence = None
        elif fence is None and (heading := _HEADING.match(lines[i])):
            headings.append((i, len(heading.group(1)), heading.group(2).strip()))

    wanted = " ".join(section.lstrip("#").split()).casefold()
    for n, (start, level, title) in enumerate(headings):
        if " ".join(title.split()).casefold() == wanted:
            end = next((line for line, lvl, _ in headings[n + 1 :] if lvl <= level), len(lines))
            return "\n".join(lines[start:end]).rstrip()

    listed = ", ".join(f"'{title}'" for _, _, title in headings[:_LIST_LIMIT])
    raise ValueError(
        f"No section '{section}' in '{rel_path}'. "
        + (f"Its headings: {listed}." if headings else "It has no headings - read the whole note.")
    )


@mcp.tool(
    description="Return a note's Markdown content. name: a path, filename, title, alias or id - case and "
    "punctuation don't matter ('nuru mcp' finds Nuru-MCP.md); if nothing matches, the error lists the "
    "closest notes. section: optional heading text (e.g. 'Decisions') to return just that part of a long "
    "note, through to the next heading of the same or higher level; an unknown section lists the note's "
    "headings. For a note's tags, links and open tasks without its text, use note_info."
)
@_tool
def read_note(name: str, section: str | None = None) -> str:
    """Return the contents of a note (Markdown file) by name - all of it, or one section.

    The tags, links, backlinks and open tasks are note_info's. Folding them into read_note as a
    `part` argument was tried on 2026-10-03 and measured worse: with the text as the default the
    model asked for them on 2 of 8 prompts, with them as the default it asked for the text every
    time (0 of 8) - against 5 of 8 for note_info as its own tool.

    Args:
        name: Which note. Tried in order: as a vault-relative path, as a
            filename anywhere in the vault (the first match, alphabetically by
            path, if several), then forgivingly - case, slug, title, alias and
            id all match, so "nuru mcp", "Nuru MCP" and "nuru-mcp.md" find
            the same note, and so does its title or its id.
        section: A heading's text, to return only that section (see
            _extract_section). Blank or omitted returns the whole note.

    Raises FileNotFoundError if no matching note is found anywhere in the
    vault - never returns an empty string, which would be indistinguishable
    from a note that's genuinely empty - and the message names the closest
    notes. A hint returned as ordinary text couldn't be told from the note's
    own content, which is why a miss is an error and not a result. Raises
    ValueError, listing the note's headings, if `section` isn't one of them.
    """
    _check_accessible(VAULT_PATH)

    target = _resolve_note(name)

    try:
        text = target.read_text()
    except PermissionError as e:
        raise PermissionError(
            f"Access denied to '{target}'. Check the VAULT_PATH env var and its permissions."
        ) from e
    if section is None or not section.strip():
        return text
    return _extract_section(text, section, _rel(target))


def _link_targets(text: str) -> list[str]:
    """The distinct [[wikilink]] targets in `text`, in order of first appearance.

    Only the note name: the |alias, #heading and #^block parts are dropped, and
    an embed (![[Note]]) counts, as it does in Obsidian's own link graph.
    """
    targets = (m.group(1).strip() for m in _WIKILINK.finditer(text))
    return list(dict.fromkeys(t for t in targets if t))


@mcp.tool(
    description="What surrounds one note, without its text (read_note gives the text). name: a path, title "
    "or id, as for read_note. Returns {note, tags, outgoing, backlinks, todos, todo_count, hint}: tags are its "
    "frontmatter and inline #tags; outgoing is each [[link]] in it as {link, path}, path null when no note "
    "has that name; backlinks are the notes that link to it; todos are its open tasks (unticked checkboxes "
    "and TODO lines) as {line, text}. Each list is capped at 25."
)
@_tool
def note_info(name: str) -> dict[str, Any]:
    """get_links, note_tags and note_todos for one note, in one call.

    The name is resolved once, by get_links; tags and todos are then read from
    that same path, so all three describe the same note even when a forgiving
    name could have matched another. Hints from the parts are joined.
    """
    links = get_links(name)
    path = links["note"]
    todos = note_todos(path)
    result: dict[str, Any] = {
        "note": path,
        "tags": note_tags(path)["tags"],
        "outgoing": links["outgoing"],
        "backlinks": links["backlinks"],
        "todos": todos["todos"],
        "todo_count": todos["count"],
    }
    hints = [part["hint"] for part in (links, todos) if part.get("hint")]
    if hints:
        result["hint"] = " ".join(hints)
    return result


@_tool
def get_links(name: str) -> dict[str, Any]:
    """A note's outgoing [[wikilinks]] resolved to paths, plus its backlinks.

    Args:
        name: The note, resolved as read_note resolves it.

    Returns {"note", "outgoing", "backlinks"}. "note" is the resolved path.
    "outgoing" holds {"link", "path"}: `link` is the target as written and
    `path` the note it names (resolved as read_note resolves a name, so a link
    by filename, title, alias, id or folder/path all work), or null when no
    note has that name - a broken link, or an attachment. "backlinks" holds the
    paths of the other notes that link here, alphabetically. Each list is
    capped at _LIST_LIMIT, and a "hint" says so, or explains an empty or
    partly-unresolved result. A note's link to itself, and a bare [[#Heading]],
    aren't counted. Archived notes take part; Templates/ and hidden folders don't.
    """
    _check_accessible(VAULT_PATH)
    target = _resolve_note(name)
    target_rel = _rel(target)
    names = _NoteNames()

    outgoing: list[dict[str, str | None]] = []
    for link in _link_targets(target.read_text(errors="ignore")):
        found = names.resolve(link)
        outgoing.append({"link": link, "path": _rel(found[0]) if found else None})

    backlinks: list[str] = []
    for path in names.notes:
        rel = _rel(path)
        if rel == target_rel:
            continue
        for link in _link_targets(path.read_text(errors="ignore")):
            found = names.resolve(link)
            if found and _rel(found[0]) == target_rel:
                backlinks.append(rel)
                break

    result: dict[str, Any] = {
        "note": target_rel,
        "outgoing": outgoing[:_LIST_LIMIT],
        "backlinks": backlinks[:_LIST_LIMIT],
    }
    hints: list[str] = []
    if len(outgoing) > _LIST_LIMIT:
        hints.append(f"Showing {_LIST_LIMIT} of {len(outgoing)} outgoing links.")
    if len(backlinks) > _LIST_LIMIT:
        hints.append(f"Showing {_LIST_LIMIT} of {len(backlinks)} backlinks.")
    if any(link["path"] is None for link in result["outgoing"]):
        hints.append("A link with path null matches no note in the vault (a broken link, or an attachment).")
    if not outgoing and not backlinks:
        hints.append("This note has no [[links]] to other notes, and no note links to it.")
    if hints:
        result["hint"] = " ".join(hints)
    return result


@_tool
def find_broken_links() -> dict[str, Any]:
    """Every [[wikilink]] in the vault's notes that resolves to no note.

    The whole-vault form of get_links' null paths: one walk, the same name
    resolution (_NoteNames), so it agrees with get_links. A link to an
    attachment that exists (![[diagram.png]]) isn't broken. A link counts once
    per note, at the line it first appears on. Notes are those _all_notes()
    allows - archive/ included, as in get_links.

    Returns {"count", "links"}: "links" holds at most _LIST_LIMIT of
    {"path", "link", "line"}, by path then line; "count" is the total. A "hint"
    says when none are broken or when the list was cut off.
    """
    _check_accessible(VAULT_PATH)
    names = _NoteNames()
    files = {
        p.name.lower()
        for p in VAULT_PATH.rglob("*")
        if p.is_file() and p.suffix != ".md" and _is_visible(p) and not _is_trashed(p)
    }

    broken: list[dict[str, Any]] = []
    for path in names.notes:
        seen: set[str] = set()
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
            for link in _link_targets(line):
                if link in seen or names.resolve(link) or Path(link).name.lower() in files:
                    continue
                seen.add(link)
                broken.append({"path": _rel(path), "link": link, "line": number})

    result: dict[str, Any] = {"count": len(broken), "links": broken[:_LIST_LIMIT]}
    if not broken:
        result["hint"] = "Every [[wikilink]] in the vault resolves to a note or an attachment."
    elif len(broken) > _LIST_LIMIT:
        result["hint"] = f"Showing {_LIST_LIMIT} of {len(broken)} broken links."
    return result


# An open task: a Markdown checkbox not yet ticked ("- [ ]", "* [ ]", "1. [ ]"), or a TODO
# marker anywhere in the line ("TODO:", "TODO(clarence):"). A ticked "[x]" is done and doesn't count.
_OPEN_CHECKBOX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]")
_TODO_MARKER = re.compile(r"\bTODO\b")


@_tool
def note_tags(name: str) -> dict[str, Any]:
    """Every tag on one note, as _note_tags reads them: the frontmatter `tags` list and inline
    #tags, lowercased and without the '#'. Returns {"note", "tags"}, tags sorted."""
    _check_accessible(VAULT_PATH)
    target = _resolve_note(name)
    return {"note": _rel(target), "tags": sorted(_note_tags(target.read_text(errors="ignore")))}


@_tool
def note_todos(name: str) -> dict[str, Any]:
    """The open tasks in one note: every unticked Markdown checkbox, and every line with a
    TODO marker. Ticked "[x]" items are done and left out.

    Returns {"note", "count", "todos"}: "todos" holds at most _LIST_LIMIT of {"line", "text"}
    (1-based line number, the line stripped), "count" is the total. Frontmatter is skipped (a
    "todo" tag is not a task), and so are fenced code blocks (a TODO comment in a snippet is
    not the user's). A "hint" explains an empty or cut-off result.
    """
    _check_accessible(VAULT_PATH)
    target = _resolve_note(name)
    text = target.read_text(errors="ignore")
    frontmatter = _FRONTMATTER.match(text)
    lines = text.splitlines()
    todos: list[dict[str, Any]] = []
    fence: str | None = None
    for i in range(frontmatter.group(0).count("\n") if frontmatter else 0, len(lines)):
        line = lines[i]
        if opening := _CODE_FENCE.match(line):
            if fence is None:
                fence = opening.group(1)
            elif opening.group(1) == fence:
                fence = None
            continue
        if fence is None and (_OPEN_CHECKBOX.match(line) or _TODO_MARKER.search(line)):
            todos.append({"line": i + 1, "text": line.strip()})

    result: dict[str, Any] = {"note": _rel(target), "count": len(todos), "todos": todos[:_LIST_LIMIT]}
    if not todos:
        result["hint"] = "This note has no unticked checkboxes and no TODO lines."
    elif len(todos) > _LIST_LIMIT:
        result["hint"] = f"Showing the first {_LIST_LIMIT} of {len(todos)} open tasks."
    return result


def _resolve_target(path: str) -> Path:
    """Resolve a vault-relative path, rejecting anything that escapes the vault."""
    target = VAULT_PATH / path
    if not _is_within_vault(target):
        raise ValueError(f"Path '{path}' is outside the vault.")
    return target.resolve()


def _open_index() -> sqlite3.Connection:
    """Open the index, reconciled against the current filesystem.

    Reconciling on every open (rather than only "at startup", which this
    long-running process has no other hook for) keeps it correct regardless
    of when or how the vault changed outside this server's own tools.
    """
    conn = note_index.connect(VAULT_PATH / _INDEX_DB_NAME)
    note_index.reconcile(conn, VAULT_PATH)
    return conn


def _slugify(title: str) -> str:
    """Transliterate to ASCII (unidecode), then keep only [a-z0-9], hyphenated.

    A title that's entirely non-Latin (e.g. all CJK, or all emoji) can
    transliterate to nothing - callers must fall back to something else
    (create_note uses the note's own id) rather than treat that as an empty
    title, which it isn't.
    """
    return _SLUG_INVALID.sub("-", unidecode(title).strip().lower()).strip("-")


def _reject_hidden(target: Path, rel_path: str) -> None:
    """Refuse to touch a dotfile/dot-directory (e.g. .obsidian/, this
    server's own index db), the same boundary list_files/list_notes already
    draw for reads. Structural/destructive tools must enforce this, not just
    hide it from a listing - hiding it is cosmetic, this is the actual
    boundary.
    """
    if not _is_visible(target):
        raise PermissionError(f"'{rel_path}' is inside a hidden/config folder (e.g. .obsidian) and is protected.")


# The user/ folder (directives, daily) is meant to be read-only
# to everything except write_note, which already gates what goes there by type.
# This enforces that for the structural tool, rename_or_move_file; it is a narrow slice of a real permission model, not one
# - there's no notion here of who's calling, so it can't distinguish a
# human-directed action from an autonomous one.
_PROTECTED_FOLDER = "user"


# Notes the user wrote - `author: user` in their frontmatter - outside user/ are the user's alone
# when this server runs for Nuru, which turns this on (OBSIDIAN_PROTECT_USER_AUTHORED=1) for its own
# server process: write_note and rename_or_move_file (so archive and trash too) refuse them, and a
# change becomes a new note instead (a correction can point at the original). Other clients - Claude
# Desktop, the Inspector - leave it off: there a person is choosing what to change. Inside user/ the
# folder rules above apply as before (write_note still appends to today's daily).
_PROTECT_USER_AUTHORED = os.environ.get("OBSIDIAN_PROTECT_USER_AUTHORED") == "1"


def _reject_user_authored(target: Path, rel_path: str) -> None:
    if not _PROTECT_USER_AUTHORED or not target.is_relative_to(VAULT_PATH):
        return
    rel = target.relative_to(VAULT_PATH)
    if rel.parts[:1] == (_PROTECTED_FOLDER,):
        return
    notes = [target] if target.is_file() else sorted(target.rglob("*.md")) if target.is_dir() else []
    for note in notes:
        if note.suffix == ".md" and _read_frontmatter(note.read_text(errors="ignore")).get("author") == "user":
            shown = rel_path if note == target else str(note.relative_to(VAULT_PATH))
            raise PermissionError(
                f"'{shown}' was written by the user (author: user), so Nuru may not change, move or delete it. "
                "Tell the user it can't be changed. Don't make a copy or a new note in its place; only if they "
                "asked to correct what it says, write a correction note that points at it."
            )


def _reject_user_folder_for_nuru(rel_path: str) -> None:
    """With OBSIDIAN_PROTECT_USER_AUTHORED on (Nuru's own server), nothing under user/ is written:
    it is the user's folder. Nuru keeps its own daily notes and reminders in nuru/."""
    if _PROTECT_USER_AUTHORED and Path(rel_path).parts[:1] == (_PROTECTED_FOLDER,):
        raise PermissionError(
            f"'{rel_path}' is in {_PROTECTED_FOLDER}/, the user's own folder, and Nuru never writes there. "
            "Tell the user what you would have added instead."
        )


def _reject_protected_folder(target: Path, rel_path: str) -> None:
    if target.is_relative_to(VAULT_PATH):
        parts = target.relative_to(VAULT_PATH).parts
        if parts and parts[0] == _PROTECTED_FOLDER:
            raise PermissionError(
                f"'{rel_path}' is under '{_PROTECTED_FOLDER}/', the user's own folder: it can't be moved, archived or "
                "deleted. Tell the user."
            )


def _build_frontmatter(
    *,
    note_id: str | None,
    type: str,
    created: str,
    author: str | None,
    supersedes: str | None,
    type_field: tuple[str, object] | None,
    tags: list[str],
    aliases: list[object],
) -> str:
    fields: dict[str, object] = {
        "id": note_id,
        "version": template.VERSION,
        "type": type,
        "created": created,
        "author": author,
        "supersedes": supersedes,
    }
    if type_field is not None:
        fields[type_field[0]] = type_field[1]
    fields["tags"] = tags
    fields["aliases"] = aliases
    return yaml.dump(
        fields, Dumper=_FrontmatterDumper, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def _frontmatters_mentioning(needle: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """(path, frontmatter) of every note whose text contains `needle`. The
    substring test comes first, so only the few likely matches pay for a YAML parse."""
    for path in _all_notes():
        text = path.read_text(errors="ignore")
        if needle in text:
            yield path, _read_frontmatter(text)


def _next_session(type: str) -> str:
    """Today's next unused session id, 'YYYY-MM-DD-NN', for a note of this type.

    One past the highest NN any note of this type already has today - not the
    first gap - so a session made later never gets a lower number than an
    earlier one. Per type: a transcript and the session note for the same
    conversation share an id, so each type's first note of the day is -01.
    """
    today = _today()
    numbers = [0]
    for _, frontmatter in _frontmatters_mentioning(today):
        session = frontmatter.get("session")
        if (
            frontmatter.get("type") == type
            and isinstance(session, str)
            and (match := _SESSION_PATTERN.match(session))
            and match.group(1) == today
        ):
            numbers.append(int(match.group(2)))
    number = max(numbers) + 1
    if number > 99:
        raise ValueError(f"Today already has 99 '{type}' notes - pass session explicitly.")
    return f"{today}-{number:02d}"


def _find_daily(day: str) -> Path | None:
    """The daily note for `day` ('YYYY-MM-DD') in the daily folder (template.TYPE_FOLDER).

    Found by its frontmatter (type daily, that date), not by guessing a
    filename: a daily made in Obsidian, or by create_note under a title like
    "Daily Note", can be named anything. Only the daily folder counts: the
    user's own dailies in user/ are theirs, and Nuru never writes there.
    """
    folder = (template.TYPE_FOLDER["daily"],)
    for path, frontmatter in _frontmatters_mentioning(day):
        if (
            Path(_rel(path)).parts[:1] == folder
            and frontmatter.get("type") == "daily"
            and _date_str(frontmatter.get("date")) == day
        ):
            return path
    return None


@_tool
def create_note(
    type: str,
    title: str,
    author: str,
    body: str = "",
    tags: list[str] | None = None,
    supersedes: str | None = None,
    date: str | None = None,
    targets: list[str] | None = None,
    session: str | None = None,
    folder: str | None = None,
) -> dict[str, str]:
    """Create a new note, with server-owned frontmatter, in the vault - write_note's
    create path, not a tool of its own.

    Notes are append-only after creation: frontmatter is written exactly
    once, here, and never rewritten - write_note adds to the body.

    Args:
        type: One of: directives, daily, correction, profile, preferences,
            person, topic, index, transcript, project, session. Determines
            the note's folder.
        title: Human-readable title. Slugified to become the filename, and
            used verbatim as the first alias.
        author: One of: user, nuru, both. Types under user/ (directives,
            daily) require author="user".
        body: Markdown content, placed under the title heading.
        tags: Extra tags, beyond the type itself (which is always a tag).
            No tag may contain whitespace.
        supersedes: id of the note this one replaces, if any. Must already
            exist in the index.
        date: Only valid for type="daily": "YYYY-MM-DD", a real calendar
            date. Defaults to today (see _today) if omitted.
        targets: Required, and only valid, for type="correction". Every id
            in the list must already exist in the index.
        session: Only valid for type="transcript" or type="session":
            "YYYY-MM-DD-NN", with a real calendar date. Defaults to today's
            next unused number for that type (see _next_session) if omitted.

    Returns {"id": <ulid>, "path": <vault-relative path>}.
    """
    _check_accessible(VAULT_PATH)

    if type not in template.TYPE_FOLDER:
        raise ValueError(f"Unknown type '{type}'. Valid types: {', '.join(sorted(template.TYPE_FOLDER))}.")
    if author not in template.VALID_AUTHORS:
        raise ValueError(f"Unknown author '{author}'. Valid authors: {', '.join(sorted(template.VALID_AUTHORS))}.")
    if template.TYPE_FOLDER[type] == _PROTECTED_FOLDER and author != "user":
        raise ValueError(
            f"type '{type}' is under '{_PROTECTED_FOLDER}/', which only author=\"user\" may create."
        )

    type_fields = {
        name: value
        for name, value in (("date", date), ("targets", targets), ("session", session))
        if value is not None
    }
    required_field = template.TYPE_FIELD.get(type)
    allowed_fields = {required_field} if required_field else set()
    # date and session, unlike targets, are values the server can supply: left
    # out entirely, they default rather than have the caller state (or get
    # wrong) today's date or the next free session number. Only when *no* extra
    # field was passed, so a wrong one (session on a daily) still fails below
    # with an accurate "got [...]", not one polluted by a defaulted value.
    if not type_fields:
        if required_field == "date":
            type_fields["date"] = date = _today()
        elif required_field == "session":
            type_fields["session"] = session = _next_session(type)
    if set(type_fields) != allowed_fields:
        expected = f"'{required_field}'" if required_field else "none"
        raise ValueError(f"type '{type}' expects exactly these extra fields: {expected}; got {sorted(type_fields)}.")
    if date is not None:
        _validate_calendar_date(date, "date")
    if session is not None:
        _validate_session(session)

    if tags:
        invalid_tags = [t for t in tags if re.search(r"\s", t)]
        if invalid_tags:
            raise ValueError(f"tags must not contain whitespace: {invalid_tags}.")

    if not title.strip():
        raise ValueError("title is empty.")

    note_id = str(ULID())
    # A title that's entirely non-Latin (all CJK, all emoji, ...) can
    # transliterate to nothing - fall back to the id rather than reject a
    # non-empty title just because ASCII slugification couldn't represent it.
    slug = _slugify(title) or note_id.lower()

    with contextlib.closing(_open_index()) as conn:
        if supersedes is not None and not note_index.note_exists(conn, supersedes):
            raise ValueError(f"supersedes references unknown note id '{supersedes}'.")
        if targets is not None:
            unknown_targets = [t for t in targets if not note_index.note_exists(conn, t) and not _matching_notes(t)]
            if unknown_targets:
                raise ValueError(f"targets references unknown note id(s): {unknown_targets}.")

    folder = (folder or "").strip().strip("/") or template.TYPE_FOLDER[type]
    if folder != template.TYPE_FOLDER[type]:
        parts = Path(folder).parts
        if parts[:1] not in [(root,) for root in template.NEW_NOTE_ROOTS] or ".." in parts or any(
            part.startswith(".") for part in parts
        ):
            raise ValueError(
                f"A new note can't go in '{folder}/': new notes go in a folder under "
                f"{' or '.join(r + '/' for r in template.NEW_NOTE_ROOTS)}."
            )
    rel_path = f"{folder}/{slug}.md"
    target = _resolve_target(rel_path)

    created = datetime.now().strftime("%Y-%m-%dT%H:%M")
    type_field = (required_field, type_fields[required_field]) if required_field else None
    frontmatter = _build_frontmatter(
        note_id=note_id,
        type=type,
        created=created,
        author=author,
        supersedes=supersedes,
        type_field=type_field,
        tags=[type, *(tags or [])],
        aliases=[title, note_id],
    )
    if type == "correction":
        # A correction links the notes it corrects, so each one's backlinks lead here. By id: the
        # id is in every note's aliases, and it survives the note being renamed or archived.
        body = f"Corrects: {', '.join(f'[[{t}]]' for t in targets)}\n\n{body}"
    file_content = f"---\n{frontmatter}---\n\n# {title}\n\n{body}"

    def _create() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # "x" mode opens with O_EXCL: the existence check and the create
            # are one atomic filesystem operation, so two callers racing on
            # the same path (e.g. a client retrying after a timeout while an
            # earlier, abandoned attempt is still landing) can't both succeed.
            with target.open("x") as f:
                f.write(file_content)
        except FileExistsError as e:
            raise FileExistsError(f"A note already exists at '{rel_path}'.") from e

        try:
            with contextlib.closing(_open_index()) as conn:
                note_index.insert_note(
                    conn,
                    id=note_id,
                    path=rel_path,
                    type=type,
                    title=title,
                    author=author,
                    created=created,
                    supersedes=supersedes,
                )
        except sqlite3.IntegrityError as e:
            # A UNIQUE violation here means the index already has a row at
            # this path or id despite reconciliation and the atomic create
            # above - e.g. two callers racing on the same title. Give a
            # message instead of letting a bare sqlite3 error reach the
            # caller as the framework's generic, contentless crash message.
            target.unlink(missing_ok=True)
            raise ValueError(
                f"The index already has an entry for path '{rel_path}' or id '{note_id}'."
            ) from e
        except Exception:
            # The index insert is what makes this note visible to
            # supersedes and id lookups - if it fails, the file must
            # not stand, so a retry doesn't collide with a half-created note.
            target.unlink(missing_ok=True)
            raise

    _run_with_timeout(_create)
    return {"id": note_id, "path": rel_path}


def _blank_line_separator(existing: str) -> str:
    """The newlines needed before appended content so it starts its own
    paragraph/block instead of running onto the end of the last line.

    Returns "" for an empty file (nothing to separate from), "\\n" if
    `existing` already ends in exactly one newline (completing the blank
    line), or "\\n\\n" if it ends mid-line or already has a full blank line
    is not yet present.
    """
    if not existing:
        return ""
    if existing.endswith("\n\n"):
        return ""
    if existing.endswith("\n"):
        return "\n"
    return "\n\n"


def _append_text(target: Path, content: str) -> None:
    """Append content to a note's file, starting its own paragraph (see _blank_line_separator)."""

    def _append() -> None:
        separator = _blank_line_separator(target.read_text())
        with target.open("a") as f:
            f.write(separator + content)

    _run_with_timeout(_append)


# Shaped like a ULID, as create_note mints them - loosely, so a mistyped id (an O for a 0)
# still reads as an id passed as write_note's `note`, not as a new note's title.
_NOTE_ID = re.compile(r"[0-9A-Z]{26}")


_WRITE_MODES = ("append", "replace")

# Two names this alike are probably one note mistyped, not two notes - unless they differ only in
# digits (a date, a number), which is how a series of notes is named.
_NEAR_NAME_RATIO = 0.88


def _title_of(path: Path, frontmatter: dict[str, Any]) -> str:
    """A note's title for a message: the index's, else its first alias, else its filename."""
    aliases = frontmatter.get("aliases")
    title = _indexed_notes().get(_rel(path), {}).get("title") or (aliases[0] if isinstance(aliases, list) and aliases else None)
    return str(title or path.stem)


def _near_names(slug: str) -> list[Path]:
    """Existing notes whose filename is all but the same as `slug` - a likely typo of one of them."""
    near = []
    for path in _all_notes():
        stem = path.stem.lower()
        if stem == slug or (
            difflib.SequenceMatcher(None, stem, slug).ratio() >= _NEAR_NAME_RATIO
            and re.sub(r"\d", "", stem) != re.sub(r"\d", "", slug)
        ):
            near.append(path)
    return near


def _repeats_the_note(existing: str, content: str) -> bool:
    """Does an append's content contain the note's own text? Then it is a whole-note rewrite sent
    as an append, which would leave the note containing itself twice."""
    body = existing[match.end():] if (match := _FRONTMATTER.match(existing)) else existing
    squash = lambda text: " ".join(text.split())  # noqa: E731
    body, content = squash(body), squash(content)
    return len(body) >= 40 and body[:200] in content


# Nuru's own folder: the one place a note can be rewritten (write_note mode="replace") on Nuru's server.
_NURU_FOLDER = "nuru"


def _replaced_copy_path(rel_path: str) -> Path:
    """Where the version a replace overwrites is kept: trash/<its path>.<timestamp>.md - invisible to
    every tool, like anything in trash/, but there in Obsidian if a rewrite went wrong."""
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    return VAULT_PATH / _TRASH_FOLDER / f"{rel_path.removesuffix('.md')}.{stamp}.md"


def _correction_targets(content: str) -> list[str]:
    """The notes a correction's [[links]] name, by id where they have one (an id survives a rename),
    else by their filename. A correction must link at least one existing note."""
    names = _NoteNames()
    indexed = _indexed_notes()
    targets: list[str] = []
    for link in _link_targets(content):
        found = names.resolve(link)
        if not found:
            continue
        path = found[0]
        note_id = indexed.get(_rel(path), {}).get("id") or _read_frontmatter(path.read_text(errors="ignore")).get("id")
        target = str(note_id) if note_id else path.stem
        if target not in targets:
            targets.append(target)
    if not targets:
        raise ValueError(
            "A correction must link the note it corrects: put [[that note's title]] in content, e.g. "
            "\"[[FreeRTOS Task Management]] says 0.58; it should be 0.36.\""
        )
    return targets


_TYPE_LINES = "; ".join(f"{name} = {what}" for name, what in template.TYPE_DESCRIPTIONS.items())


@mcp.tool(
    description=(
        "Write to a note, creating it if it doesn't exist. note: an existing note's path, title or id; or, for "
        "a new note, a title (it goes in nuru/) or a path under nuru/ or shared/ such as 'nuru/People/Dana Lee' "
        "(folders are made as needed). content: Markdown. mode: 'append' (default) adds content to the end - send "
        "ONLY the new text, never the note's existing text; 'replace' makes content the note's whole new text "
        "(frontmatter kept), so send the complete note - only for notes in nuru/, and the old version is kept. "
        "type (optional, for a new note; default topic): "
        f"{_TYPE_LINES}. type=daily (no note) writes to your own daily note for today - use it for reminders "
        "too. author (optional): nuru (default) or both. A correction is a new note whose content links the "
        "note it corrects as [[its title]]. tags, supersedes (id of a note this replaces), targets (ids of the "
        "notes a correction corrects - found from its [[links]] if left out) and session (YYYY-MM-DD-NN) only "
        "apply to a new note. "
        "Nothing in user/ can be written, nor archived notes. Returns {done, path, id, created}: done says in "
        "words what happened."
    )
)
@_tool
def write_note(
    content: str = "",
    note: str | None = None,
    mode: str = "append",
    type: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    supersedes: str | None = None,
    targets: list[str] | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Write `content` to a note, creating the note first if there isn't one.

    Replaces create_note, append_note and add_to_daily, so the model never
    has to decide whether a note exists before writing to it:

    - type="daily" is today's daily in the daily folder ("today" is the
      server's clock; see _find_daily): written to if it exists, otherwise
      created as "Daily YYYY-MM-DD", author "nuru". `note` is ignored.
    - Otherwise `note` is resolved as read_note resolves a name (path,
      filename, title, alias or id; never anything in trash/). One match is
      written to. Several are refused, listing them - a guess would write to
      the wrong note. None creates a new note: a bare title goes in nuru/; a
      path puts it in that folder, which must be under nuru/ or shared/
      (template.NEW_NOTE_ROOTS). A new note may be empty. Its type defaults to
      template.DEFAULT_TYPE and its author to "nuru".

    mode says how an existing note is written: "append" adds `content` to the
    end (with a blank line before it); "replace" makes `content` the whole of
    the note after its frontmatter, which stays as it was - the one way to
    change existing text, so every guard below applies to it too. Replacing
    with nothing is refused: that is a delete, which is a move to trash.

    Guards against the ways a write lands somewhere it shouldn't:
    - An id that matches nothing is refused, with the closest notes.
    - A new note whose name is all but an existing note's (see _near_names) is
      refused with "did you mean": a mistyped name or path would otherwise
      quietly become a duplicate.
    - An append whose content contains the note's own text is refused (see
      _repeats_the_note): the model meant to rewrite it, which is
      mode="replace", and an append would leave the note twice over.
    - An existing note can't take supersedes or targets, which only make sense
      for a new one, nor type="correction" unless it is one (a correction must
      never become an edit of the note it corrects). Any other type passed
      for an existing note is ignored: the note keeps its own.
    - An archived note is refused: move it out of archive/ first.
    - With OBSIDIAN_PROTECT_USER_AUTHORED on (Nuru's server): nothing under
      user/, and no note the user wrote anywhere else.

    Returns {"done", "path", "id", "created"}: "done" is one sentence naming
    the note, its path and what was done to it, for the reply; "created" says
    whether this call made the note; "id" is null for a note the index
    doesn't know (one made in Obsidian without an id in its frontmatter).
    """
    _check_accessible(VAULT_PATH)
    if mode not in _WRITE_MODES:
        raise ValueError(f"Unknown mode '{mode}'. Use 'append' to add to the end, or 'replace' to rewrite the note.")

    if type == "daily":
        today = _today()
        existing = _find_daily(today)
        if existing is None:
            made = create_note(type="daily", title=f"Daily {today}", author="nuru", body=content, date=today)
            return {"done": f"Created today's daily note 'Daily {today}' at {made['path']}.", **made, "created": True}
        matches = [existing]
    else:
        if note is None or not note.strip():
            raise ValueError(
                "note is missing - pass note=\"<a short title>\" to create a note, or the path of an existing "
                "note to write to it (search finds paths)."
            )
        matches = _matching_notes(note)
        if len(matches) > 1:
            raise ValueError(
                f"'{note}' matches {len(matches)} notes: {', '.join(_rel(p) for p in matches[:5])}. "
                "Pass the path of the one to write to."
            )
        if not matches:
            if _NOTE_ID.fullmatch(note.strip()):
                message = f"No note has the id '{note}'."
                if close := _NoteNames().closest(note):
                    message += f" Closest matches: {', '.join(_rel(p) for p in close)}."
                raise FileNotFoundError(f"{message} To make a new note, pass a title or a path as note.")
            name = note.strip().removesuffix(".md")
            folder, title = (str(Path(name).parent), Path(name).name) if "/" in name else (None, name)
            if near := _near_names(_slugify(title)):
                raise FileExistsError(
                    f"No note is at '{note}', but {', '.join(_rel(p) for p in near[:3])} has nearly the same name. "
                    "Did you mean that note? Write to its path - or give the new note a clearly different name."
                )
            note_type = type or template.DEFAULT_TYPE
            if note_type == "correction" and not targets:
                targets = _correction_targets(content)
            if note_type in template.TYPE_FOLDER:
                _reject_user_folder_for_nuru(f"{folder or template.TYPE_FOLDER[note_type]}/{title}")
            made = create_note(
                type=note_type, title=title, author=author or "nuru", body=content, tags=tags,
                supersedes=supersedes, targets=targets, session=session, folder=folder,
            )
            return {"done": f"Created the {note_type} note '{title}' at {made['path']}.", **made, "created": True}

    target = matches[0]
    rel_path = _rel(target)
    text = target.read_text(errors="ignore")
    frontmatter = _read_frontmatter(text)
    if supersedes is not None or targets is not None:
        raise ValueError(
            f"'{rel_path}' already exists; supersedes and targets only apply to a new note. "
            "Give the new note its own title."
        )
    if type == "correction" and frontmatter.get("type") != "correction":
        raise ValueError(
            f"'{rel_path}' already exists and is a {frontmatter.get('type') or 'untyped'} note, not a correction. "
            "A correction is a new note: give it its own title, with targets."
        )
    _reject_user_folder_for_nuru(rel_path)
    if _is_archived(target):
        raise PermissionError(
            f"'{rel_path}' is archived and can't be written to. Tell the user; only if they ask to restore it, "
            f"move it out with rename_or_move_file('{rel_path}', '{rel_path.removeprefix(_ARCHIVE_FOLDER + '/')}')."
        )
    _reject_user_authored(target, rel_path)
    if not content.strip():
        raise ValueError(
            "content is empty - nothing to add. (To delete a note, move it to trash with rename_or_move_file.)"
        )
    if mode == "append" and _repeats_the_note(text, content):
        raise ValueError(
            f"content repeats the text already in '{rel_path}', so appending it would put the note in twice. "
            "To change the note, call again with mode=\"replace\" and the complete new text; to add to it, "
            "send only the new text."
        )

    title = _title_of(target, frontmatter)
    if mode == "replace":
        if _PROTECT_USER_AUTHORED and Path(rel_path).parts[:1] != (_NURU_FOLDER,):
            raise PermissionError(
                f"'{rel_path}' isn't in {_NURU_FOLDER}/, so it can't be replaced: only Nuru's own notes can be "
                "rewritten. Add to it with mode=\"append\", or write a correction note that links [[it]]."
            )
        head = match.group(0) if (match := _FRONTMATTER.match(text)) else ""
        if " ".join(text[len(head):].split()) == " ".join(content.split()):
            return {
                "done": f"Nothing changed: the new text for '{title}' at {rel_path} is the same as what the note "
                "already says.",
                "path": rel_path,
                "id": _indexed_notes().get(rel_path, {}).get("id") or frontmatter.get("id"),
                "created": False,
            }
        backup = _replaced_copy_path(rel_path)

        def _replace() -> None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(text)
            target.write_text(head + ("\n" if head else "") + content)

        _run_with_timeout(_replace)
        done = (f"Replaced the text of the existing note '{title}' at {rel_path} (its frontmatter kept; the old "
                "version is in trash/).")
    else:
        _append_text(target, content)
        done = f"Added to the end of the existing note '{title}' at {rel_path}."
    note_id = _indexed_notes().get(rel_path, {}).get("id") or frontmatter.get("id")
    return {"done": done, "path": rel_path, "id": note_id, "created": False}


@mcp.tool(
    description="Rename or move a file within the vault. source_path: a vault-relative path, or a note's "
    "title or id. destination_path: the new vault-relative path, or just 'archive' or 'trash' to move it "
    "there under its own folder path (nuru/x.md -> archive/nuru/x.md). Archive keeps a note visible but "
    "read-only; only move a note out of the archive when the user asks to restore it. Trash is final: use "
    "it to delete; nothing comes back out of trash. Returns {done, from, to}: done says in words what happened."
)
@_tool
def rename_or_move_file(source_path: str, destination_path: str) -> dict[str, str]:
    """Rename or move a file within the vault - which is also how a note is archived or deleted.

    If a note create_note made is being moved, its index row is repointed to
    the new path in the same operation, so it stays reachable by id
    afterward - this is what makes archive-by-move (a move, not a rewrite)
    viable under the append-only design.

    Args:
        source_path: Current path of the file, relative to the vault root -
            or, for a note, any name read_note accepts. A name that fits
            several notes is refused rather than guessed. Nothing in trash/
            can be a source: trash is one-way.
        destination_path: New path for the file, relative to the vault root.
            "archive" or "trash" alone moves it into that folder under its
            own path (an archived note going to trash drops the "archive/").

    Returns {"from", "to"}: vault-relative paths before and after.
    """
    _check_accessible(VAULT_PATH)

    source = _resolve_target(source_path)
    if source.is_relative_to(VAULT_PATH) and _is_trashed(source):
        raise PermissionError(f"'{source_path}' is in {_TRASH_FOLDER}/, and nothing can be moved out of trash.")
    if not source.is_file():
        matches = _resolve_notes(source_path)
        if len(matches) > 1:
            raise ValueError(
                f"'{source_path}' matches {len(matches)} notes: {', '.join(_rel(p) for p in matches[:5])}. "
                "Pass the path of the one to move."
            )
        source = matches[0]
        source_path = _rel(source)

    folder = destination_path.strip().strip("/")
    if folder in (_ARCHIVE_FOLDER, _TRASH_FOLDER):
        if folder == _ARCHIVE_FOLDER and _is_archived(source):
            raise ValueError(f"'{source_path}' is already archived.")
        destination_path = f"{folder}/{_rel(source).removeprefix(_ARCHIVE_FOLDER + '/')}"
    destination = _resolve_target(destination_path)
    _reject_hidden(source, source_path)
    _reject_hidden(destination, destination_path)
    _reject_protected_folder(source, source_path)
    _reject_protected_folder(destination, destination_path)
    _reject_user_authored(source, source_path)

    if not source.is_file():
        raise FileNotFoundError(f"No file found at '{source_path}'.")
    if destination.exists():
        raise FileExistsError(f"A file already exists at '{destination_path}'.")

    source_rel = str(source.relative_to(VAULT_PATH))
    destination_rel = str(destination.relative_to(VAULT_PATH))

    def _move() -> None:
        # Opened (and reconciled) before the physical move: _open_index()
        # reconciles against whatever's on disk right now, and reconciling
        # after the move would see source_rel already gone and delete its
        # row out from under update_path below, rather than repoint it.
        with contextlib.closing(_open_index()) as conn:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            try:
                note_index.update_path(conn, source_rel, destination_rel)
            except Exception:
                # Keep the index and the filesystem in agreement: if the
                # index update fails, undo the move rather than leave a
                # note's id pointing at wherever it used to be.
                destination.rename(source)
                raise

    _run_with_timeout(_move)
    name = destination.stem
    if _is_trashed(destination):
        done = f"Deleted '{name}': moved {source_rel} to {destination_rel}. It can't be brought back from trash by Nuru."
    elif _is_archived(destination):
        done = f"Archived '{name}': moved {source_rel} to {destination_rel}. It stays readable but can't be written."
    else:
        done = f"Moved '{name}' from {source_rel} to {destination_rel}."
    return {"done": done, "from": source_rel, "to": destination_rel}


def main() -> None:
    mcp.run()
