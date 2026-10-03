"""SQLite index of notes created via create_note: id -> path/type/title/author/created/supersedes.

The vault's Markdown files remain the source of truth for content and
frontmatter; this index exists only so create_note can validate `supersedes`
and so ID-based lookups can resolve an id to a path without
scanning the vault. It only knows about notes created through create_note -
a note added any other way (e.g. directly in Obsidian) has no row here.
"""

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    created TEXT NOT NULL,
    supersedes TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the index, creating its schema if this is a fresh database."""
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def insert_note(
    conn: sqlite3.Connection,
    *,
    id: str,
    path: str,
    type: str,
    title: str,
    author: str,
    created: str,
    supersedes: str | None,
) -> None:
    conn.execute(
        "INSERT INTO notes (id, path, type, title, author, created, supersedes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, path, type, title, author, created, supersedes),
    )
    conn.commit()


def note_exists(conn: sqlite3.Connection, note_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row is not None


def get_path(conn: sqlite3.Connection, note_id: str) -> str | None:
    row = conn.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else None


def all_by_path(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Every indexed note's id/type/title/created, keyed by its vault-relative path."""
    rows = conn.execute("SELECT path, id, type, title, created FROM notes").fetchall()
    return {
        path: {"id": id, "type": type, "title": title, "created": created}
        for path, id, type, title, created in rows
    }


def update_path(conn: sqlite3.Connection, old_path: str, new_path: str) -> None:
    """Repoint whichever indexed note (if any) is at old_path to new_path.

    A no-op if nothing is indexed at old_path - not every file a caller
    renames/moves is a note create_note made.
    """
    conn.execute("UPDATE notes SET path = ? WHERE path = ?", (new_path, old_path))
    conn.commit()


def delete_by_path(conn: sqlite3.Connection, path: str) -> None:
    """Remove whichever indexed note (if any) is at path. A no-op if none is."""
    conn.execute("DELETE FROM notes WHERE path = ?", (path,))
    conn.commit()


def reconcile(conn: sqlite3.Connection, vault_path: Path) -> list[str]:
    """Remove rows whose indexed path no longer exists on disk under vault_path.

    The index only ever learns about a filesystem change through this
    server's own tools (create_note/rename_or_move_file/delete_file_or_
    directory) - it has no way to notice the vault changing any other way
    (Obsidian sync, a git pull, a note deleted directly in the UI, or the
    whole vault cleared out from under it). Left unreconciled, a stale row
    can make a legitimate create_note collide with a path that no longer
    exists on disk but is still UNIQUE-constrained in the index.

    Returns the paths of the rows removed.
    """
    rows = conn.execute("SELECT path FROM notes").fetchall()
    stale = [path for (path,) in rows if not (vault_path / path).is_file()]
    for path in stale:
        conn.execute("DELETE FROM notes WHERE path = ?", (path,))
    if stale:
        conn.commit()
    return stale
