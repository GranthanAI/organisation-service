"""parse(): the rules for an uploaded invite CSV."""

import pytest

from app.core.exceptions import CsvInvalid
from app.services.csv_parse import CsvRow, parse
from app.utils.csv_safe import safe_cell

MAX_BYTES, MAX_ROWS = 1_048_576, 5000


def run(text: str | bytes, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
    return parse(text.encode("utf-8") if isinstance(text, str) else text, max_bytes, max_rows)


def test_a_simple_file():
    assert run("email,name,role\na@x.edu,Ann,admin\nb@x.edu,,\n") == [
        CsvRow(2, "a@x.edu", "Ann", "admin"), CsvRow(3, "b@x.edu", None, None)]


def test_a_bom_is_ignored():
    assert run("\ufeffemail\na@x.edu\n") == [CsvRow(2, "a@x.edu", None, None)]
    assert run(b"\xef\xbb\xbfemail,name\na@x.edu,Ann\n")[0].email == "a@x.edu"


def test_crlf_and_lf_both_work_and_so_does_a_missing_final_newline():
    assert [r.email for r in run("email\r\na@x.edu\r\nb@x.edu\r\n")] == ["a@x.edu", "b@x.edu"]
    assert [r.email for r in run("email\na@x.edu\nb@x.edu")] == ["a@x.edu", "b@x.edu"]


def test_quoted_fields_with_commas_quotes_and_newlines():
    rows = run('email,name\n"a@x.edu","Smith, Ann"\nb@x.edu,"Bob ""the builder"""\nc@x.edu,"two\nlines"\nd@x.edu,Dee\n')
    assert [r.name for r in rows] == ["Smith, Ann", 'Bob "the builder"', "two\nlines", "Dee"]
    assert [r.row for r in rows] == [2, 3, 4, 5]                     # a quoted newline is still one record


def test_a_missing_email_column_is_refused():
    with pytest.raises(CsvInvalid, match="email column"):
        run("name,role\nAnn,member\n")


def test_header_names_are_trimmed_and_case_insensitive():
    assert run("  EMAIL , Name ,ROLE\na@x.edu,Ann,Admin\n") == [CsvRow(2, "a@x.edu", "Ann", "Admin")]


def test_unknown_columns_are_ignored_and_short_rows_are_fine():
    rows = run("id,email,phone,name\n1,a@x.edu,555,Ann\n2,b@x.edu\n")
    assert rows == [CsvRow(2, "a@x.edu", "Ann", None), CsvRow(3, "b@x.edu", None, None)]


def test_blank_lines_are_skipped_but_still_count_in_row_numbers():
    rows = run("email\n\na@x.edu\n   \n,\nb@x.edu\n")
    assert [(r.row, r.email) for r in rows] == [(3, "a@x.edu"), (6, "b@x.edu")]


def test_a_blank_line_before_the_header_is_tolerated():
    assert [r.row for r in run("\n\nemail\na@x.edu\n")] == [4]


def test_an_empty_or_header_only_file():
    with pytest.raises(CsvInvalid):
        run("")
    with pytest.raises(CsvInvalid):
        run("\n\n")
    assert run("email\n") == []


def test_exactly_5000_rows_pass_and_5001_do_not():
    assert len(run("email\n" + "".join(f"s{i}@x.edu\n" for i in range(5000)))) == 5000
    with pytest.raises(CsvInvalid, match="5000"):
        run("email\n" + "".join(f"s{i}@x.edu\n" for i in range(5001)))


def test_over_the_byte_limit_is_refused_with_the_limit_in_the_detail():
    with pytest.raises(CsvInvalid, match="100 bytes"):
        run("email\n" + "a" * 200 + "@x.edu\n", max_bytes=100)
    run("email\na@x.edu\n", max_bytes=len(b"email\na@x.edu\n"))          # exactly the limit is fine


def test_cp1252_bytes_are_refused_as_not_utf8():
    ansi = "email,name\na@x.edu,José\n".encode("cp1252")        # an e-acute saved as ANSI by Excel
    with pytest.raises(CsvInvalid, match="save the file as CSV UTF-8"):
        run(ansi)
    assert run("email,name\na@x.edu,José\n")[0].name == "José"   # the same name in UTF-8 is fine


def test_utf16_is_refused():
    with pytest.raises(CsvInvalid):
        run("email\na@x.edu\n".encode("utf-16"))


def test_a_huge_cell_is_csv_invalid_and_a_nul_byte_is_just_a_bad_email_later():
    assert run("email\na@x.edu\x00\n")[0].email == "a@x.edu\x00"       # no crash; the email check rejects it
    with pytest.raises(CsvInvalid):
        run("email\n" + "a" * 200_000 + "@x.edu\n")


def test_formula_cells_are_kept_as_data():
    rows = run('email,name\na@x.edu,"=HYPERLINK(""http://evil"")"\n+b@x.edu,-1\n@c,\t tab\n')
    assert rows[0].name == '=HYPERLINK("http://evil")' and rows[1].email == "+b@x.edu" and rows[1].name == "-1"
    assert rows[2].email == "@c"


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_safe_cell_prefixes_dangerous_cells(value):
    assert safe_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["plain", "a=b", "1-1", "", "x@y.edu", "'quoted"])
def test_safe_cell_leaves_other_cells_alone(value):
    assert safe_cell(value) == value


def test_safe_cell_handles_none_and_numbers():
    assert safe_cell(None) == "" and safe_cell(12) == "12"
