"""Excluded SQLite sidecar for evidence-scoped review and conservative identity."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import uuid

from obsidian_mcp.stewardship import Snapshot, digest

APP_ID = 0x4E535431


class State:
    def __init__(self, path: Path, snapshot: Snapshot):
        self.path = path.absolute()
        root = snapshot.root
        if self.path.is_relative_to(root) and not snapshot.policy.excludes(self.path.relative_to(root).as_posix()):
            raise ValueError('State database must be outside authored notes or excluded by policy')
        if any(p.is_symlink() for p in [self.path, *self.path.parents]):
            raise ValueError('State database path cannot contain symlinks')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=5)
        app = self.conn.execute('PRAGMA application_id').fetchone()[0]
        tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app not in {0, APP_ID} or (app == 0 and tables):
            self.conn.close()
            raise ValueError('Refusing an unrelated SQLite database')
        self.conn.execute(f'PRAGMA application_id={APP_ID}')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS entities(path TEXT PRIMARY KEY, entity TEXT NOT NULL, revision TEXT NOT NULL, hint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, evidence_revision TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        stored_root = self.conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if stored_root and stored_root[0] != str(root):
            self.conn.close()
            raise ValueError('State database belongs to another vault')
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES('root',?)", (str(root),))

    def close(self):
        self.conn.close()

    def reconcile(self, snapshot: Snapshot, findings: list[dict], *, complete: bool, namespace: str) -> list[dict]:
        old = {row[0]: row[1:] for row in self.conn.execute('SELECT path,entity,revision,hint FROM entities')}
        removed = {p: item for p, item in old.items() if p not in snapshot.documents}
        additions = {p: d for p, d in snapshot.documents.items() if p not in old}
        entities, warnings = {}, []
        claimed = set()
        for path, doc in sorted(snapshot.documents.items()):
            if path in old:
                entities[path] = old[path][0]
                continue
            candidates = [p for p, item in removed.items() if (doc.identity_hint and item[2] == doc.identity_hint) or item[1] == doc.revision]
            # Never assign one entity to two identical new notes or guess a rename.
            viable = [p for p in candidates if p not in claimed and sum(
                (new.identity_hint and removed[p][2] == new.identity_hint) or removed[p][1] == new.revision
                for new in additions.values()) == 1]
            if len(viable) == 1:
                source = viable[0]
                entities[path] = removed[source][0]
                claimed.add(source)
            else:
                entities[path] = uuid.uuid4().hex
                if candidates:
                    warnings.append({'path': path, 'reason': 'ambiguous_rename_identity', 'candidates': candidates})
        rows, active = [], set()
        with self.conn:
            if complete:
                self.conn.execute('DELETE FROM entities')
            for path, doc in snapshot.documents.items():
                self.conn.execute('INSERT OR REPLACE INTO entities VALUES(?,?,?,?)', (path, entities[path], doc.revision, doc.identity_hint))
            for original in findings:
                row = dict(original)
                entity = entities.get(row['source'], row['source'])
                row['id'] = digest(f'{namespace}\0{entity}\0{row["rule"]}\0{row["rule_version"]}\0{row["entity_key"]}'.encode())
                row['evidence_revision'] = digest(json.dumps([row['revision'], row['evidence']], sort_keys=True, default=str).encode())
                active.add(row['id'])
                old_finding = self.conn.execute('SELECT evidence_revision,status FROM findings WHERE id=?', (row['id'],)).fetchone()
                status = old_finding[1] if old_finding and old_finding[0] == row['evidence_revision'] and old_finding[1] != 'resolved' else 'open'
                row['status'] = status
                self.conn.execute('INSERT OR REPLACE INTO findings VALUES(?,?,?,?)', (row['id'], row['evidence_revision'], status, json.dumps(row, default=str, ensure_ascii=False)))
                rows.append(row)
            if complete:
                # Namespace ownership is in the stored payload, not guessed by rule.
                for id_, payload in self.conn.execute('SELECT id,payload FROM findings').fetchall():
                    if id_ not in active and json.loads(payload).get('namespace') == namespace:
                        self.conn.execute("UPDATE findings SET status='resolved' WHERE id=?", (id_,))
            for row in rows:
                row['namespace'] = namespace
                self.conn.execute('UPDATE findings SET payload=? WHERE id=?', (json.dumps(row, default=str, ensure_ascii=False), row['id']))
        snapshot.diagnostics.extend(warnings)
        return rows

    def review(self, id_: str, expected_revision: str, status: str):
        if status not in {'acknowledged', 'suppressed', 'open'}:
            raise ValueError('Review state must be acknowledged, suppressed, or open')
        with self.conn:
            row = self.conn.execute('SELECT evidence_revision FROM findings WHERE id=?', (id_,)).fetchone()
            if not row or row[0] != expected_revision:
                raise ValueError('Finding evidence changed; rescan before reviewing')
            self.conn.execute('UPDATE findings SET status=? WHERE id=?', (status, id_))

    def counts(self):
        return dict(Counter(row[0] for row in self.conn.execute('SELECT status FROM findings')))
