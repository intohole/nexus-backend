"""file_download_response 文件下载响应原语测试。"""
from urllib.parse import unquote

from fastapi import Response

from nexus.export import _content_disposition, file_download_response


def test_ascii_filename_single_semantics():
    r = file_download_response(b"x", "export.csv", "text/csv")
    assert isinstance(r, Response)
    assert r.headers["content-type"].startswith("text/csv")
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert 'filename="export.csv"' in disp
    assert f"filename*=UTF-8''export.csv" in disp


def test_cjk_filename_double_frame():
    disp = file_download_response(b"x", "演示文稿.pptx").headers["content-disposition"]
    ascii_frame = disp.split(";")[1]
    assert 'filename=".pptx"' in ascii_frame  # 纯中文保留扩展名兜底
    encoded = disp.split("filename*=UTF-8''")[1]
    assert unquote(encoded) == "演示文稿.pptx"


def test_filename_sanitized():
    disp = _content_disposition('we"ird\nname.csv')
    assert 'filename="weirdname.csv"' in disp
    assert _content_disposition('"""', fallback="fallback").startswith("attachment; filename=\"fallback\"")
