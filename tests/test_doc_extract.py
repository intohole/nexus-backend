"""doc_extract 原语回归：文本/CSV/DOCX/XLSX 真文件 + PDF 降级链与错误契约。"""
from __future__ import annotations

import io

import pytest

from nexus.doc_extract import DocExtractError, extract_document_text


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(rows: list[list[object]], title: str = "Sheet1") -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestTextExtraction:
    def test_utf8_text(self):
        assert extract_document_text("note.txt", "你好世界".encode()) == "你好世界"

    def test_gbk_fallback(self):
        assert extract_document_text("cn.txt", "中文内容".encode("gbk")) == "中文内容"

    def test_utf8_sig_bom(self):
        assert extract_document_text("a.md", "标题".encode("utf-8-sig")) == "标题"

    def test_empty_returns_empty(self):
        assert extract_document_text("x.txt", b"") == ""

    def test_csv_pipe_join_and_cap(self):
        data = "a,b\n1,2\n\n , \n".encode()
        assert extract_document_text("t.csv", data) == "a | b\n1 | 2"

    def test_xlsx_truncated_at_cap(self):
        assert extract_document_text("t.xlsx", _xlsx_bytes([["v"]]), max_chars=3) == "[Sh"


class TestBinaryFormats:
    def test_docx_paragraphs_and_tables(self):
        from docx import Document
        doc = Document()
        doc.add_paragraph("第一段")
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "列甲"
        table.rows[0].cells[1].text = "列乙"
        buf = io.BytesIO(); doc.save(buf)
        text = extract_document_text("r.docx", buf.getvalue())
        assert "第一段" in text and "列甲 | 列乙" in text

    def test_xlsx_cells(self):
        text = extract_document_text("s.xlsx", _xlsx_bytes([["名称", "数量"], ["牛奶", 2]], "库存"))
        assert "[库存]" in text and "牛奶 | 2" in text

    def test_corrupt_docx_raises(self):
        with pytest.raises(DocExtractError):
            extract_document_text("bad.docx", b"not a docx")

    def test_pdf_degrade_chain_no_deps_raises(self):
        # 本机无 pdfplumber/PyPDF2 副本时走 pypdf；用损坏字节验证报错契约
        with pytest.raises(DocExtractError):
            extract_document_text("bad.pdf", b"%PDF-broken")

    def test_legacy_binary_rejected(self):
        with pytest.raises(DocExtractError):
            extract_document_text("old.doc", b"\xd0\xcf")


class TestPdfReal:
    def test_pdf_text_extracted(self):
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        text = extract_document_text("blank.pdf", buf.getvalue())
        assert text == ""
