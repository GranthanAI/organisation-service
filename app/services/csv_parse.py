"""Parsing an uploaded invite CSV (pure: bytes in, rows out; no database).

Rules: at most `max_bytes` and `max_rows` data rows (422 CSV_INVALID, the limit in the detail); UTF-8 (a BOM is fine) and
nothing else ("save the file as CSV UTF-8"); comma delimiter; the first row is the header; header names are trimmed and
case-insensitive: `email` (required), `name`, `role`; other columns are ignored; blank lines are skipped. CRLF and LF both
work and quoted fields are the csv module's business. A cell that looks like a formula is kept as data (it is only
neutralised when WE write a CSV, see utils/csv_safe.py).

Row numbers are spreadsheet row numbers: the header is row 1, and blank lines count.
"""

import csv
import io
from dataclasses import dataclass

from app.core.exceptions import CsvInvalid


@dataclass(frozen=True)
class CsvRow:
    row: int
    email: str
    name: str | None
    role: str | None


def parse(data: bytes, max_bytes: int, max_rows: int) -> list[CsvRow]:
    if len(data) > max_bytes:
        raise CsvInvalid(f"the file is larger than {max_bytes} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvInvalid("save the file as CSV UTF-8") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",")
    rows: list[CsvRow] = []
    header: list[str] | None = None
    email_at = name_at = role_at = -1
    try:
        for number, cells in enumerate(reader, start=1):
            if not any(c.strip() for c in cells):
                continue                                       # a blank line (before the header, or between rows)
            if header is None:
                header = [c.strip().lower() for c in cells]
                if "email" not in header:
                    raise CsvInvalid("the first row must be a header with an email column")
                email_at = header.index("email")
                name_at = header.index("name") if "name" in header else -1
                role_at = header.index("role") if "role" in header else -1
                continue
            if len(rows) >= max_rows:
                raise CsvInvalid(f"the file has more than {max_rows} rows")
            rows.append(CsvRow(
                row=number,
                email=_cell(cells, email_at),
                name=(_cell(cells, name_at) or None) if name_at >= 0 else None,
                role=(_cell(cells, role_at) or None) if role_at >= 0 else None))
    except csv.Error as exc:
        raise CsvInvalid("the file is not valid CSV") from exc
    if header is None:
        raise CsvInvalid("the file is empty: the first row must be a header with an email column")
    return rows


def _cell(cells: list[str], at: int) -> str:
    return cells[at].strip() if 0 <= at < len(cells) else ""
