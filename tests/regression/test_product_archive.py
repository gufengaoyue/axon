"""回归：产物归档到项目目录（v0.3.0，用户要求）。

工作空间 = 草稿/执行区；项目目录 = 正式产出区。
"""
from __future__ import annotations

import pytest

from axon.agent.loop import AgentLoop, TurnResult


@pytest.fixture
def loopy(cfg, store, tmp_path):
    loop = AgentLoop.__new__(AgentLoop)
    loop.store = store
    loop.config = cfg
    loop.journal = None
    return loop


def _product(path, kind="file"):
    return {"path": str(path), "size": None, "kind": kind, "sha256": None}


def test_archives_workspace_product_to_project(loopy, cfg, ws):
    src = ws / "slides" / "01.pptx"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"pptx-bytes")
    r = TurnResult("ok", "verified", 1, 1, products=[_product(src)])
    moved = loopy._archive_products(r)
    dst = cfg.project / "slides" / "01.pptx"
    assert dst.exists() and not src.exists()
    assert moved and moved[0][1] == str(dst)
    assert r.products[0]["path"] == str(dst)        # 路径已更新
    assert r.products[0]["archived"] == "moved"


def test_skips_downloads_and_tmp(loopy, cfg, ws):
    d1 = ws / "downloads" / "pic.png"
    d2 = ws / "tmp" / "scratch.txt"
    d3 = ws / ".hidden" / "x.txt"
    for d in (d1, d2, d3):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"x")
    r = TurnResult("ok", "verified", 1, 1,
                   products=[_product(d1, "download"), _product(d2), _product(d3)])
    assert loopy._archive_products(r) == []
    assert d1.exists() and d2.exists() and d3.exists()   # 原位不动


def test_same_content_dedup_removes_workspace_copy(loopy, cfg, ws):
    src = ws / "README.md"
    src.write_text("hello", encoding="utf-8")
    dst = cfg.project / "README.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("hello", encoding="utf-8")
    r = TurnResult("ok", "verified", 1, 1, products=[_product(src)])
    moved = loopy._archive_products(r)
    assert moved and not src.exists() and dst.read_text(encoding="utf-8") == "hello"
    assert r.products[0]["archived"] == "same"


def test_conflict_never_overwrites_user_file(loopy, cfg, ws):
    src = ws / "report.md"
    src.write_text("new", encoding="utf-8")
    dst = cfg.project / "report.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("user-original", encoding="utf-8")
    r = TurnResult("ok", "verified", 1, 1, products=[_product(src)])
    loopy._archive_products(r)
    assert dst.read_text(encoding="utf-8") == "user-original"      # 用户文件未被动
    assert (cfg.project / "report-2.md").read_text(encoding="utf-8") == "new"


def test_outside_workspace_untouched(loopy, cfg, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    r = TurnResult("ok", "verified", 1, 1, products=[_product(outside)])
    assert loopy._archive_products(r) == []
    assert outside.exists()


def test_disabled_by_switch(loopy, cfg, ws):
    cfg.tools.move_products_to_project = False
    src = ws / "keep_in_ws.txt"
    src.write_text("x", encoding="utf-8")
    r = TurnResult("ok", "verified", 1, 1, products=[_product(src)])
    assert loopy._archive_products(r) == []
    assert src.exists()


def test_journal_records_archive(tmp_path):
    from axon.memory.journal import Journal
    from axon.memory.store import MemoryStore
    from axon.providers.base import Message

    class S:
        id = "s1"

    store = MemoryStore(tmp_path / "AXON.md")
    r = TurnResult("ok", "verified", 1, 1)
    entry = Journal(store).record_turn(
        S(), r, [Message(role="user", content="做 PPT", turn=1)], [],
        archived=[("ws/a.pptx", "proj/a.pptx")])
    assert "归档" in entry and "a.pptx" in entry
