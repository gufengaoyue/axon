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


def _type(reader: InterjectReader, text: str, submit: bool = False) -> None:
    """v0.4.31 模态输入：Enter 唤起编辑态 → 逐字符 → 可选 Enter 提交。"""
    reader.feed("\r")                    # 唤起
    for ch in text:
        reader.feed(ch)
    if submit:
        reader.feed("\r")                # 确认


def _submit(reader: InterjectReader, text: str) -> None:
    _type(reader, text, submit=True)


# ---- 采集 ----

def test_type_and_submit():
    r = _reader_enabled()
    _type(r, "先别做 PPT")
    assert r.current_input() == "先别做 PPT"
    assert r.mode == "editing"          # 编辑态（未确认）
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
    r.feed("\x1b")                       # Esc 取消 → 退出编辑态
    assert r.current_input() == ""
    assert r.mode == "idle"


def test_multiple_submissions_preserved():
    """执行中可多次插嘴 → 回合结束后合并呈现。"""
    r = _reader_enabled()
    _submit(r, "补充一")
    _submit(r, "补充二")
    assert r.pop_all() == "补充一\n补充二"


def test_empty_submit_ignored():
    r = _reader_enabled()
    r.feed("\r")                        # 唤起
    r.feed("   ")                        # 空白
    r.feed("\r")                        # 确认空白 = 取消
    assert not r.has_pending() and r.mode == "idle"


def test_control_keys_not_swallowed_as_text():
    r = _reader_enabled()
    _type(r, "x")
    r.feed("\x03")                       # Ctrl+C：交给主线程（瞬断语义不变）
    r.feed("\t")                         # v0.4.31：制表符忽略（单行输入）
    r.feed("\x00"); r.feed("H")          # 扩展键前缀 → 吞掉第二字节
    assert r.current_input() == "x"
    assert "1" not in r.current_input()


def test_disabled_when_not_tty(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    r = InterjectReader()
    assert r.enabled is False
    r.start()                            # 不应起线程
    assert r._thread is None


def test_status_line_renders_interjection(monkeypatch, capsys):
    """v0.4.31：状态行只留轻提示，编辑内容走独占输入行。"""
    import io
    import sys as _s
    from axon.cli import StatusLine
    r = _reader_enabled()
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
    assert "回车插嘴" in out

    # 模态输入行：Enter 唤起 → 打字回显 → Enter 提交后清行
    buf = io.StringIO()
    old = _s.stdout
    _s.stdout = buf
    try:
        r.on_mode_change = lambda m: (sl.begin_interject_input() if m == "editing"
                                      else sl.end_interject_input())
        r.on_render = sl.update_interject_input
        _type(r, "改用 PDF 输出")
        out2 = buf.getvalue()
    finally:
        _s.stdout = old
        r.on_mode_change = None
        r.on_render = None
    assert "⌨ 插嘴> " in out2 and "改用 PDF 输出" in out2


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
    _submit(r, "加一页总结")
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
    _submit(r, "算了别做了")
    calls: list[str] = []
    monkeypatch.setattr(climod, "_run_turn",
                        lambda *a, **k: calls.append(a[2]) or None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    climod._settle_interjections({"keyboard": r, "console": None,
                                  "cfg": _cfg_stream_off()}, object())
    assert not calls                        # 丢弃 → 不注入
    assert not r.has_pending()
