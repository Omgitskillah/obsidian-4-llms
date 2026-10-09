"""Explicit structured claims: compare scope and units without choosing truth."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from itertools import combinations

from obsidian_mcp.stewardship import Snapshot, finding, valid_date


def finite(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a numerical claim')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid numerical claim') from exc
    if not result.is_finite():
        raise ValueError('Nonfinite numerical claim')
    return result


def contradictions(snapshot: Snapshot, fields: dict, *, max_pairs: int = 500) -> dict:
    if not isinstance(fields, dict) or type(max_pairs) is not int or not 1 <= max_pairs <= 5000:
        raise ValueError('Claim fields must be declared, max_pairs 1..5000')
    for name, spec in fields.items():
        if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) - {'units', 'tolerance'} or not isinstance(spec.get('units'), dict):
            raise ValueError('Fields require explicit unit factor/offset mappings')
        if finite(spec.get('tolerance', '0')) < 0:
            raise ValueError('Negative tolerance')
        for unit, mapping in spec['units'].items():
            if not isinstance(unit, str) or not isinstance(mapping, dict) or set(mapping) - {'factor', 'offset'} or 'factor' not in mapping or finite(mapping['factor']) <= 0:
                raise ValueError('Units require a positive factor and optional offset')
            finite(mapping.get('offset', '0'))
    claims, findings, by_entity = [], [], defaultdict(list)
    for doc in snapshot.documents.values():
        if doc.issues:
            continue
        raw_claims = doc.properties.get('claims', [])
        if not isinstance(raw_claims, list):
            findings.append(finding(doc, 'claim.invalid', 'claims', {'reason': 'claims_must_be_list'}, line=doc.locations.get('claims', 1), severity='defect'))
            continue
        for i, raw in enumerate(raw_claims):
            try:
                if not isinstance(raw, dict) or set(raw) - {'entity', 'field', 'value', 'unit', 'scope', 'date', 'kind', 'qualifier', 'status'}:
                    raise ValueError('Invalid claim keys')
                if any(not isinstance(raw.get(k), str) or not raw[k] for k in ('entity', 'field', 'unit', 'scope', 'date', 'kind')):
                    raise ValueError('Claims require entity/field/unit/scope/date/kind strings')
                if (not valid_date(raw['date']) or raw['kind'] not in {'measurement', 'estimate', 'specification'}
                        or not isinstance(raw.get('status', 'current'), str) or raw.get('status', 'current') not in {'current', 'superseded'}):
                    raise ValueError('Invalid claim date/kind/status')
                if not isinstance(raw.get('qualifier', ''), str):
                    raise ValueError('Qualifier must be a literal string')
                spec = fields.get(raw['field'])
                if not spec or raw['unit'] not in spec['units']:
                    raise ValueError('Undeclared field or unit')
                unit = spec['units'][raw['unit']]
                normalized = finite(raw.get('value')) * finite(unit['factor']) + finite(unit.get('offset', '0'))
                claim = {**raw, 'normalized': str(normalized), 'source': doc.path, 'revision': doc.revision,
                         'line': doc.locations.get('claims', 1), 'ordinal': i, 'status': raw.get('status', 'current')}
                claims.append(claim)
                by_entity[(raw['entity'], raw['field'])].append(claim)
            except ValueError as exc:
                findings.append(finding(doc, 'claim.invalid', str(i), {'raw': raw, 'reason': str(exc)}, line=doc.locations.get('claims', 1), severity='defect'))
    comparisons, examined, truncated = [], 0, False
    for (entity, field), rows in sorted(by_entity.items()):
        for left, right in combinations(rows, 2):
            if examined >= max_pairs:
                truncated = True
                break
            examined += 1
            if 'superseded' in {left['status'], right['status']}:
                classification = 'explicitly_superseded'
            elif left['scope'] != right['scope']:
                classification = 'different_scope'
            elif left['date'] != right['date']:
                classification = 'different_date'
            elif left['kind'] != right['kind']:
                classification = 'different_claim_kind'
            elif left.get('qualifier', '') != right.get('qualifier', ''):
                classification = 'different_condition'
            else:
                tolerance = finite(fields[field].get('tolerance', '0'))
                classification = 'equivalent' if abs(finite(left['normalized']) - finite(right['normalized'])) <= tolerance else 'conflict'
            pair = {'entity': entity, 'field': field, 'classification': classification, 'left': left, 'right': right}
            comparisons.append(pair)
            if classification == 'conflict':
                doc = snapshot.documents[left['source']]
                findings.append(finding(doc, 'claim.conflict', f'{left["ordinal"]}:{right["source"]}:{right["ordinal"]}', pair,
                                        line=left['line'], action='Review same-scope incompatible claims; neither source is selected as true'))
    return snapshot.report(findings, claims=claims, comparisons=comparisons, candidate_pairs_examined=examined,
                           comparisons_truncated=truncated, coverage_warning='Declared numerical frontmatter claims only. No prose extraction, truth selection, or silent correction.')
