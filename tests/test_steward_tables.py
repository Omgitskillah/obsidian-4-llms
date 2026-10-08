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
