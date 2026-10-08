"""Read-only stewardship snapshots and explicit-schema property validation.

All note text is data. No inference, plugin execution, or authored-file writes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from markdown_it import MarkdownIt
import yaml

from obsidian_mcp import mutations
from obsidian_mcp.link_integrity import Policy, _comments


class PropertyLoader(yaml.SafeLoader):
    """Keep dates as authored strings; validate them explicitly after parsing."""


PropertyLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != 'tag:yaml.org,2002:timestamp']
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Document:
    path: str
    revision: str
    text: str
    properties: dict
    locations: dict[str, int]
    body: str
    issues: tuple[dict, ...]


def parse_document(path: str, data: bytes) -> Document:
    text = data.decode('utf-8')
    properties, locations, issues = {}, {}, []
    body = text
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {'---', '...'}), None)
        if end is None:
            issues.append({'line': 1, 'rule': 'frontmatter.syntax', 'evidence': 'Unclosed frontmatter'})
        else:
            body = '\n' * (end + 1) + ''.join(lines[end + 1:])
            try:
                node = yaml.compose(''.join(lines[1:end]), Loader=yaml.BaseLoader)
                if node is not None and not isinstance(node, yaml.MappingNode):
                    raise ValueError('Frontmatter must be a mapping')
                seen = set()
                for key, value in node.value if node else []:
                    if not isinstance(key, yaml.ScalarNode):
                        raise ValueError('Property keys must be scalar strings')
                    name = key.value
                    locations[name] = key.start_mark.line + 2
                    if name in seen:
                        issues.append({'line': locations[name], 'rule': 'frontmatter.duplicate', 'evidence': name})
                    seen.add(name)
                    if name == '<<':
                        raise ValueError('YAML merge keys are unsupported')
                loaded = yaml.load(''.join(lines[1:end]), Loader=PropertyLoader)
                properties = loaded or {}
                if not isinstance(properties, dict) or any(not isinstance(k, str) for k in properties):
                    raise ValueError('Property keys must be strings')
            except (yaml.YAMLError, ValueError, RecursionError) as exc:
                issues.append({'line': 1, 'rule': 'frontmatter.syntax', 'evidence': str(exc)})
                properties = {}
    return Document(path, digest(data), text, properties, locations, body, tuple(issues))


class Snapshot:
    """Hash every eligible file; reuse parsed documents by content revision.

    Full reconciliation handles missed events and deletions. A changed file or
    read failure is a diagnostic, never a clean bill of health.
    """
    def __init__(self, root: Path, policy: Policy | None = None):
        self.root = root.resolve()
        self.policy = policy or Policy()
        self.documents: dict[str, Document] = {}
        self.assets: dict[str, tuple[str, int]] = {}
        self.diagnostics: list[dict] = []
        self.stats: dict = {}

    def refresh(self):
        if not self.root.is_dir():
            raise ValueError('Vault must be an existing directory')
        documents, assets, diagnostics = {}, {}, []
        parsed, bytes_read = 0, 0

        def error(exc):
            diagnostics.append({'path': str(exc.filename), 'reason': str(exc)})

        for folder, dirs, files in os.walk(self.root, followlinks=False, onerror=error):
            base = Path(folder)
            for name in list(dirs):
                path = base / name
                rel = path.relative_to(self.root).as_posix()
                if path.is_symlink():
                    diagnostics.append({'path': rel, 'reason': 'symlink_not_scanned'})
                    dirs.remove(name)
                elif self.policy.excludes(rel):
                    dirs.remove(name)
            for name in sorted(files):
                path = base / name
                rel = path.relative_to(self.root).as_posix()
                if self.policy.excludes(rel):
                    continue
                try:
                    if path.is_symlink():
                        raise ValueError('symlink_not_scanned')
                    before = path.lstat()
                    data = mutations.read(self.root, path)
                    after = path.lstat()
                    bytes_read += len(data)
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError('changed_during_read')
                    revision = digest(data)
                    if path.suffix.lower() == '.md':
                        old = self.documents.get(rel)
                        if old and old.revision == revision:
                            documents[rel] = old
                        else:
                            documents[rel] = parse_document(rel, data)
                            parsed += 1
                    else:
                        assets[rel] = (revision, len(data))
                except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                    diagnostics.append({'path': rel, 'reason': str(exc)})
        self.documents, self.assets, self.diagnostics = documents, assets, diagnostics
        self.stats = {'parsed_notes': parsed, 'notes': len(documents), 'assets': len(assets), 'bytes_read': bytes_read}
        return self

    def report(self, findings: list[dict], **extra) -> dict:
        findings.sort(key=lambda f: (f['source'], f['line'], f['rule'], f['id']))
        revisions = {p: d.revision for p, d in self.documents.items()}
        revisions.update({p: item[0] for p, item in self.assets.items()})
        snapshot = digest(json.dumps({'revisions': revisions, 'findings': findings, 'diagnostics': self.diagnostics}, sort_keys=True, default=str).encode())
        return {'version': '1', 'snapshot': snapshot, 'complete': not self.diagnostics,
                'findings': findings, 'counts': dict(Counter(f['rule'] for f in findings)),
                'diagnostics': self.diagnostics, 'stats': self.stats, 'model_calls': 0, **extra}


def finding(doc: Document, rule: str, key: str, evidence: Any, *, line: int = 1,
            severity: str = 'review', action: str = 'Review the cited evidence') -> dict:
    return {'id': digest(f'{doc.path}\0{rule}\0{key}'.encode()), 'rule': rule, 'rule_version': '1',
            'source': doc.path, 'line': line, 'revision': doc.revision, 'evidence': evidence,
            'severity': severity, 'proposed_action': action}


def body_lines(doc: Document, *, quotes: bool = False) -> list[tuple[int, str]]:
    """Visible body lines, with examples/code/comments masked and line numbers kept."""
    text = _comments(doc.body)
    lines = text.splitlines()
    excluded = set()
    for token in MarkdownIt('commonmark').parse(text):
        if token.type in {'fence', 'code_block'} and token.map:
            excluded.update(range(*token.map))
    return [(i + 1, line) for i, line in enumerate(lines)
            if i not in excluded and (quotes or not line.lstrip().startswith('>'))]


TYPES = {'string': lambda v: isinstance(v, str),
         'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
         'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
         'boolean': lambda v: isinstance(v, bool),
         'list': lambda v: isinstance(v, list),
         'date': lambda v: valid_date(v)}


def valid_date(value) -> bool:
    if isinstance(value, date):
        return type(value) is date
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate_schemas(schemas: list[dict]) -> None:
    if not isinstance(schemas, list):
        raise ValueError('schemas must be a list')
    names = set()
    for schema in schemas:
        if not isinstance(schema, dict) or set(schema) - {'name', 'glob', 'type', 'fields', 'aliases', 'rules'}:
            raise ValueError('Invalid schema keys')
        if not isinstance(schema.get('name'), str) or schema['name'] in names:
            raise ValueError('Schema names must be unique strings')
        names.add(schema['name'])
        if not isinstance(schema.get('glob', '*.md'), str) or not isinstance(schema.get('fields'), dict):
            raise ValueError('Schema requires fields and a path glob')
        for field, spec in schema['fields'].items():
            if not isinstance(field, str) or not isinstance(spec, dict) or set(spec) - {'type', 'required', 'values', 'item_type'}:
                raise ValueError('Invalid field specification')
            if spec.get('type') not in TYPES or spec.get('item_type', 'string') not in TYPES:
                raise ValueError('Unsupported property type')
            if 'required' in spec and not isinstance(spec['required'], bool):
                raise ValueError('required must be boolean')
            if 'values' in spec and not isinstance(spec['values'], list):
                raise ValueError('values must be a list')
        aliases = schema.get('aliases', {})
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or v not in schema['fields'] for k, v in aliases.items()):
            raise ValueError('aliases must map legacy property names to declared fields')
        for rule in schema.get('rules', []):
            if not isinstance(rule, dict) or set(rule) != {'left', 'op', 'right'} or rule['op'] not in {'eq', 'le', 'requires'}:
                raise ValueError('Cross-field rules accept left/op/right: eq, le, requires')
            if rule['left'] not in schema['fields'] or rule['right'] not in schema['fields']:
                raise ValueError('Cross-field rules reference undeclared fields')


def properties(snapshot: Snapshot, schemas: list[dict]) -> dict:
    validate_schemas(schemas)  # Bad configuration fails before note classification.
    findings = []
    for doc in snapshot.documents.values():
        for issue in doc.issues:
            findings.append(finding(doc, issue['rule'], str(issue['line']), issue['evidence'], line=issue['line'], severity='defect'))
        if doc.issues:
            continue
        applicable = [s for s in schemas if fnmatch.fnmatchcase(doc.path, s.get('glob', '*.md'))
                      and ('type' not in s or doc.properties.get('type') == s['type'])]
        declared = {}
        for schema in applicable:
            for key, spec in schema['fields'].items():
                if key in declared and declared[key] != spec:
                    raise ValueError(f'Conflicting schemas for {doc.path}: {key}')
                declared[key] = spec
                value = doc.properties.get(key)
                reason = None
                if key not in doc.properties:
                    if spec.get('required'):
                        reason = 'missing'
                elif not TYPES[spec['type']](value):
                    reason = 'type'
                elif spec['type'] == 'list' and 'item_type' in spec and not all(TYPES[spec['item_type']](v) for v in value):
                    reason = 'item_type'
                elif 'values' in spec and value not in spec['values']:
                    reason = 'value'
                if reason:
                    findings.append(finding(doc, f'property.{reason}', f'{schema["name"]}:{key}',
                                            {'property': key, 'actual': value, 'expected': spec},
                                            line=doc.locations.get(key, 1), severity='defect'))
            for alias, canonical in schema.get('aliases', {}).items():
                if alias in doc.properties:
                    findings.append(finding(doc, 'property.alias', alias, {'property': alias, 'canonical': canonical}, line=doc.locations[alias]))
            for rule in schema.get('rules', []):
                left, right = doc.properties.get(rule['left']), doc.properties.get(rule['right'])
                try:
                    ok = (left == right if rule['op'] == 'eq' else left <= right if rule['op'] == 'le'
                          else left is None or right is not None)
                except TypeError:
                    ok = False
                if not ok:
                    findings.append(finding(doc, 'property.cross_field', json.dumps(rule, sort_keys=True),
                                            {'rule': rule, 'left': left, 'right': right}, severity='defect'))
    return snapshot.report(findings, schemas=[s['name'] for s in schemas])
