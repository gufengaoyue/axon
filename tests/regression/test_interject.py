"""回归：插嘴功能（v0.2.6，用户需求）。

约定：执行中采集（非阻塞）→ 回合结束后确认 → 包装 <user_interjection> 注入新一轮。
"""
from __future__ import annotations

import pytest

from axon.interject import InterjectReader, wrap_interjection


class _CfgStub:
    """关闭 stream_interject 的配置桩（验证旧路径：回合后弹确认）。"""

    class agent:
        stream_interject = False


def _cfg_stream_off():
    return _CfgStub()


def _reader_enabled() -> InterjectReader:
    r = InterjectReader()
    r._enabled = True          # 单测强制启用（CI 无 TTY）
    return r


def _type(reader: InterjectReader, text: str) -> None:
    for ch in text:
        reader.feed(ch)


# ---- 采集 ----

def test_type_and_submit():
    r = _reader_enabled()
    _type(r, "先别做 PPT")
    assert r.current_input() == "先别做 PPT"
    assert not r.has_pending()          # 未回车 → 尚未提交
    r.feed("\r")
    assert r.has_pending()
    assert r.pop_all() == "先别做 PPT"
    assert r.current_input() == ""
    assert not r.has_pending()


def test_backspace_and_esc():
    r = _reader_enabled()
    _type(r, "abc")
    r.feed("\x08")                       # Backspace
    assert r.current_input() == "ab"
    r.feed("\x1b")                       # Esc 清空
    assert r.current_input() == ""


def test_multiple_submissions_preserved():
    """执行中可多次插嘴 → 回合结束后合并呈现。"""
    r = _reader_enabled()
    _type(r, "补充一")
    r.feed("\r")
    _type(r, "补充二")
    r.feed("\r")
    assert r.pop_all() == "补充一\n补充二"


def test_empty_submit_ignored():
    r = _reader_enabled()
    r.feed("\r")
    assert not r.has_pending()


def test_control_keys_not_swallowed_as_text():
    r = _reader_enabled()
    _type(r, "x")
    r.feed("\x03")                       # Ctrl+C：交给主线程（瞬断语义不变）
    r.feed("\t")
    assert r.current_input() == "x\t" or r.current_input() == "x"   # 控制符不进文本
    assert "1" not in r.current_input()


def test_disabled_when_not_tty(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    r = InterjectReader()
    assert r.enabled is False
    r.start()                            # 不应起线程
    assert r._thread is None


class _FakeStatus:
    """验证状态行渲染包含插嘴回显。"""


def test_status_line_renders_interjection(monkeypatch, capsys):
    from axon.cli import StatusLine
    r = _reader_enabled()
    _type(r, "改用 PDF 输出")
    sl = StatusLine(console=None)
    sl.keyboard = r
    sl._tty = True
    sl._notes = ["思考中"]
    sl._msg = "思考中"
    sl._start()
    import time as _t
    _t.sleep(0.25)
    sl._stop()
    out = capsys.readouterr().out
    assert "插嘴" in out and "改用 PDF 输出" in out


# ---- 注入 ----

def test_wrap_interjection_contract():
    wrapped = wrap_interjection("改用 16:9 尺寸")
    assert wrapped.startswith("<user_interjection>") and wrapped.endswith("</user_interjection>")
    assert "调整决策" in wrapped or "新增需求" in wrapped
    assert "改用 16:9 尺寸" in wrapped


def test_settle_confirm_injects_new_turn(monkeypatch):
    """确认后应以 <user_interjection> 起新一轮。"""
    from axon import cli as climod
    r = _reader_enabled()
    _type(r, "加一页总结")
    r.feed("\r")
    calls: list[str] = []

    def fake_run_turn(app, session, text, on_stuck):
        calls.append(text)
        from axon.agent.loop import TurnResult
        return TurnResult("ok", "verified", 1, 1)

    monkeypatch.setattr(climod, "_run_turn", fake_run_turn)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    app = {"keyboard": r, "console": None, "cfg": _cfg_stream_off()}
    climod._settle_interjections(app, object())
    assert calls and calls[0].startswith("<user_interjection>")
    assert "加一页总结" in calls[0]


def test_settle_discard_on_no(monkeypatch):
    from axon import cli as climod
    r = _reader_enabled()
    _type(r, "算了别做了")
    r.feed("\r")
    calls: list[str] = []
    monkeypatch.setattr(climod, "_run_turn",
                        lambda *a, **k: calls.append(a[2]) or None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    climod._settle_interjections({"keyboard": r, "console": None,
                                  "cfg": _cfg_stream_off()}, object())
    assert not calls                        # 丢弃 → 不注入
    assert not r.has_pending()
