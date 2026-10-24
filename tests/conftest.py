"""Tests write notes as Obsidian does on every OS, UTF-8 with \\n line ends, and read them as UTF-8.
Windows' defaults (its code page, \\r\\n) would otherwise make every test's notes Windows-only files.
Only the tests' own calls that name no encoding change; the server names its encoding itself."""
import pathlib
import sys

if sys.platform == "win32":
    _write_text, _read_text = pathlib.Path.write_text, pathlib.Path.read_text

    def _write(self, data, encoding=None, errors=None, newline=None):
        return _write_text(self, data, encoding or "utf-8", errors, "\n" if newline is None else newline)

    def _read(self, encoding=None, errors=None, newline=None):
        return _read_text(self, encoding or "utf-8", errors, newline)

    pathlib.Path.write_text, pathlib.Path.read_text = _write, _read
