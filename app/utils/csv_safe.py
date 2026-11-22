"""Spreadsheet formula-injection guard for CSV files we write.

A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return can be run as a formula by Excel or Sheets, so it
gets a leading apostrophe (the spreadsheet then shows it as text). Every cell of every CSV this service writes goes
through `safe_cell`.
"""

DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(DANGEROUS_PREFIXES) else text
