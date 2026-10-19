"""回归：v0.4.28 两项（用户实测 + 用户讨论定案）。

① 执行结束后状态行残留"正在执行 X, 请稍等... (0s)"（截图：两行重复、
   停住不消失，操作其实早已完成）。根因：双行状态行（v0.2.9）的 "\n" 占位
   在 spinner 重启时**重复执行**（text() 停 → tool() 再起）→ 光标行偏移 →
   `\x1b[1A` 清错行 → 旧帧永久残留。修复：双行**默认关闭**（底部工具栏已
   常驻等价提示）+ 占位 primed 守卫（开回时也不再重复）。

② 思考内容只保留可见尾部，用户要求"保留现显示方式同时能回顾全部"
   （收起/展开）。方案选型：终端内交互展开在 Windows 终端不可靠
   （v0.4.17/0.4.19 教训）且与插嘴键盘采集冲突 → 采用**落盘 + /think**：
   思考全文存档 <程序数据目录>/thinking/<会话>.log，/think [n|all] 回顾。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest
from rich.console import Console

from axon.cli import StatusLine, _cmd_think, _think_log_path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = (_ROOT / "axon" / "cli.py").read_text(encoding="utf-8")


@pytest.fixture
def rich_sl(tmp_path):
    f = open(tmp_path / "o.txt", "w", encoding="utf-8")
    s = StatusLine(console=Console(file=f, width=70, force_terminal=False, no_color=True))
    s._tty = True
    yield s
    f.close()


@pytest.fixture
def sl_str():
    """StringIO 控制台：可读回打印内容（存档提示行断言用）。"""
    s = StatusLine(console=Console(file=io.StringIO(), width=70,
                                   force_terminal=False, no_color=True))
    s._tty = True
    return s


# ============================================================ ① 双行默认关 + 占位修复

def test_two_line_default_off(rich_sl):
    """默认单行——杜绝占位/上移错位残留（用户实测）。"""
    assert rich_sl.two_line_status is False
    assert rich_sl._two_line is False          # _start 前即为 False


def test_two_line_config_can_reenable(rich_sl):
    rich_sl.two_line_status = True
    assert rich_sl._two_line is False          # _start 前仍是 False（启动时才置位）


def test_single_line_mode_no_ansi_sequences(monkeypatch, rich_sl):
    """默认单行：无 "\n" 占位、无 \x1b[ 光标序列（只有 \r 同行重写）——
    这正是消除"残行停住"类 bug 的关键（占位/上移机制整个不存在）。"""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rich_sl.thinking("x")
    time.sleep(0.15)
    rich_sl._stop()
    out = buf.getvalue()
    assert "\r" in out
    assert "\n" not in out and "\x1b[" not in out


def test_two_line_primes_once_per_start(monkeypatch, rich_sl):
    """开回双行时：每次启动恰好一次占位；运行中重复 _start() 是 no-op
    （锁定语义，防未来重构出现"单次启动重复占位"回归）。"""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr("axon.cli._VT_OK", True)   # pytest 下 stdout 非控制台
    rich_sl.two_line_status = True
    rich_sl._tty = True
    rich_sl.thinking("x")
    time.sleep(0.15)
    rich_sl._stop()
    assert buf.getvalue().count("\n\x1b[1A") == 1        # 一次 start = 一次占位
    rich_sl.thinking("y")                                 # 重启 → 再一次
    time.sleep(0.15)
    rich_sl._start()                                      # 运行中 → no-op
    time.sleep(0.15)
    rich_sl._stop()
    assert buf.getvalue().count("\n\x1b[1A") == 2        # 共两次，no-op 不新增


def test_tool_result_success_keeps_spinner(rich_sl):
    """v0.3.0 设计回归：工具成功不停转圈（✓ 行打出后由下一个 thinking() 接管
    状态行）——修复残行不该改这个语义。"""
    rich_sl.tool("execute_shell", "ls")
    assert rich_sl._running is True
    rich_sl.tool_result("execute_shell", True, "a.txt")
    assert rich_sl._running is True            # 成功 → 不停
    rich_sl._stop()


def test_interject_tail_has_enter_hint_in_single_line(rich_sl):
    """单行模式的插嘴尾缀（v0.4.31 起为"回车插嘴"）。

    v0.4.23 的"← 按回车发送"随内容显示，但内容每帧重绘 → 闪烁/失焦；
    v0.4.31 改为模态输入：状态行只留"回车插嘴"唤起提示，编辑内容走独占输入行。
    """
    class _KB:
        def current_input(self):
            return "补充内容"
    rich_sl.keyboard = _KB()
    frame = rich_sl._render_once(0)
    assert "回车插嘴" in frame


# ============================================================ ② 思考存档 + /think

def test_thinking_full_accumulates(rich_sl):
    """thinking_write 始终累积全文（滚动窗只显尾部，全文供存档）。"""
    rich_sl.thinking_write("第一段思考\n")
    rich_sl.thinking_write("第二段思考\n")
    assert "第一段思考" in rich_sl._thinking_full
    assert "第二段思考" in rich_sl._thinking_full


def test_close_thinking_archives_to_log(sl_str, monkeypatch, tmp_path):
    """关思考框 → 全文落盘 <program_root>/thinking/<sid8>.log 并提示。"""
    monkeypatch.setattr("axon.config.program_root", lambda: tmp_path)
    sl_str.session_id = "abcdef1234567890"
    sl_str.thinking_write("完整思考内容一\n")
    sl_str.thinking_write("完整思考内容二\n")
    sl_str._close_thinking()
    p = tmp_path / "thinking" / "abcdef12.log"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "完整思考内容一" in text and "完整思考内容二" in text
    assert "思考 #1" in text
    # 提示行（每回合一次）
    out = sl_str.console.file.getvalue()
    assert "/think 可回顾" in out
    # 关框后清空累积（下一段重新计）
    assert sl_str._thinking_full == ""


def test_archive_note_once_per_turn(sl_str, monkeypatch, tmp_path):
    """同一回合多次开合思考框，存档提示只打一次（降噪）。"""
    monkeypatch.setattr("axon.config.program_root", lambda: tmp_path)
    sl_str.session_id = "s1"
    sl_str.thinking_write("甲\n")
    sl_str._close_thinking()
    sl_str.thinking_write("乙\n")
    sl_str._close_thinking()
    out = sl_str.console.file.getvalue()
    assert out.count("/think 可回顾") == 1


def test_cmd_think_reads_rounds(rich_sl, monkeypatch, tmp_path):
    """/think 默认显最近 1 段；n=最近 n 段；all=全部。"""
    monkeypatch.setattr("axon.config.program_root", lambda: tmp_path)
    rich_sl.session_id = "sess-1"
    rich_sl.thinking_write("第一回合的思考\n")
    rich_sl._close_thinking()
    rich_sl._think_note_shown = False          # 模拟新回合
    rich_sl.thinking_write("第二回合的思考\n")
    rich_sl._close_thinking()

    class _S:
        id = "sess-1"
    lines: list[str] = []

    def out(text, style=""):
        lines.append(text)

    _cmd_think({"loop": type("L", (), {"status": rich_sl})()}, _S(), "", out)
    assert "共 2 段" in lines[0]
    assert any("第二回合的思考" in ln for ln in lines)
    assert not any("第一回合的思考" in ln for ln in lines)   # 默认只显最近 1 段

    lines.clear()
    _cmd_think({"loop": type("L", (), {"status": rich_sl})()}, _S(), "2", out)
    assert any("第一回合的思考" in ln for ln in lines)


def test_cmd_think_no_log(rich_sl, monkeypatch, tmp_path):
    monkeypatch.setattr("axon.config.program_root", lambda: tmp_path)
    lines: list[str] = []
    _cmd_think({"loop": type("L", (), {"status": rich_sl})()},
               type("S", (), {"id": "none"})(), "", lambda t, style="": lines.append(t))
    assert lines and "还没有思考存档" in lines[0]


def test_think_log_path_uses_session_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr("axon.config.program_root", lambda: tmp_path)
    p = _think_log_path(type("S", (), {"id": "0123456789abcdef"})())
    assert p == tmp_path / "thinking" / "01234567.log"


# ============================================================ 接线检查

def test_wiring():
    assert "self._two_line = bool(_VT_OK) and self.two_line_status" in _SRC
    assert "_two_line_primed" in _SRC
    assert "_archive_thinking" in _SRC
    assert '"/think"' in _SRC or "/think" in _SRC
    assert "two_line_status" in _SRC
