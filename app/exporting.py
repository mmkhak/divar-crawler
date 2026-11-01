"""CSV (UTF-8 BOM) and RTL XLSX exports."""
from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

HEADERS = [
    "عنوان",
    "لینک",
    "محله",
    "قیمت",
    "خلاصه",
    "توضیحات",
    "شماره",
    "کار",
    "زمان",
    "توکن",
]


def listing_row(item: dict[str, Any], job_name: str = "") -> list[Any]:
    return [
        item.get("title") or "",
        item.get("link") or "",
        item.get("district") or "",
        item.get("price_text") or "",
        item.get("snippet") or "",
        item.get("description") or "",
        item.get("phone") or "",
        job_name,
        item.get("created_at") or "",
        item.get("token") or "",
    ]


def to_csv(rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx(rows: list[list[Any]], sheet_name: str = "آگهی‌ها") -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name[:31] or "آگهی‌ها"
    sheet.sheet_view.rightToLeft = True
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="right")
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = 22
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
