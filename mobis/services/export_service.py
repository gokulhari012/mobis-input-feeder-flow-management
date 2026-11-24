"""CSV / Excel export of tabular data."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from mobis.utils.timeutil import fmt_datetime

#: (key, header, optional formatter)
Column = tuple[str, str, Optional[Callable[[Any], Any]]]


def _cell(value: Any, formatter: Optional[Callable[[Any], Any]]) -> Any:
    if formatter is not None:
        return formatter(value)
    if isinstance(value, datetime):
        return fmt_datetime(value)
    if value is None:
        return ""
    return value


def export_rows(path: Path, columns: list[Column], rows: Iterable[dict[str, Any]]) -> int:
    """Write rows to ``.csv`` or ``.xlsx`` (chosen by suffix).  Returns the row count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [header for _key, header, _fmt in columns]
    count = 0
    if path.suffix.lower() == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font

        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Export")
        from openpyxl.cell import WriteOnlyCell

        header_cells = []
        for header in headers:
            cell = WriteOnlyCell(sheet, value=header)
            cell.font = Font(bold=True)
            header_cells.append(cell)
        sheet.append(header_cells)
        for row in rows:
            sheet.append([_cell(row.get(key), fmt) for key, _h, fmt in columns])
            count += 1
        workbook.save(path)
        return count

    with path.open("w", newline="", encoding="utf-8-sig") as handle:  # BOM: opens cleanly in Excel
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_cell(row.get(key), fmt) for key, _h, fmt in columns])
            count += 1
    return count
