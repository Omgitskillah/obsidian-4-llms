"""POSIX vault mutations. Cooperating writers lock; external editors do not."""
import contextlib
import fcntl
import hashlib
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Iterator

_LOCK = threading.RLock()
_LOCAL = threading.local()


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def coordinate(root: Path) -> Iterator[None]:
    # Reentrant for write_note -> create_note. flock coordinates other instances.
    with _LOCK:
        if getattr(_LOCAL, "active", False):
            yield
            return
        fd = os.open(root / ".obsidian-mcp-write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Mutation lock is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            _LOCAL.active = True
            try:
                yield
            finally:
                _LOCAL.active = False
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def parent(root: Path, path: Path, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Pin directories and refuse symlinks, including ones swapped after validation."""
    relative = path.relative_to(root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid mutation path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def _read(fd: int, name: str) -> tuple[bytes, int]:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Mutation target is not a regular file")
        return file.read(), stat.S_IMODE(info.st_mode)


def read(root: Path, path: Path) -> bytes:
    with parent(root, path) as (fd, name):
        return _read(fd, name)[0]


def _stage(fd: int, data: bytes, mode: int) -> str:
    name = f".obsidian-mcp-{uuid.uuid4().hex}.tmp"
    file_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        with os.fdopen(file_fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
    except BaseException:
        os.unlink(name, dir_fd=fd)
        raise
    return name


def create(root: Path, path: Path, data: bytes) -> None:
    with parent(root, path, create=True) as (fd, name):
        temp = _stage(fd, data, 0o600)
        try:
            # Publish a complete file without ever overwriting a competing create.
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        finally:
            os.unlink(temp, dir_fd=fd)


def update(root: Path, path: Path, before: bytes, after: bytes) -> None:
    with parent(root, path) as (fd, name):
        current, mode = _read(fd, name)
        if current != before:
            raise ValueError("Revision conflict: reread the note before writing")
        temp = _stage(fd, after, mode)
        try:
            if _read(fd, name)[0] != before:
                raise ValueError("Revision conflict: note changed before commit")
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass


def remove(root: Path, path: Path, expected: bytes) -> None:
    with parent(root, path) as (fd, name):
        if _read(fd, name)[0] != expected:
            raise ValueError("Recovery refused: file changed")
        os.unlink(name, dir_fd=fd)


@contextlib.contextmanager
def move(root: Path, source: Path, destination: Path) -> Iterator[None]:
    """No-clobber same-filesystem move, keeping source until index update succeeds."""
    with parent(root, source) as (src_fd, src), parent(root, destination, create=True) as (dst_fd, dst):
        before, _ = _read(src_fd, src)
        os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
        try:
            if _read(dst_fd, dst)[0] != before:
                raise ValueError("Revision conflict during move")
            yield
            if _read(src_fd, src)[0] != before:
                raise ValueError("Revision conflict during move")
            os.unlink(src, dir_fd=src_fd)
        except BaseException:
            # Never remove a destination modified by another editor during recovery.
            if _read(dst_fd, dst)[0] != before:
                raise ValueError("Move recovery refused: destination changed")
            os.unlink(dst, dir_fd=dst_fd)
            raise
