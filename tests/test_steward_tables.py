from decimal import Decimal
import pytest

from obsidian_mcp.stewardship import Snapshot
from obsidian_mcp.steward_tables import number, tables


CONFIG = [{'name': 'money', 'columns': {'Item': {'type': 'string'}, 'Amount': {'type': 'number', 'required': True, 'currency': '€'}},
           'totals': [{'label_column': 'Item', 'label': 'Total', 'column': 'Amount', 'tolerance': '0.01'}]}]


def test_decimal_totals_and_examples(tmp_path):
    (tmp_path / 'a.md').write_text('| Item | Amount |\n|---|---:|\n| A | €0.10 |\n| B | €0.20 |\n| Total | €0.31 |\n'
                                   '\n```\n| Item | Amount |\n|---|---|\n| Total | €999 |\n```\n')
    scan = Snapshot(tmp_path).refresh()
    report = tables(scan, CONFIG)
    assert not report['findings']
    assert report['complete']
    (tmp_path / 'a.md').write_text('| Item | Amount |\n|---|---|\n| A | €0.10 |\n| B | €0.20 |\n| Total | €0.32 |\n')
    report = tables(scan.refresh(), CONFIG)
    assert report['counts'] == {'table.total': 1}
    assert report['findings'][0]['evidence']['expected'] == '0.30'


@pytest.mark.parametrize('raw,spec,expected', [('-1,25', {'decimal': ','}, '-1.25'), ('50%', {'unit': '%', 'percent': True}, '0.5'), ('0', {}, '0'), ('3 ms', {'unit': 'ms'}, '3')])
def test_numbers(raw, spec, expected):
    assert number(raw, spec) == Decimal(expected)


@pytest.mark.parametrize('raw', ['1,000.00', 'NaN', '__import__("os")', '1+2', ''])
def test_no_guessed_or_executed_values(raw):
    with pytest.raises(ValueError):
        number(raw, {})


def test_missing_invalid_and_na(tmp_path):
    (tmp_path / 'a.md').write_text('| Item | Amount |\n|---|---|\n| A | |\n| B | nope |\n| Total | €0 |\n')
    report = tables(Snapshot(tmp_path).refresh(), CONFIG)
    assert report['counts'] == {'table.cell': 2}
    assert not report['complete']
    assert report['diagnostics'][0]['reason'] == 'total_unresolved_inputs'


def test_escaped_pipe_and_formula(tmp_path):
    (tmp_path / 'a.md').write_text('| Name | Qty | Price | Cost |\n|---|---|---|---|\n| a\\|b | 2 | 3 | 7 |\n')
    config = [{'name': 'cost', 'columns': {'Qty': {'type': 'number'}, 'Price': {'type': 'number'}, 'Cost': {'type': 'number'}},
               'formulas': [{'left': 'Qty', 'right': 'Price', 'result': 'Cost', 'op': 'multiply'}]}]
    report = tables(Snapshot(tmp_path).refresh(), config)
    assert report['counts'] == {'table.formula': 1}
    assert report['findings'][0]['evidence']['inputs']['Name'] == 'a|b'


def test_invalid_tolerance_and_unmatched(tmp_path):
    (tmp_path / 'a.md').write_text('| x | y |\n|---|---|\n| 1 | 2 |\n')
    assert not tables(Snapshot(tmp_path).refresh(), CONFIG)['complete']
    config = [{'name': 'n', 'columns': {'x': {'type': 'number'}}, 'totals': [{'label_column': 'x', 'label': 'Total', 'column': 'x', 'tolerance': '-1'}]}]
    with pytest.raises(ValueError):
        tables(Snapshot(tmp_path).refresh(), config)


def test_malformed_rows_not_silently_padded(tmp_path):
    (tmp_path / 'a.md').write_text('| Item | Amount |\n|---|---|\n| A | €1 | surplus |\n')
    report = tables(Snapshot(tmp_path).refresh(), CONFIG)
    assert not report['tables']
    assert report['diagnostics'][0]['reason'] == 'unsupported_row_width'


def test_large_integer_formula_not_rounded(tmp_path):
    (tmp_path / 'a.md').write_text('| A | B | C |\n|---|---|---|\n| 123456789012345678901234567890 | 2 | 246913578024691357802469135780 |\n')
    config = [{'name': 'huge', 'columns': {k: {'type': 'number'} for k in ['A', 'B', 'C']},
               'formulas': [{'left': 'A', 'right': 'B', 'result': 'C', 'op': 'multiply'}]}]
    assert not tables(Snapshot(tmp_path).refresh(), config)['findings']


# ---------------------------------------------------------------- F13: refusals and per-cell findings
from obsidian_mcp.steward_tables import validate


@pytest.mark.parametrize('raw, spec, reason', [('12', {'currency': '€'}, 'currency_missing'), ('12', {'unit': 'ms'}, 'unit_missing'),
                                               ('1,000.5', {}, 'invalid_number'), ('9' * 81, {}, 'number_exceeds_80_digit_limit')])
def test_a_number_outside_its_declared_grammar_is_refused(raw, spec, reason):
    with pytest.raises(ValueError, match=reason):
        number(raw, spec)


def column(**spec):
    return [{'name': 't', 'columns': {'A': {'type': 'number', **spec}}}]


@pytest.mark.parametrize('configs, message', [
    ({}, 'table schemas must be a list'),
    (['money'], 'Invalid table schema'),
    ([{'name': 't', 'columns': {}, 'colour': 'red'}], 'Invalid table schema'),
    ([{'name': 't', 'columns': []}], 'Named table schemas require unique names and columns'),
    ([{'name': 't', 'columns': {}}, {'name': 't', 'columns': {}}], 'Named table schemas require unique names and columns'),
    ([{'name': 't', 'columns': {'A': 'number'}}], 'Invalid column'),
    (column(width=3), 'Invalid column'),
    (column(type='money'), 'Unsupported table type/decimal separator'),
    (column(decimal=';'), 'Unsupported table type/decimal separator'),
    (column(required='yes'), 'required/percent must be boolean'),
    (column(currency=1), 'currency/unit must be literal strings'),
    (column(na='-'), 'na must be literal strings'),
    ([{'name': 't', 'columns': {'A': {'type': 'number'}}, 'totals': [{'label': 'Total'}]}], 'Totals require label_column/label/column'),
    ([{'name': 't', 'columns': {'A': {'type': 'string'}}, 'totals': [{'label_column': 'A', 'label': 'T', 'column': 'A'}]}],
     'Total references invalid columns'),
    ([{'name': 't', 'columns': {'L': {'type': 'string'}, 'A': {'type': 'number'}},
       'totals': [{'label_column': 'L', 'label': 'T', 'column': 'A', 'tolerance': '-1'}]}], 'Tolerance must be finite and nonnegative'),
    ([{'name': 't', 'columns': {'A': {'type': 'number'}}, 'formulas': [{'left': 'A', 'op': 'add'}]}], 'Formula requires left/right/result/op'),
    ([{'name': 't', 'columns': {'A': {'type': 'number'}}, 'formulas': [{'left': 'A', 'right': 'A', 'result': 'A', 'op': 'divide'}]}],
     'Invalid formula fields'),
    ([{'name': 't', 'columns': {'A': {'type': 'number'}}, 'formulas': [{'left': 'A', 'right': 'A', 'result': 'A', 'op': 'add',
                                                                         'tolerance': 'NaN'}]}], 'Tolerance must be finite and nonnegative'),
    ([{'name': 't', 'columns': {'A': {'type': 'number'}}, 'unique_by': ['B']}], 'unique_by references undeclared columns'),
])
def test_a_table_schema_outside_the_known_shape_is_refused(configs, message):
    with pytest.raises(ValueError, match=message):
        validate(configs)


SCHEMA = [{'name': 'log', 'columns': {'Day': {'type': 'date', 'required': True}, 'Hours': {'type': 'number', 'na': ['n/a']}},
           'unique_by': ['Day']}]


def test_cells_are_checked_one_by_one_and_repeated_keys_are_flagged(tmp_path):
    (tmp_path / 'a.md').write_text('| Day | Hours |\n|---|---|\n| 2026-10-01 | 3 |\n| 2026-13-40 | 2 |\n|  | n/a |\n'
                                   '| 2026-10-01 | 4 |\n| 2026-10-02 | lots |\n')
    report = tables(Snapshot(tmp_path).refresh(), SCHEMA)
    reasons = sorted((f['rule'], f['evidence'].get('reason')) for f in report['findings'])
    assert reasons == [('table.cell', 'invalid_date'), ('table.cell', 'invalid_number'), ('table.cell', 'missing_required'),
                       ('table.duplicate_row', None)]


def test_a_row_whose_width_differs_from_its_header_is_left_unchecked_and_said_so(tmp_path):
    (tmp_path / 'a.md').write_text('| Day | Hours |\n|---|---|\n| 2026-10-01 | 3 | extra |\n')
    report = tables(Snapshot(tmp_path).refresh(), SCHEMA)
    assert report['findings'] == [] and {'path': 'a.md', 'line': 1, 'reason': 'unsupported_row_width'} in report['diagnostics']


def test_a_table_with_a_repeated_header_is_a_defect_and_an_empty_optional_cell_is_fine(tmp_path):
    (tmp_path / 'a.md').write_text('| Day | Day |\n|---|---|\n| 2026-10-01 | 2026-10-02 |\n\n'
                                   '| Day | Hours |\n|---|---|\n| 2026-10-03 |  |\n')
    report = tables(Snapshot(tmp_path).refresh(), SCHEMA)
    assert [f['rule'] for f in report['findings']] == ['table.duplicate_header']
