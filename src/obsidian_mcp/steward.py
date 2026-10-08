"""Configured read-only stewardship runner, CLI, and bounded digest."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import threading

from obsidian_mcp import semantic
from obsidian_mcp.link_integrity import LinkIndex, Policy, AuditCancelled
from obsidian_mcp.stewardship import Snapshot, finding, properties, attachments, tasks, project_reviews, inbox_triage, digest
from obsidian_mcp.steward_tables import tables
from obsidian_mcp.steward_duplicates import duplicates
from obsidian_mcp.steward_claims import contradictions
from obsidian_mcp.steward_graph import knowledge_map
from obsidian_mcp.steward_state import State

PACKAGES = {'properties', 'links', 'attachments', 'tasks', 'tables', 'reviews', 'triage', 'duplicates', 'claims', 'graph', 'index'}


def bounded(value, depth=0):
    if depth > 8:
        return {'truncated': True, 'reason': 'nesting_limit'}
    if isinstance(value, str) and len(value) > 2000:
        return {'excerpt': value[:2000], 'truncated': True, 'characters': len(value)}
    if isinstance(value, list):
        items = [bounded(v, depth + 1) for v in value[:20]]
        return {'items': items, 'truncated': True, 'count': len(value)} if len(value) > 20 else items
    if isinstance(value, dict):
        result = {k: bounded(v, depth + 1) for k, v in list(value.items())[:40]}
        if len(value) > 40:
            result['_truncated_keys'] = len(value) - 40
        return result
    return value


class Steward:
    def __init__(self, root: Path, config: dict, *, state_path: Path | None = None, should_stop=None):
        if not isinstance(config, dict) or set(config) - {'packages', 'policy', 'schemas', 'attachments', 'as_of', 'timezone', 'tables', 'triage', 'duplicates', 'claims', 'graph', 'index_env'}:
            raise ValueError('Invalid stewardship configuration keys')
        if not isinstance(config.get('packages'), list) or not all(isinstance(p, str) and p in PACKAGES for p in config['packages']):
            raise ValueError('packages must explicitly select known feature names')
        if len(set(config['packages'])) != len(config['packages']):
            raise ValueError('Duplicate package selection')
        policy = config.get('policy', {})
        if not isinstance(policy, dict) or set(policy) - {'exclusions'} or not isinstance(policy.get('exclusions', []), list) or not all(isinstance(p, str) for p in policy.get('exclusions', [])):
            raise ValueError('Steward policy accepts a string-list exclusions override')
        self.config = config
        self.snapshot = Snapshot(root, Policy(exclusions=tuple(policy['exclusions'])) if 'exclusions' in policy else Policy(), should_stop)
        self.should_stop = should_stop
        self.state_path = state_path
        self._lock = threading.RLock()

    def run_once(self, *, limit: int = 50, offset: int = 0, include_details: bool = False) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError('Finding pages require limit 1..100 and nonnegative offset')
        with self._lock:
            self.snapshot.refresh()
            reports, config, scan = {}, self.config, self.snapshot
            for package in config['packages']:
                if self.should_stop and self.should_stop():
                    raise AuditCancelled()
                if package == 'properties':
                    reports[package] = properties(scan, config.get('schemas', []))
                elif package == 'attachments':
                    reports[package] = attachments(scan, **config.get('attachments', {}))
                elif package in {'tasks', 'reviews'}:
                    args = {'as_of': config.get('as_of'), 'timezone': config.get('timezone', 'UTC')}
                    reports[package] = tasks(scan, **args) if package == 'tasks' else project_reviews(scan, **args)
                elif package == 'tables':
                    reports[package] = tables(scan, config.get('tables', []))
                elif package == 'triage':
                    reports[package] = inbox_triage(scan, config.get('triage', {}))
                elif package == 'duplicates':
                    reports[package] = duplicates(scan, **config.get('duplicates', {}))
                elif package == 'claims':
                    reports[package] = contradictions(scan, config.get('claims', {}))
                elif package == 'graph':
                    reports[package] = knowledge_map(scan, **config.get('graph', {}))
                elif package == 'links':
                    index = LinkIndex(scan.root, scan.policy)
                    index.report()
                    occurrences, rows = Counter(), []
                    for r in index.results:
                        occurrences[(r['source'], r['raw'])] += 1
                        if r['status'] in {'broken', 'ambiguous', 'unsupported'} and r['source'] in scan.documents:
                            key = f'{r["raw"]}:{occurrences[(r["source"], r["raw"])]}'
                            rows.append(finding(scan.documents[r['source']], 'link.' + r['status'], key, r, line=r['line'],
                                                severity='defect' if r['status'] == 'broken' else 'review'))
                    reports[package] = scan.report(rows, diagnostics=index.diagnostics, complete=not scan.diagnostics and not index.diagnostics)
                elif package == 'index':
                    cfg = semantic.load_config(scan.root, config.get('index_env', {}))
                    if not cfg.db_path.exists():
                        reports[package] = scan.report([], index_status={'state': 'not_built'}, complete=False)
                    else:
                        conn = sqlite3.connect(cfg.db_path.resolve().as_uri() + '?mode=ro', uri=True)
                        try:
                            status = semantic.maintenance_status(conn, cfg)
                            reports[package] = scan.report([], index_status=status)
                        finally:
                            conn.close()
            all_findings = []
            if self.should_stop and self.should_stop():
                raise AuditCancelled()
            state = State(self.state_path, scan) if self.state_path else None
            try:
                for package, report in reports.items():
                    if state:
                        report['findings'] = state.reconcile(scan, report['findings'], complete=report['complete'], namespace=package)
                    else:
                        for row in report['findings']:
                            row['status'], row['namespace'] = 'open', package
                    all_findings.extend(report['findings'])
                state_counts = state.counts() if state else dict(Counter(f['status'] for f in all_findings))
            finally:
                if state:
                    state.close()
            all_findings.sort(key=lambda r: (r['namespace'], r['source'], r['line'], r['id']))
            details = {p: {k: v for k, v in r.items() if k not in {'findings', 'snapshot', 'stats'}} for p, r in reports.items()}
            if not include_details:
                details = {p: {k: len(v) if isinstance(v, list) else v for k, v in values.items()
                               if not isinstance(v, dict) or k == 'counts'} for p, values in details.items()}
            # Stable content/config/evidence snapshot; volatile queue age is excluded.
            signature = {'config': config, 'revisions': {p: d.revision for p, d in scan.documents.items()},
                         'assets': scan.assets, 'findings': all_findings, 'diagnostics': scan.diagnostics}
            return {'version': '1', 'snapshot': digest(json.dumps(signature, sort_keys=True, default=str).encode()),
                    'packages': list(reports), 'complete': not scan.diagnostics and all(r['complete'] for r in reports.values()),
                    'findings': [r if include_details else {**r, 'evidence': bounded(r['evidence'])} for r in all_findings[offset:offset + limit]], 'finding_count': len(all_findings),
                    'next_offset': offset + limit if offset + limit < len(all_findings) else None,
                    'counts': dict(Counter(f['rule'] for f in all_findings)), 'review_states': state_counts,
                    'diagnostics': scan.diagnostics, 'stats': scan.stats, 'details': details, 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description='Read-only configured vault stewardship; no model calls.')
    parser.add_argument('vault', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--details', action='store_true', help='Include full feature-specific inventory/evidence in CLI output')
    parser.add_argument('--review-id')
    parser.add_argument('--evidence-revision')
    parser.add_argument('--review-status', choices=['acknowledged', 'suppressed', 'open'])
    args = parser.parse_args()
    steward = Steward(args.vault, json.loads(args.config.read_text()), state_path=args.state)
    report = steward.run_once(limit=args.limit, offset=args.offset, include_details=args.details)
    if any([args.review_id, args.evidence_revision, args.review_status]):
        if not all([args.state, args.review_id, args.evidence_revision, args.review_status]):
            parser.error('Review requires --state, --review-id, --evidence-revision and --review-status')
        state = State(args.state, steward.snapshot)
        try:
            state.review(args.review_id, args.evidence_revision, args.review_status)
        finally:
            state.close()
        report = steward.run_once(limit=args.limit, offset=args.offset, include_details=args.details)
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
