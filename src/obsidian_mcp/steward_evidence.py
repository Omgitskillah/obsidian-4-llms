"""Versioned, bounded maintenance evidence. Facts come from parsers, never chat.

The caller declares schemas/units; unsupported prose stays review-only. Complete
active task mirrors are included or preparation fails rather than dropping them.
"""
import json

from obsidian_mcp.stewardship import body_lines, digest, tasks, attachments
from obsidian_mcp.steward_tables import tables
from obsidian_mcp.steward_claims import contradictions

VERSION = 'maintenance-evidence-v1'
MAX_EVIDENCE = 32
MAX_BYTES = 24000


def prepare(snapshot, feature, paths, config):
    selected = set(paths)
    facts, statements, evidence = {}, [], []
    proposals = [{'type': 'review', 'values': {}}]
    decisions = ['uncertain', 'abstain']
    limitations = ['Suggestions require human review; no mutations or permissions are inferred.']

    def cite(path, line, classification, end=None):
        doc = snapshot.documents[path]
        lines = doc.text.splitlines()
        end = end or line
        quote = '\n'.join(lines[line - 1:end])
        if not quote.strip():
            return
        row = {'source': path, 'revision': doc.revision, 'line': line,
               'end_line': end, 'quote': quote, 'classification': classification}
        row['id'] = digest(json.dumps(row, sort_keys=True).encode())
        if row not in evidence:
            evidence.append(row)
        selected.add(path)

    if feature == 'tasks':
        report = tasks(snapshot, as_of=config.get('as_of', '1970-01-01'), timezone=config.get('timezone', 'UTC'))
        identities = {r['identity'] for r in report['tasks'] if r['source'] in selected and r['identity'] and not r['historical']}
        rows = [r for r in report['tasks'] if r['source'] in selected or (r['identity'] in identities and not r['historical'])]
        facts['occurrences'] = rows
        facts['authoritative_completion'] = None
        statements.append('No authoritative completion is inferred. Compare all active mirrors and their declared statuses.')
        for row in rows:
            cite(row['source'], row['line'], 'task_occurrence')
        limitations.append('Explicit project-scoped IDs only; historical copies are not active mirrors.')
    elif feature == 'links':
        rows = [r for r in snapshot.links().results if r['source'] in selected]
        facts['references'] = rows
        for row in rows:
            cite(row['source'], row['line'], 'live_reference')
        # Each target remains paired with the actual broken reference. Never
        # promote a path found in prose or a fenced example to a repair target.
        for row in rows:
            for target in row.get('candidates', []):
                path = target.get('path')
                if path in snapshot.documents and snapshot.documents[path].properties.get('scope') == snapshot.documents[row['source']].properties.get('scope'):
                    target_doc = snapshot.documents[path]
                    cite(path, 1, 'eligible_target', min(len(target_doc.text.splitlines()), 8))
                    proposals.append({'type': 'replacement_target', 'values': {'reference': row['finding_id'], 'target': path, 'target_revision': target_doc.revision}})
        statements.append('Parsed references and eligible targets are candidates for review, not approved repairs.')
    elif feature == 'attachments':
        report = attachments(snapshot, reference_properties=tuple(config.get('reference_properties', [])))
        paths_referenced = {r.get('path') for r in snapshot.links().results if r['source'] in selected}
        facts['assets'] = [r for r in report['inventory'] if r['path'] in paths_referenced]
        facts['deletion_eligible'] = False
        for row in snapshot.links().results:
            if row['source'] in selected and row.get('embed'):
                cite(row['source'], row['line'], 'asset_reference')
        statements.append('Deletion is not approved. Known references are present; unscanned or plugin ownership remains unknown.')
        limitations.append(report['coverage_warning'])
    elif feature == 'tables':
        report = tables(snapshot, config.get('tables', []))
        checked = [r for r in report['tables'] if r['source'] in selected]
        findings = [r for r in report['findings'] if r['source'] in selected]
        facts.update(tables=checked, findings=findings, diagnostics=[r for r in report['diagnostics'] if r.get('path') in selected])
        for table in checked:
            for row in table['rows']:
                cite(table['source'], row['line'], 'parsed_table_row')
        for row in findings:
            if row['rule'] in {'table.total', 'table.formula'}:
                item = row['evidence']
                proposals.append({'type': 'corrected_total' if row['rule'] == 'table.total' else 'corrected_formula',
                                  'values': {'finding': row['id'], 'expected': item['expected'], 'authored': item['actual']}})
                statements.append(f"{row['source']}:{row['line']}: computed {item['expected']}; authored {item['actual']}. Review the declared schema before correcting.")
        if len(proposals) > 1 and not facts['diagnostics']:
            decisions.append('supported')
        limitations.append('Only caller-declared table schemas, units and formulas are evaluated.')
    elif feature == 'claims':
        report = contradictions(snapshot, config.get('claims', {}))
        comparisons = [r for r in report['comparisons'] if r['left']['source'] in selected and r['right']['source'] in selected]
        facts['comparisons'] = comparisons
        for row in comparisons:
            for side in ('left', 'right'):
                claim = row[side]
                doc = snapshot.documents[claim['source']]
                # A YAML property may span several lines. Include the whole
                # original frontmatter, not reconstructed text masquerading as a quote.
                last = next(i for i, line in enumerate(doc.text.splitlines()[1:], 2) if line.strip() in {'---', '...'})
                cite(doc.path, 1, 'declared_claim', last)
            statements.append(f"{row['entity']} {row['field']}: {row['classification']}; values {row['left']['normalized']} and {row['right']['normalized']} in the declared base unit.")
        if comparisons and all(r['classification'] == 'equivalent' for r in comparisons):
            decisions.append('supported')
            proposals.append({'type': 'equivalence', 'values': {}})
        limitations.append('Prose claims are unclassified. Different conditions/scopes/dates never select a true source.')
    elif feature == 'graph':
        edges = [r for r in snapshot.links().results if r['source'] in selected and r['status'] == 'resolved' and r.get('path') in snapshot.documents]
        facts['edges'] = [{'source': r['source'], 'target': r['path'], 'kind': 'navigation', 'line': r['line']} for r in edges]
        for row in edges:
            cite(row['source'], row['line'], 'navigation_edge')
        facts['asserted_dependencies'] = []
        statements.append('Resolved navigation edges do not establish dependencies.')
    elif feature == 'duplicates':
        facts['exact_bytes_equal'] = len(selected) > 1 and len({snapshot.documents[p].revision for p in selected}) == 1
        if facts['exact_bytes_equal']:
            decisions.append('supported')
            proposals.append({'type': 'compare', 'values': {}})
        statements.append('Exact byte equality is recorded; no merge or deletion is approved.')
    else:
        facts['declared_properties'] = {p: snapshot.documents[p].properties for p in sorted(selected)}
        limitations.append('No prose-derived owner, next action, routing or sharing permission is established.')
    # Remaining features and prose-only claims have explicit review-only evidence.
    if not evidence:
        for path in sorted(selected):
            doc = snapshot.documents[path]
            for line, text in body_lines(doc):
                if text.strip():
                    cite(path, line, 'unclassified_prose')
            for line in sorted(set(doc.locations.values())):
                cite(path, line, 'declared_property')
    if not evidence:
        raise ValueError('No eligible evidence; preparation abstains')
    if len(selected) > 20 or len(evidence) > MAX_EVIDENCE:
        raise ValueError('Evidence bound exceeded; contrary evidence cannot be truncated')
    docs = [snapshot.documents[p] for p in selected]
    if any(d.issues for d in docs):
        raise ValueError('Invalid evidence frontmatter')
    if len(docs) > 1 and (any(not isinstance(d.properties.get('scope'), str) for d in docs)
                          or len({d.properties['scope'] for d in docs}) != 1):
        raise ValueError('Evidence crosses undeclared or differing scope boundaries')
    result = {'schema': VERSION, 'feature': feature, 'facts': facts, 'evidence': evidence,
              'allowed_proposals': proposals, 'allowed_decisions': decisions,
              'factual_statements': statements, 'limitations': limitations,
              'sources': [{'source': d.path, 'revision': d.revision} for d in sorted(docs, key=lambda d: d.path)]}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError('Evidence byte bound exceeded; preparation abstains')
    return result
