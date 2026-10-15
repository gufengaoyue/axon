"""回归：任务级验收自检（v0.4.2，用户要求"检查任务有没有完成、跟任务有什么出入"）。"""
from __future__ import annotations

from axon.agent.loop import TurnResult
from axon.agent.verify import acceptance_check


def _r(products=None, effects=None):
    return TurnResult("ok", "verified", 1, 1, products=products or [],
                      side_effects=effects or [])


def _p(path):
    return {"path": str(path)}


def test_count_mismatch_flagged(cfg):
    a = cfg.project / "a.pdf"
    a.write_bytes(b"%PDF")
    ok, lines = acceptance_check("帮我找 3 篇文章并下载 PDF",
                                 _r([_p(a)]), cfg)
    assert ok is False and any("3篇" in ln and "1 项" in ln for ln in lines)


def test_count_match_passes(cfg):
    files = []
    for i in range(3):
        f = cfg.project / f"{i}.pdf"
        f.write_bytes(b"%PDF")
        files.append(_p(f))
    ok, lines = acceptance_check("下载 3 篇 PDF", _r(files), cfg)
    assert ok is True and any("✓" in ln for ln in lines)


def test_internal_unit_not_strict(cfg):
    """「5 页 PPT」产出 1 个文件 → 不应报未完成（避免误报）。"""
    f = cfg.project / "deck.pptx"
    f.write_bytes(b"PK")
    ok, lines = acceptance_check("做一份 5 页的 PPT", _r([_p(f)]), cfg)
    assert ok is True
    assert any(ln.startswith("ℹ") for ln in lines)


def test_missing_product_flagged(cfg):
    ok, lines = acceptance_check("下载 1 篇", _r([_p(cfg.project / "gone.pdf")]), cfg)
    assert ok is False and any("产物不存在" in ln for ln in lines)


def test_failed_effects_flagged(cfg):
    ok, lines = acceptance_check("改一下配置", _r(
        effects=[{"kind": "fs_write", "target": "x", "result": "blocked",
                  "verdict": "deny"}]), cfg)
    assert ok is False and any("未成功" in ln for ln in lines)


def test_requested_output_path_flagged(cfg, tmp_path):
    missing = tmp_path / "nope" / "summary.md"
    ok, lines = acceptance_check(f"结果写到 {missing}", _r(), cfg)
    assert ok is False and any("输出文件不存在" in ln for ln in lines)


def test_no_requirement_no_product(cfg):
    ok, lines = acceptance_check("你好", _r(), cfg)
    assert ok is True and lines


def test_workspace_product_noted(cfg):
    """产物仍在工作空间 → 给归档提示（不算失败）。"""
    f = cfg.workspace / "draft.md"
    f.write_text("x", encoding="utf-8")
    ok, lines = acceptance_check("写一份草稿", _r([_p(f)]), cfg)
    assert any("仍在工作空间" in ln for ln in lines)


def test_acceptance_recorded_in_journal(tmp_path):
    from axon.memory.journal import Journal
    from axon.memory.store import MemoryStore
    from axon.providers.base import Message

    class S:
        id = "s1"

    store = MemoryStore(tmp_path / "AXON.md")
    r = _r()
    r.acceptance = (False, ["⚠ 请求要求 3篇，本回合产物 1 项"])
    entry = Journal(store).record_turn(S(), r, [Message(role="user", content="t", turn=1)], [])
    assert "自检" in entry and "3篇" in entry


def test_footer_shows_acceptance(capsys):
    from rich.console import Console

    from axon.cli import _print_turn_result
    r = _r()
    r.acceptance = (False, ["⚠ 请求要求 3篇，本回合产物 2 项（可能未完成）"])
    _print_turn_result(Console(width=120), r)
    out = capsys.readouterr().out
    assert "完成度自检" in out and "3篇" in out
