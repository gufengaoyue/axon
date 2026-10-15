"""回归：下载文件名/后缀推断（v0.4.1，用户实测 arXiv 版本号被当扩展名）。"""
from __future__ import annotations

import pytest

from axon.tools.builtin.web_fetch import (_ext_of, _looks_like_ext, _safe_name,
                                          _sniff_ext)


def test_arxiv_version_number_is_not_ext():
    """`/pdf/2405.15793` 里的版本号不是扩展名 —— 应回退到 Content-Type/魔数。"""
    assert _looks_like_ext("15793") is False          # 纯数字 → 不是扩展名
    assert _looks_like_ext("pdf") is True
    assert _looks_like_ext("") is False
    assert _looks_like_ext("7473737") is False
    assert _ext_of("https://arxiv.org/pdf/2405.15793", "application/pdf",
                   'inline; filename="2405.15793v3.pdf"', b"%PDF-1.5") == "pdf"
    # 只有 URL、无头信息时靠魔数
    assert _ext_of("https://arxiv.org/pdf/2405.15793", "", "", b"%PDF-1.5") == "pdf"


def test_disposition_and_content_type_still_work():
    assert _ext_of("https://x.com/download", "application/octet-stream",
                   'attachment; filename="report.docx"', b"PK") == "docx"
    assert _ext_of("https://x.com/a", "application/pdf", "", b"") == "pdf"


def test_magic_sniff_covers_office_and_images():
    assert _sniff_ext(b"%PDF-1.7") == "pdf"
    assert _sniff_ext(b"\x89PNG\r\n\x1a\n") == "png"
    assert _sniff_ext(b"\xff\xd8\xff\xe0") == "jpg"
    assert _sniff_ext(b"GIF89a") == "gif"
    assert _sniff_ext(b"PK\x03\x04word/document.xml") == "docx"
    assert _sniff_ext(b"PK\x03\x04xl/workbook.xml") == "xlsx"
    assert _sniff_ext(b"PK\x03\x04ppt/presentation.xml") == "pptx"
    assert _sniff_ext(b"PK\x03\x04random") == "zip"
    assert _sniff_ext(b'{"a": 1}') == "json"
    assert _sniff_ext(b"<!DOCTYPE html><html>") == "html"
    assert _sniff_ext(b"") == ""


def test_url_ext_still_preferred_when_valid():
    assert _ext_of("https://x.com/paper.pdf", "", "", b"%PDF") == "pdf"
    assert _ext_of("https://x.com/photo.PNG", "", "", b"") == "png"


def test_safe_name_keeps_good_suffix():
    """用户给的 save_as 已有合理后缀 → 原样保留（这就是坏名的根因场景）。"""
    assert _safe_name("01-swe-agent-aci-2405.15793.pdf", "pdf") == \
        "01-swe-agent-aci-2405.15793.pdf"
    assert _safe_name("report.docx", "pdf") == "report.docx"      # 尊重用户命名
    assert _safe_name("keep.md", "pdf") == "keep.md"


def test_safe_name_replaces_fake_suffix():
    assert _safe_name("2405.15793", "pdf") == "2405.pdf"
    assert _safe_name("x.15793", "pdf") == "x.pdf"
    assert _safe_name("draft.7473737", "pptx") == "draft.pptx"


def test_safe_name_adds_missing_suffix():
    assert _safe_name("report", "docx") == "report.docx"
    assert _safe_name("", "pdf") == "download.pdf"


def test_safe_name_sanitizes_dangerous_chars():
    assert "/" not in _safe_name("a/b:c*d?.pdf", "pdf")
