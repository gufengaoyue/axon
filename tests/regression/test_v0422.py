"""回归：v0.4.22 每回合用时（用户纠正：不是退出时，是每次完成任务后显示）。

页脚格式：`── 已完成（V 层验证通过）· 轮次 3 · token 1234 · 用时 2 分 35 秒`
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from axon.agent.loop import TurnResult
from axon.cli import _fmt_duration, _print_turn_result

_SRC = (Path(__file__).resolve().parents[2] / "axon" / "cli.py").read_text(encoding="utf-8")


# ---- 时长格式 ----

def test_fmt_duration_seconds():
    assert _fmt_duration(8) == "8 秒"
    assert _fmt_duration(59.9) == "59 秒"


def test_fmt_duration_minutes():
    assert _fmt_duration(65) == "1 分 5 秒"
    assert _fmt_duration(155) == "2 分 35 秒"


def test_fmt_duration_hours():
    assert _fmt_duration(3725) == "1 小时 2 分 5 秒"


# ---- 页脚显示 ----

def test_turn_result_shows_elapsed(capsys):
    r = TurnResult(final_text="ok", stop_reason="verified",
                   turns_used=3, tokens_used=1234)
    _print_turn_result(Console(width=120), r, elapsed=155)
    out = capsys.readouterr().out
    assert "用时 2 分 35 秒" in out
    assert "轮次 3" in out and "token 1234" in out


def test_turn_result_without_elapsed_still_works(capsys):
    """向后兼容：不传 elapsed（测试/旧调用）不加"用时"。"""
    r = TurnResult(final_text="ok", stop_reason="verified",
                   turns_used=1, tokens_used=0)
    _print_turn_result(Console(width=100), r)
    out = capsys.readouterr().out
    assert "用时" not in out


# ---- 接线 ----

def test_run_turn_measures_elapsed():
    assert "t_turn = time.monotonic()" in _SRC
    assert "elapsed=time.monotonic() - t_turn" in _SRC


def test_exit_time_display_removed():
    """v0.4.22：退出时的会话总时长已移除（用户要求改为每回合显示）。"""
    assert "本次会话时长" not in _SRC
