"""Discovery of the scoping / modelling spreadsheets uploaded for an org."""

from __future__ import annotations

import os

SPREADSHEET_SUFFIX = ".xlsx"
# Office writes `~$<name>.xlsx` lock files next to an open workbook.
_LOCK_FILE_PREFIX = "~$"


def is_spreadsheet_name(file_name: str) -> bool:
    """True for a readable .xlsx workbook name (any case), not a lock or hidden file."""
    return (
        file_name.lower().endswith(SPREADSHEET_SUFFIX)
        and not file_name.startswith((_LOCK_FILE_PREFIX, "."))
    )


def find_spreadsheets(input_dir: str) -> list[str]:
    """Sorted paths of the .xlsx workbooks directly under `input_dir`."""
    if not os.path.isdir(input_dir):
        return []
    return sorted(
        os.path.join(input_dir, name)
        for name in os.listdir(input_dir)
        if is_spreadsheet_name(name) and os.path.isfile(os.path.join(input_dir, name))
    )
