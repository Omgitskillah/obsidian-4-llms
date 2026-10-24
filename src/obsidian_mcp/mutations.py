"""Vault mutations. Cooperating writers lock; external editors do not.

On POSIX every step goes through pinned directory descriptors, so a symlink swapped in after a check
can't redirect it. Windows has no directory descriptors: there the root and each directory on the way
are held open, refusing a symlink or junction, without letting anyone rename or delete them, for the
whole operation, so the path through them leads where it was checked; a file is checked once open,
when Windows no longer lets it be renamed or deleted either.
"""
import contextlib
import errno
import hashlib
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Iterator

_WINDOWS = os.name == "nt"
if _WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]
else:
    import fcntl

# What Windows lacks is a no-op there; O_BINARY keeps Windows from translating line endings.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)

_LOCK = threading.RLock()
_LOCAL = threading.local()


def started() -> bool:
    """Whether this coordinated operation may have changed a vault path."""
    return getattr(_LOCAL, "started", False)


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock(fd: int, on: bool) -> None:
    if not _WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_EX if on else fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    if not on:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    while True:  # LK_LOCK gives up after ten tries a second apart; flock waits as long as it takes
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return
        except OSError as error:
            if error.errno != errno.EDEADLK:
                raise


@contextlib.contextmanager
def coordinate(root: Path) -> Iterator[None]:
    # Reentrant for write_note -> create_note. The file lock coordinates other instances.
    with _LOCK:
        if getattr(_LOCAL, "active", False):
            yield
            return
        if _WINDOWS:
            fd = _open(str(root), ".obsidian-mcp-write.lock", os.O_CREAT | os.O_RDWR, 0o600)
        else:
            fd = os.open(root / ".obsidian-mcp-write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Mutation lock is not a regular file")
            _lock(fd, True)
            _LOCAL.active = True
            _LOCAL.started = False
            try:
                yield
            finally:
                _LOCAL.active = False
                _lock(fd, False)
        finally:
            os.close(fd)


# A reparse point that names another place (symlink, junction...); others, such as OneDrive's
# placeholders, are the file itself.
_NAME_SURROGATE = 0x20000000


def _check(path: str) -> os.stat_result:
    """Windows: what lstat says of a file, refusing a symlink or junction."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or info.st_reparse_tag & _NAME_SURROGATE:
        raise OSError(errno.ELOOP, "Refusing a symlink or junction", path)
    return info


def _pin(path: str) -> int:
    """Windows: a handle on the directory at path (not where a symlink or junction there leads), which
    stops it being renamed or deleted until closed. It asks to list the directory: Windows doesn't
    apply sharing to a handle that only reads attributes, and so wouldn't stop a rename."""
    list_directory, read_attributes = 0x1, 0x80
    share_read_write = 0x1 | 0x2  # not FILE_SHARE_DELETE
    open_existing, backup_semantics, open_reparse_point = 3, 0x02000000, 0x00200000
    handle = _kernel32.CreateFileW(path, list_directory | read_attributes, share_read_write, None, open_existing,
                                   backup_semantics | open_reparse_point, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _AttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # FileAttributeTagInfo
            raise ctypes.WinError(ctypes.get_last_error())
        if info.FileAttributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and info.ReparseTag & _NAME_SURROGATE:
            raise OSError(errno.ELOOP, "Refusing a symlink or junction", path)
        if not info.FileAttributes & stat.FILE_ATTRIBUTE_DIRECTORY:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)
    except BaseException:
        _kernel32.CloseHandle(handle)
        raise
    return handle


@contextlib.contextmanager
def parent(root: Path, path: Path, *, create: bool = False) -> Iterator[tuple[int | str, str]]:
    """Pin directories and refuse symlinks, including ones swapped after validation. Yields the
    directory (a descriptor; on Windows its path) and the name in it."""
    relative = path.relative_to(root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid mutation path")
    if _WINDOWS:
        directory = str(root)
        pinned = [_pin(directory)]
        try:
            for component in relative.parts[:-1]:
                directory = os.path.join(directory, component)
                if create:
                    try:
                        os.mkdir(directory)
                    except FileExistsError:
                        pass
                pinned.append(_pin(directory))
            yield directory, relative.name
        finally:
            for handle in reversed(pinned):
                _kernel32.CloseHandle(handle)
        return
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


def _open(directory: int | str, name: str, flags: int, mode: int = 0o777) -> int:
    """Open name in directory without following a symlink."""
    if not _WINDOWS:
        return os.open(name, flags | _NOFOLLOW, mode, dir_fd=directory)
    path = os.path.join(directory, name)
    fd = os.open(path, flags | _BINARY, mode)
    try:
        # Open, the file can't be renamed or deleted, so the name still leads to it.
        opened, named = os.fstat(fd), _check(path)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise OSError(errno.ELOOP, "Refusing a symlink or junction", path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_read(directory: int | str, name: str) -> int:
    return _open(directory, name, os.O_RDONLY | _NONBLOCK)


def _unlink(directory: int | str, name: str) -> None:
    if _WINDOWS:
        os.unlink(os.path.join(directory, name))
    else:
        os.unlink(name, dir_fd=directory)


def _link(src_dir: int | str, src: str, dst_dir: int | str, dst: str) -> None:
    if _WINDOWS:
        os.link(os.path.join(src_dir, src), os.path.join(dst_dir, dst))
    else:
        os.link(src, dst, src_dir_fd=src_dir, dst_dir_fd=dst_dir, follow_symlinks=False)


def _read(fd: int | str, name: str) -> tuple[bytes, int]:
    with os.fdopen(open_read(fd, name), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Mutation target is not a regular file")
        return file.read(), stat.S_IMODE(info.st_mode)


def read(root: Path, path: Path) -> bytes:
    with parent(root, path) as (fd, name):
        return _read(fd, name)[0]


def _stage(fd: int | str, data: bytes, mode: int) -> str:
    name = f".obsidian-mcp-{uuid.uuid4().hex}.tmp"
    file_fd = _open(fd, name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(file_fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
    except BaseException:
        _unlink(fd, name)
        raise
    return name


def create(root: Path, path: Path, data: bytes) -> None:
    _LOCAL.started = True
    with parent(root, path, create=True) as (fd, name):
        temp = _stage(fd, data, 0o600)
        try:
            # Publish a complete file without ever overwriting a competing create.
            _link(fd, temp, fd, name)
        finally:
            _unlink(fd, temp)


def update(root: Path, path: Path, before: bytes, after: bytes) -> None:
    _LOCAL.started = True
    with parent(root, path) as (fd, name):
        current, mode = _read(fd, name)
        if current != before:
            raise ValueError("Revision conflict: reread the note before writing")
        temp = _stage(fd, after, mode)
        try:
            if _read(fd, name)[0] != before:
                raise ValueError("Revision conflict: note changed before commit")
            if _WINDOWS:
                os.replace(os.path.join(fd, temp), os.path.join(fd, name))
            else:
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                _unlink(fd, temp)
            except FileNotFoundError:
                pass


def remove(root: Path, path: Path, expected: bytes) -> None:
    _LOCAL.started = True
    with parent(root, path) as (fd, name):
        if _read(fd, name)[0] != expected:
            raise ValueError("Recovery refused: file changed")
        _unlink(fd, name)


@contextlib.contextmanager
def move(root: Path, source: Path, destination: Path) -> Iterator[None]:
    """No-clobber same-filesystem move, keeping source until index update succeeds."""
    _LOCAL.started = True
    with parent(root, source) as (src_fd, src), parent(root, destination, create=True) as (dst_fd, dst):
        before, _ = _read(src_fd, src)
        _link(src_fd, src, dst_fd, dst)
        try:
            if _read(dst_fd, dst)[0] != before:
                raise ValueError("Revision conflict during move")
            yield
            if _read(src_fd, src)[0] != before:
                raise ValueError("Revision conflict during move")
            _unlink(src_fd, src)
        except BaseException:
            # Never remove a destination modified by another editor during recovery.
            if _read(dst_fd, dst)[0] != before:
                raise ValueError("Move recovery refused: destination changed")
            _unlink(dst_fd, dst)
            raise
