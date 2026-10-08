"""Declared Markdown tables and Decimal checks; never executes formulas."""
from decimal import Decimal, InvalidOperation
import fnmatch
import re

from markdown_it import MarkdownIt

from obsidian_mcp.stewardship import Snapshot, finding, valid_date
from obsidian_mcp.link_integrity import _comments


def number(raw: str, spec: dict) -> Decimal:
    text = raw.strip()
    suffix = spec.get('unit', '')
    prefix = spec.get('currency', '')
    if prefix:
        if not text.startswith(prefix):
            raise ValueError('currency_missing')
        text = text[len(prefix):].strip()
    if suffix:
        if not text.endswith(suffix):
            raise ValueError('unit_missing')
        text = text[:-len(suffix)].strip()
    separator = spec.get('decimal', '.')
    # No guessed thousands separators. Values must use the declared grammar.
    pattern = r'[+-]?\d+(?:' + re.escape(separator) + r'\d+)?'
    if not re.fullmatch(pattern, text):
        raise ValueError('invalid_number')
    value = Decimal(text.replace(separator, '.'))
    return value / 100 if spec.get('percent') else value


def validate(configs):
    if not isinstance(configs, list):
        raise ValueError('table schemas must be a list')
    names = set()
    for config in configs:
        if not isinstance(config, dict) or set(config) - {'name', 'glob', 'columns', 'totals', 'formulas', 'unique_by'}:
            raise ValueError('Invalid table schema')
        if not isinstance(config.get('name'), str) or config['name'] in names or not isinstance(config.get('columns'), dict):
            raise ValueError('Named table schemas require unique names and columns')
        names.add(config['name'])
        for name, spec in config['columns'].items():
            if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) - {'type', 'required', 'currency', 'unit', 'decimal', 'percent', 'na'}:
                raise ValueError('Invalid column')
            if spec.get('type') not in {'number', 'date', 'string'} or spec.get('decimal', '.') not in {'.', ','}:
                raise ValueError('Unsupported table type/decimal separator')
            if any(k in spec and not isinstance(spec[k], bool) for k in ('required', 'percent')):
                raise ValueError('required/percent must be boolean')
            if any(k in spec and not isinstance(spec[k], str) for k in ('currency', 'unit')):
                raise ValueError('currency/unit must be literal strings')
            if not isinstance(spec.get('na', []), list) or not all(isinstance(x, str) for x in spec.get('na', [])):
                raise ValueError('na must be literal strings')
        for total in config.get('totals', []):
            if not isinstance(total, dict) or set(total) - {'label_column', 'label', 'column', 'tolerance'} or not {'label_column', 'label', 'column'} <= set(total):
                raise ValueError('Totals require label_column/label/column')
            if total['label_column'] not in config['columns'] or config['columns'].get(total['column'], {}).get('type') != 'number':
                raise ValueError('Total references invalid columns')
            tolerance = Decimal(str(total.get('tolerance', '0')))
            if not tolerance.is_finite() or tolerance < 0:
                raise ValueError('Tolerance must be finite and nonnegative')
        for formula in config.get('formulas', []):
            if not isinstance(formula, dict) or set(formula) - {'left', 'right', 'result', 'op', 'tolerance'} or not {'left', 'right', 'result', 'op'} <= set(formula):
                raise ValueError('Formula requires left/right/result/op')
            if formula['op'] not in {'add', 'multiply'} or any(config['columns'].get(formula[k], {}).get('type') != 'number' for k in ('left', 'right', 'result')):
                raise ValueError('Invalid formula fields')
            tolerance = Decimal(str(formula.get('tolerance', '0')))
            if not tolerance.is_finite() or tolerance < 0:
                raise ValueError('Tolerance must be finite and nonnegative')
        if any(k not in config['columns'] for k in config.get('unique_by', [])):
            raise ValueError('unique_by references undeclared columns')


def extract(doc):
    tokens = MarkdownIt('commonmark').enable('table').parse(_comments(doc.body))
    tables, table, row = [], None, None
    for token in tokens:
        if token.type == 'table_open':
            table = {'line': token.map[0] + 1, 'rows': []}
        elif token.type == 'tr_open' and table is not None:
            row = {'line': token.map[0] + 1 if token.map else table['line'], 'cells': []}
        elif token.type == 'inline' and row is not None:
            row['cells'].append(token.content)
        elif token.type == 'tr_close' and row is not None:
            table['rows'].append(row)
            row = None
        elif token.type == 'table_close':
            tables.append(table)
            table = None
    return tables


def row_width(line):
    cells, escaped = [''], False
    for character in line.strip():
        if character == '|' and not escaped:
            cells.append('')
        else:
            cells[-1] += character
        escaped = character == '\\' and not escaped
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return len(cells)


def tables(snapshot: Snapshot, configs: list[dict]) -> dict:
    validate(configs)
    findings, checked, diagnostics = [], [], list(snapshot.diagnostics)
    for doc in snapshot.documents.values():
        for index, table in enumerate(extract(doc)):
            headers = table['rows'][0]['cells']
            authored_lines = doc.body.splitlines()
            if any(row_width(authored_lines[r['line'] - 1]) != len(headers) for r in table['rows'][1:]):
                diagnostics.append({'path': doc.path, 'line': table['line'], 'reason': 'unsupported_row_width'})
                continue
            if len(set(headers)) != len(headers):
                findings.append(finding(doc, 'table.duplicate_header', str(index), headers, line=table['line'], severity='defect'))
                continue
            matches = [c for c in configs if fnmatch.fnmatchcase(doc.path, c.get('glob', '*.md')) and set(c['columns']) <= set(headers)]
            if not matches:
                diagnostics.append({'path': doc.path, 'line': table['line'], 'reason': 'table_without_matching_schema'})
                continue
            for config in matches:
                parsed, seen = [], set()
                for row_index, row in enumerate(table['rows'][1:]):
                    raw = dict(zip(headers, row['cells']))
                    values = {}
                    for name, spec in config['columns'].items():
                        value = raw.get(name, '')
                        if value in spec.get('na', []):
                            values[name] = None
                            continue
                        try:
                            if not value:
                                if spec.get('required'):
                                    raise ValueError('missing_required')
                                values[name] = None
                            elif spec['type'] == 'number':
                                values[name] = number(value, spec)
                            elif spec['type'] == 'date':
                                if not valid_date(value):
                                    raise ValueError('invalid_date')
                                values[name] = value
                            else:
                                values[name] = value
                        except (ValueError, InvalidOperation) as exc:
                            values[name] = None
                            findings.append(finding(doc, 'table.cell', f'{config["name"]}:{index}:{row_index}:{name}',
                                                    {'column': name, 'raw': value, 'reason': str(exc)}, line=row['line'], severity='defect'))
                    if config.get('unique_by'):
                        key = tuple(raw.get(k) for k in config['unique_by'])
                        if key in seen:
                            findings.append(finding(doc, 'table.duplicate_row', f'{index}:{row_index}', raw, line=row['line']))
                        seen.add(key)
                    for formula in config.get('formulas', []):
                        a, b, actual = (values.get(formula[k]) for k in ('left', 'right', 'result'))
                        if all(isinstance(v, Decimal) for v in (a, b, actual)):
                            expected = a + b if formula['op'] == 'add' else a * b
                            if abs(actual - expected) > Decimal(str(formula.get('tolerance', '0'))):
                                findings.append(finding(doc, 'table.formula', f'{index}:{row_index}:{formula["result"]}',
                                                        {'formula': formula, 'actual': str(actual), 'expected': str(expected), 'inputs': raw}, line=row['line'], severity='defect'))
                    parsed.append({'line': row['line'], 'raw': raw, 'values': values})
                for total in config.get('totals', []):
                    totals = [r for r in parsed if r['raw'].get(total['label_column']) == total['label']]
                    inputs = [r for r in parsed if r not in totals]
                    values = [r['values'].get(total['column']) for r in inputs]
                    if len(totals) != 1 or not all(isinstance(v, Decimal) for v in values) or not isinstance(totals[0]['values'].get(total['column']), Decimal):
                        diagnostics.append({'path': doc.path, 'line': table['line'], 'reason': 'total_unresolved_inputs', 'schema': config['name']})
                        continue
                    expected = sum(values, Decimal(0))
                    actual = totals[0]['values'][total['column']]
                    if abs(actual - expected) > Decimal(str(total.get('tolerance', '0'))):
                        findings.append(finding(doc, 'table.total', f'{config["name"]}:{index}:{total["column"]}',
                                                {'expected': str(expected), 'actual': str(actual), 'inputs': [{'line': r['line'], 'raw': r['raw']} for r in inputs], 'rule': total},
                                                line=totals[0]['line'], severity='defect'))
                checked.append({'source': doc.path, 'revision': doc.revision, 'line': table['line'], 'schema': config['name'],
                                'rows': [{**r, 'values': {k: str(v) if isinstance(v, Decimal) else v for k, v in r['values'].items()}} for r in parsed]})
    return snapshot.report(findings, tables=checked, diagnostics=diagnostics, complete=not diagnostics)
