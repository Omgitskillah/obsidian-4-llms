"""Model-free, read-only link auditing with a reconciled in-memory reverse index.

Uses CommonMark block parsing so examples in code are not vault references. This
is deliberately a conservative resolver, not a clone of Obsidian's UI heuristics.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import image as image_rule, link as link_rule
from obsidian_mcp import mutations

VERSION = "1"
DEFAULT_EXCLUSIONS = ("Templates", "trash", "Maintenance")
STATUSES = ("resolved", "broken", "ambiguous", "intentional-unwritten", "excluded", "unsupported", "external")
ISSUES = frozenset(("broken", "ambiguous", "unsupported"))


class AuditCancelled(Exception):
    """Stop between files without publishing a partial generation."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _norm(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().strip()


def _mask(value: str) -> str:
    return re.sub(r"[^\n]", " ", value)


def _comments(text: str) -> str:
    """Mask Obsidian comments while preserving literal %% inside code."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    protected = bytearray(len(text))
    for token in MarkdownIt("commonmark").parse(text):
        if token.map is None:
            continue
        start, end = (offsets[n] for n in token.map)
        if token.type in {"fence", "code_block"}:
            protected[start:end] = b"\1" * (end - start)
        elif token.type == "inline":
            runs = list(re.finditer(r"`+", text[start:end]))
            i = 0
            while i < len(runs):
                opening = runs[i]
                pos = start + opening.start()
                escapes = len(text[:pos]) - len(text[:pos].rstrip("\\"))
                if escapes % 2:
                    i += 1
                    continue
                closing = next((j for j in range(i + 1, len(runs)) if len(runs[j].group()) == len(opening.group())), None)
                if closing is None:
                    i += 1
                    continue
                finish = start + runs[closing].end()
                protected[pos:finish] = b"\1" * (finish - pos)
                i = closing + 1
    cursor, pieces = 0, []
    while True:
        opening = text.find("%%", cursor)
        while opening >= 0 and protected[opening]:
            opening = text.find("%%", opening + 2)
        if opening < 0:
            pieces.append(text[cursor:])
            break
        pieces.append(text[cursor:opening])
        closing = text.find("%%", opening + 2)
        finish = len(text) if closing < 0 else closing + 2
        pieces.append(_mask(text[opening:finish]))
        cursor = finish
    return "".join(pieces)


@dataclass(frozen=True)
class Policy:
    exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS
    # Explicit (source path glob, literal target) declarations, not guessed intent.
    intentional: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, path: Path | None) -> Policy:
        if path is None:
            return cls()
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - {"exclusions", "intentional"}:
            raise ValueError("Link policy accepts only exclusions and intentional")
        exclusions = data.get("exclusions", list(DEFAULT_EXCLUSIONS))
        intentional = data.get("intentional", [])
        if not isinstance(exclusions, list) or not all(isinstance(x, str) and x for x in exclusions):
            raise ValueError("exclusions must be a list of nonempty folder paths/globs")
        if not isinstance(intentional, list) or not all(
            isinstance(x, dict) and set(x) == {"source", "target"}
            and all(isinstance(x[k], str) and x[k] for k in x) for x in intentional
        ):
            raise ValueError("intentional must contain {source, target} string pairs")
        return cls(tuple(exclusions), tuple((x["source"], x["target"]) for x in intentional))

    def excludes(self, path: str) -> bool:
        parts = PurePosixPath(path).parts
        return any(p.startswith(".") for p in parts) or any(
            path == pattern or path.startswith(pattern.rstrip("/") + "/") or fnmatch.fnmatchcase(path, pattern)
            for pattern in self.exclusions
        )


@dataclass(frozen=True)
class Reference:
    line: int
    raw: str
    target: str
    syntax: str
    embed: bool


@dataclass
class Note:
    revision: str
    references: list[Reference]
    headings: Counter[str]
    blocks: Counter[str]
    warnings: list[str]


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark")

    def record(state, start, raw, target, syntax, embed):
        if not state.env.get("capture", True):
            return
        line = state.env["base_line"] + state.src[:start].count("\n")
        state.env["references_found"].append(Reference(line, raw, target, syntax, embed))

    def wiki(state, silent):
        start = state.pos
        embed = state.src.startswith("![[", start)
        opening = start + int(embed)
        if not state.src.startswith("[[", opening):
            return False
        end = state.src.find("]]", opening + 2, state.posMax)
        if end < 0 or "\n" in state.src[opening:end] or "[" in state.src[opening + 2:end]:
            return False
        raw = state.src[start:end + 2]
        inner = state.src[opening + 2:end]
        target, _, label = inner.partition("|")
        if not silent:
            record(state, start, raw, target.strip(), "wiki", embed)
            state.push("text", "", 0).content = label or target
        state.pos = end + 2
        return True

    def wrapped(rule, kind):
        def parse(state, silent):
            start, before = state.pos, len(state.tokens)
            capture = state.env.get("capture", True)
            if kind == "image":
                # Links in image alt text are display content, not references.
                state.env["capture"] = False
            try:
                ok = rule(state, silent)
            finally:
                state.env["capture"] = capture
            if ok and not silent:
                token = next((t for t in state.tokens[before:] if t.type == ("image" if kind == "image" else "link_open")), None)
                if token is not None:
                    record(state, start, state.src[start:state.pos], token.attrGet("src" if kind == "image" else "href") or "", "markdown", kind == "image")
            return ok
        return parse

    def inlines(state):
        for token in state.tokens:
            if token.type == "inline":
                state.env["base_line"] = (token.map or [0])[0] + 1
                token.children = []
                state.md.inline.parse(token.content, state.md, state.env, token.children)

    md.inline.ruler.before("image", "vault_wiki", wiki)
    md.inline.ruler.at("link", wrapped(link_rule, "link"))
    md.inline.ruler.at("image", wrapped(image_rule, "image"))
    md.core.ruler.at("inline", inlines)
    return md


def _plain(tokens) -> str:
    return "".join(t.content if t.type in {"text", "code_inline"} else " " if t.type in {"softbreak", "hardbreak"} else "" for t in tokens)


def parse_note(data: bytes) -> Note:
    text = data.decode("utf-8-sig")
    warnings = []
    # Frontmatter is not Markdown. Report missing coverage instead of inventing
    # headings/links from its YAML structure. A future property parser can add it.
    fm = re.match(r"\A---\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", text, re.S)
    if fm:
        if "[[" in fm.group() or re.search(r"\]\([^)]*\)", fm.group()):
            warnings.append("frontmatter_links_not_audited")
        text = _mask(fm.group()) + text[fm.end():]
    # Obsidian comments: preserve source line numbering.
    text = _comments(text)
    env = {"references_found": [], "base_line": 1, "capture": True}
    tokens = _parser().parse(text, env)
    headings: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    hierarchy = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1:])
            inline = tokens[i + 1]
            name = _norm(_plain(inline.children or []))
            while hierarchy and hierarchy[-1][0] >= level:
                hierarchy.pop()
            hierarchy.append((level, name))
            headings[name] += 1
            breadcrumb = "#".join(x[1] for x in hierarchy)
            if breadcrumb != name:
                headings[breadcrumb] += 1
        if token.type == "inline":
            # A code span containing ^id is not a block declaration.
            plain = "".join(t.content if t.type == "text" else "\n" if t.type in {"softbreak", "hardbreak"} else "\ufffc" for t in token.children or [])
            for match in re.finditer(r"(?:^|[ \t\n])\^([A-Za-z0-9-]+)[ \t]*(?=\n|$)", plain):
                blocks[match.group(1)] += 1
        if token.type in {"html_block", "html_inline"}:
            warnings.append("html_links_not_audited")
        if token.type == "inline" and any(t.type == "html_inline" for t in token.children or []):
            warnings.append("html_links_not_audited")
    refs = sorted(env["references_found"], key=lambda r: (r.line, r.raw, r.target))
    return Note(_digest(data), refs, headings, blocks, sorted(set(warnings)))


class LinkIndex:
    """One vault per instance; full reconciliation, cached parsing, no disk writes.

    Hashes are checked every scan, including no-change scans. Restart rebuilds
    derived state. Snapshots/queries are serialized to avoid mixed generations.
    """
    def __init__(self, vault: Path, policy: Policy | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.vault = vault.expanduser().resolve()
        self.policy = policy or Policy()
        self.should_stop = should_stop or (lambda: False)
        self.notes: dict[str, Note] = {}
        self.files: set[str] = set()
        self.excluded: set[str] = set()
        self.blocked: set[str] = set()
        self.results: list[dict] = []
        self.reverse: dict[str, list[dict]] = {}
        self.diagnostics: list[dict] = []
        self.stats: dict = {}
        self._candidate_catalog = None
        self._suggestions: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    def refresh(self) -> dict:
        with self._lock:
            return self._refresh()

    def _refresh(self) -> dict:
        start = time.perf_counter()
        if not self.vault.is_dir():
            raise FileNotFoundError(f"Vault directory not found: {self.vault}")
        files, excluded, blocked, notes, diagnostics = set(), set(), set(), {}, []
        parsed = reused = bytes_read = 0
        def error(exc):
            diagnostics.append({"path": str(Path(exc.filename).relative_to(self.vault)), "reason": "unreadable_directory"})
        for folder, dirs, names in os.walk(self.vault, followlinks=False, onerror=error):
            base = Path(folder)
            # Catalog excluded files without reading their contents, so an explicit
            # excluded target is distinct from a nonexistent target.
            dirs[:] = sorted(dirs)
            for d in dirs[:]:
                path = base / d
                if d.startswith("."):
                    dirs.remove(d)
                elif path.is_symlink():
                    blocked.add(path.relative_to(self.vault).as_posix())
                    dirs.remove(d)
            for name in sorted(names):
                if self.should_stop():
                    raise AuditCancelled()
                path = base / name
                rel = path.relative_to(self.vault).as_posix()
                if path.is_symlink():
                    blocked.add(rel)
                    continue
                if self.policy.excludes(rel):
                    excluded.add(rel)
                    continue
                files.add(rel)
                if path.suffix.lower() != ".md":
                    continue
                try:
                    before = path.lstat()
                    data = mutations.read(self.vault, path)
                    after = path.lstat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError("changed_during_scan")
                    bytes_read += len(data)
                    rev = _digest(data)
                    if rel in self.notes and self.notes[rel].revision == rev:
                        notes[rel] = self.notes[rel]
                        reused += 1
                    else:
                        notes[rel] = parse_note(data)
                        parsed += 1
                except (OSError, UnicodeError, ValueError) as exc:
                    diagnostics.append({"path": rel, "reason": "changed_during_scan" if str(exc) == "changed_during_scan" else "unreadable_note"})
        self.files, self.excluded, self.blocked, self.notes = files, excluded, blocked, notes
        self.diagnostics = diagnostics + [dict(path=p, reason=w) for p, note in notes.items() for w in note.warnings]
        if self._candidate_catalog != set(notes):
            self._suggestions.clear()
            self._candidate_catalog = set(notes)
        self._grams = defaultdict(set)
        for path in sorted(notes):
            stem = _norm(PurePosixPath(path).stem)
            for gram in {stem[i:i + 3] for i in range(max(0, len(stem) - 2))}:
                self._grams[gram].add(path)
        self._names = defaultdict(list)
        self._paths = defaultdict(list)
        for path in sorted(files | excluded | blocked):
            self._paths[unicodedata.normalize("NFC", path)].append(path)
            name = PurePosixPath(path).name
            self._names[_norm(name)].append(path)
            if PurePosixPath(path).suffix.lower() == ".md":
                self._names[_norm(name[:-3])].append(path)
        results, reverse = [], defaultdict(list)
        for source, note in sorted(notes.items()):
            occurrences = Counter()
            for ref in note.references:
                occurrences[ref.raw] += 1
                result = self._resolve(source, ref)
                result.update(source=source, revision=note.revision, line=ref.line, raw=ref.raw,
                              target=ref.target, syntax=ref.syntax, embed=ref.embed,
                              finding_id=_digest(f"{VERSION}\0{source}\0{ref.raw}\0{occurrences[ref.raw]}".encode()))
                results.append(result)
                if result.get("path"):
                    reverse[result["path"]].append(result)
        self.results, self.reverse = results, dict(reverse)
        self.stats = {"parsed_notes": parsed, "reused_notes": reused, "notes": len(notes),
                      "bytes_read": bytes_read, "references": len(results),
                      "duration_ms": round((time.perf_counter() - start) * 1000, 3), "model_calls": 0}
        return dict(self.stats)

    def _candidates(self, name: str) -> list[dict]:
        key = _norm(PurePosixPath(name).stem)
        if key not in self._suggestions:
            overlap = Counter()
            for gram in {key[i:i + 3] for i in range(max(0, len(key) - 2))}:
                overlap.update(self._grams.get(gram, ()))
            pool = sorted(overlap, key=lambda p: (-overlap[p], p))[:250]
            if len(key) < 3:
                pool = sorted(self.notes)[:250]
            scored = [(difflib.SequenceMatcher(None, key, _norm(PurePosixPath(p).stem)).ratio(), p) for p in pool]
            scored = sorted((s, p) for s, p in scored if s >= 0.65)
            scored.sort(key=lambda x: (-x[0], x[1]))
            self._suggestions[key] = [{"path": p, "basis": "filename_similarity", "similarity": round(s, 3)} for s, p in scored[:3]]
        # Revisions may change without changing the filename candidate cache.
        return [dict(c, revision=self.notes[c["path"]].revision) for c in self._suggestions[key] if c["path"] in self.notes]

    def _resolve(self, source: str, ref: Reference) -> dict:
        target = ref.target.strip()
        if ref.syntax == "wiki" and target.startswith(("##", "^^")):
            return dict(status="unsupported", reason="vault_wide_anchor_search")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
            return dict(status="external", reason="external_uri")
        # Split before decoding: an encoded # in a filename isn't an anchor.
        name, sep, fragment = target.partition("#")
        if ref.syntax == "markdown" and urlsplit(name).query:
            return dict(status="unsupported", reason="local_query_string")
        name, fragment = unquote(name), unquote(fragment)
        if "\x00" in name or "\\" in name:
            return dict(status="unsupported", reason="unsafe_path")
        if not name:
            candidates = [source]
        elif ref.syntax == "wiki" and "/" not in name:
            candidates = list(dict.fromkeys(self._names.get(_norm(name), [])))
        else:
            # Obsidian can emit vault-root paths without a leading slash, while
            # CommonMark paths are source-relative. Explicit ./, ../ and / remove
            # that uncertainty; otherwise consider both and expose ambiguity.
            roots = [posixpath.join(posixpath.dirname(source), name)] if ref.syntax == "markdown" and not name.startswith("/") else [name.lstrip("/")]
            if ref.syntax == "markdown" and not name.startswith(("/", "./", "../")):
                roots.append(name)
            roots = list(dict.fromkeys(posixpath.normpath(r) for r in roots))
            if any(r == ".." or r.startswith("../") for r in roots):
                return dict(status="unsupported", reason="outside_vault")
            paths = []
            for rel in roots:
                paths.append(rel)
                if not PurePosixPath(rel).suffix:
                    paths.append(rel + ".md")
            candidates = list(dict.fromkeys(p for request in paths
                for p in self._paths.get(unicodedata.normalize("NFC", request), ())
                if p in self.files or p in self.excluded))
            if not candidates and any(rel == p or rel.startswith(p + "/") for rel in roots for p in self.blocked):
                return dict(status="unsupported", reason="symlink_target")
            if not candidates and any(self.policy.excludes(r) for r in roots):
                return dict(status="excluded", reason="policy_exclusion", path=next(r for r in roots if self.policy.excludes(r)))
            if not candidates and ref.syntax == "markdown" and "/" not in name:
                candidates = list(dict.fromkeys(self._names.get(_norm(name), [])))
        if not candidates:
            if any(fnmatch.fnmatchcase(source, pattern) and target == literal for pattern, literal in self.policy.intentional):
                return dict(status="intentional-unwritten", reason="declared_placeholder")
            if any(d["reason"] == "unreadable_directory" for d in self.diagnostics):
                return dict(status="unsupported", reason="incomplete_catalog")
            return dict(status="broken", reason="missing_file", candidates=self._candidates(name))
        if len(candidates) > 1:
            return dict(status="ambiguous", reason="multiple_files", candidates=[{"path": p, "basis": "matching_filename"} for p in candidates[:10]], candidate_count=len(candidates))
        path = candidates[0]
        if path in self.blocked:
            return dict(status="unsupported", reason="symlink_target")
        if path in self.excluded:
            return dict(status="excluded", reason="policy_exclusion", path=path)
        if PurePosixPath(path).suffix.lower() == ".md" and path not in self.notes:
            return dict(status="unsupported", reason="unreadable_target", path=path)
        if not sep or not fragment:
            return dict(status="resolved", reason="file_exists", path=path)
        if path not in self.notes:
            return dict(status="unsupported", reason="attachment_fragment", path=path)
        note = self.notes[path]
        if fragment.startswith("^"):
            count = note.blocks[fragment[1:]]
            kind = "block"
        else:
            kind = "heading"
            key = _norm(fragment)
            count = note.headings[key]
            if not count and ref.syntax == "markdown":
                count = sum(n for h, n in note.headings.items() if "#" not in h and re.sub(r"[^\w\- ]", "", h).replace(" ", "-") == key)
        if not count:
            known = note.blocks if kind == "block" else note.headings
            close = difflib.get_close_matches(fragment.lstrip("^") if kind == "block" else _norm(fragment), sorted(known), n=3, cutoff=0.65)
            return dict(status="broken", reason=f"missing_{kind}", path=path, target_revision=note.revision,
                        candidates=[{"path": path, "fragment": "^" + h if kind == "block" else h,
                                     "revision": note.revision, "basis": f"{kind}_similarity"} for h in close])
        if count > 1:
            return dict(status="ambiguous", reason=f"duplicate_{kind}", path=path, target_revision=note.revision)
        return dict(status="resolved", reason=f"{kind}_exists", path=path, target_revision=note.revision)

    def report(self, *, limit: int = 50, offset: int = 0, target_path: str | None = None, include_resolved: bool = False) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1..100 and offset must be nonnegative")
        with self._lock:
            self._refresh()
            selected = self.reverse.get(target_path, []) if target_path is not None else self.results
            rows = selected if include_resolved or target_path is not None else [r for r in selected if r["status"] in ISSUES]
            counts = Counter(r["status"] for r in selected)
            snapshot = _digest(json.dumps({"results": self.results, "diagnostics": self.diagnostics, "policy": asdict(self.policy)}, sort_keys=True).encode())
            return {"version": VERSION, "snapshot": snapshot, "complete": not self.diagnostics,
                    "counts": {s: counts[s] for s in STATUSES}, "count": len(rows),
                    "references": rows[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(rows) else None,
                    "diagnostics": self.diagnostics[:100], "diagnostic_count": len(self.diagnostics),
                    "stats": dict(self.stats), "target_path": target_path,
                    "hint": "Read-only audit. Candidates are filename evidence, not confirmed repairs. Compare snapshot values between pages; restart pagination if changed."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit local vault references without Obsidian or models; writes no files.")
    parser.add_argument("vault", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--target", help="Exact vault-relative path for incoming references")
    parser.add_argument("--all", action="store_true", help="Include resolved/excluded/intentional/external references")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    index = LinkIndex(args.vault, Policy.load(args.policy))
    print(json.dumps(index.report(limit=args.limit, offset=args.offset, target_path=args.target, include_resolved=args.all), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
