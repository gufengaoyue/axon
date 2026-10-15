"""回归：v0.4.23 插嘴"插不进"三项修复（用户实测）。

根因链：
① 打出内容后**不知道该按回车**（输入停留缓冲态，显示 `axon ➤ 插嘴: xxx` 无提示）
② 思考滚动框（rich Live）期间，提交回显的 console.print 会被吞/破坏布局
③ bridge.submit() 失败（回合刚结束）时**静默丢弃**
"""
from __future__ import annotations

from pathlib import Path

from axon.cli import StatusLine
from axon.interject import InterjectReader

_SRC = (Path(__file__).resolve().parents[2] / "axon" / "cli.py").read_text(encoding="utf-8")


def _sl():
    s = StatusLine(console=None)
    s._tty = True
    s.keyboard = InterjectReader()
    return s


# ---- ① 回车提示 ----

def test_input_line_hints_enter_when_typing():
    sl = _sl()
    for ch in "改成竖版":
        sl.keyboard.feed(ch)
    line = sl._input_line()
    assert "按回车发送" in line                      # 关键提示
    assert "改成竖版" in line


def test_input_line_default_prompt():
    sl = _sl()
    line = sl._input_line()
    assert "回车提交" in line and "按回车发送" not in line


# ---- ② Live 期间回显 ----

def test_submit_echo_shown_in_panel_during_live():
    sl = _sl()
    sl._live_active = True
    sl._thinking_open = True
    sl._scroll_lines = ["思考中…"]
    sl._pending_echo = [("💬 [插嘴] 已提交：测试", "green")]
    panel = sl._render_panel()
    assert "插嘴" in str(panel.renderable)          # 面板底部可见


def test_pending_echo_flushed_on_close(capsys):
    sl = _sl()
    sl._live_active = True
    sl._thinking_open = True
    sl._pending_echo = [("💬 [插嘴] 已提交：测试", "green")]
    sl._close_thinking()
    out = capsys.readouterr().out
    assert "已提交" in out                           # 关闭时补打
    assert sl._pending_echo == []


# ---- ③ 送达失败可见 ----

def test_submit_source_marks_committed_and_warns():
    assert "已提交（将在下一个动作前生效）" in _SRC
    assert "未能送达" in _SRC


def test_bridge_submit_false_when_unbound():
    from axon.interject import InterjectBridge
    b = InterjectBridge()
    assert b.submit("x") is False                    # 未绑定 → 上层给出警告
