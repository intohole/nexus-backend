"""文档文本提取原语：按扩展名把上传文件字节解析为纯文本。

收编各业务仓手写解析器（MiaoBi document_parser / resumeAI file_parser /
lyra document_service 的同构部分）。PDF 降级链 pdfplumber→pypdf→PyPDF2：
pdfplumber 对中文版式最优，宿主未装时逐级回退；docx/pptx/xlsx 惰性导入，
缺依赖抛 DocExtractError 由业务层决定兜底话术。
"""
from __future__ import annotations

import csv
import io
from typing import Optional

_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb2312", "big5", "latin-1")
_XLSX_SHEET_ROW_CAP = 200


class DocExtractError(Exception):
    """格式支持但解析失败（损坏文件、缺依赖）。未知扩展名不抛错——按文本解码。"""


def extract_document_text(filename: str, data: bytes, *, max_chars: int = 20000, xlsx_row_cap: int = _XLSX_SHEET_ROW_CAP) -> str:
    """从上传文件字节提取文本：pdf/docx/pptx/xlsx/csv/常见文本，超集覆盖三仓方言。

    返回拼接纯文本；空内容返回 ""。max_chars 截断保护（表格类全文截断，
    纯文本类不截断由调用方自理——简历等小文本截断反而丢信息）。
    xlsx_row_cap 每表行数帽：RAG 全量摄取场景可调大。
    """
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith((".docx",)):
        return _extract_docx(data)
    if name.endswith((".pptx",)):
        return _extract_pptx(data)
    if name.endswith((".xlsx", ".xlsm")):
        return _truncate(_extract_xlsx(data, xlsx_row_cap), max_chars)
    if name.endswith(".csv"):
        return _truncate(_extract_csv(data), max_chars)
    if name.endswith((".doc", ".ppt", ".xls")):
        raise DocExtractError(f"老版二进制格式 {name.rsplit('.', 1)[-1]} 暂不支持，请另存为新版格式")
    return _decode_text(data)


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars] if max_chars and len(text) > max_chars else text


def _decode_text(data: bytes) -> str:
    for enc in _TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    buf = io.BytesIO(data)
    parts: list[str] = []
    if pdfplumber is not None:
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    reader_cls: Optional[type] = None
    try:
        from pypdf import PdfReader
        reader_cls = PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
            reader_cls = PdfReader
        except ImportError:
            raise DocExtractError("PDF解析依赖未安装（pdfplumber/pypdf 任一）")
    try:
        reader = reader_cls(buf)
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                parts.append(text.strip())
    except Exception as e:
        raise DocExtractError(f"PDF解析失败: {e}") from e
    return "\n".join(parts)


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        raise DocExtractError("Word解析依赖未安装（python-docx）")
    try:
        document = Document(io.BytesIO(data))
    except Exception as e:
        raise DocExtractError(f"DOCX解析失败: {e}") from e
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        raise DocExtractError("PPT解析依赖未安装（python-pptx）")
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as e:
        raise DocExtractError(f"PPTX解析失败: {e}") from e
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
            elif getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(data: bytes, row_cap: int) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise DocExtractError("Excel解析依赖未安装（openpyxl）")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise DocExtractError(f"XLSX解析失败: {e}") from e
    parts: list[str] = []
    for ws in wb.worksheets:
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v).strip() for v in row]
            if any(vals):
                rows.append(" | ".join(vals))
            if len(rows) >= row_cap:
                break
        if rows:
            parts.append(f"[{ws.title}]\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(parts)


def _extract_csv(data: bytes) -> str:
    text = _decode_text(data)
    try:
        rows = list(csv.reader(io.StringIO(text)))
        return "\n".join(
            " | ".join(c.strip() for c in r)
            for r in rows
            if any((c or "").strip() for c in r)
        )
    except Exception:
        return text
