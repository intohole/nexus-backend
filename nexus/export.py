"""导出工具：CSV 与 Excel 的行/模型序列化互转。"""
from __future__ import annotations

import csv
import io
from typing import Any

from nexus.logging import get_logger

logger = get_logger("nexus.export")

_CSV_HEADER_BOM = "\ufeff"


def rows_to_csv(headers: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return (_CSV_HEADER_BOM + buf.getvalue()).encode("utf-8")


def models_to_csv(columns: list[tuple[str, str]], items: list[object]) -> bytes:
    headers = [label for label, _ in columns]
    rows: list[list[Any]] = []
    for item in items:
        rows.append([getattr(item, attr, "") for _, attr in columns])
    return rows_to_csv(headers, rows)


def csv_to_rows(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    all_rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def dicts_to_excel(headers: list[tuple[str, str]], rows: list[dict[str, Any]]) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        logger.warning("openpyxl 未安装，Excel 导出降级为 CSV")
        return rows_to_csv([label for label, _ in headers], [[row.get(attr, "") for _, attr in headers] for row in rows])

    wb = Workbook()
    ws = wb.active
    ws.append([label for label, _ in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(attr, "") for _, attr in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()