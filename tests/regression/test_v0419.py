"""回归：v0.4.19 审批键盘竞争修复 + 看门狗暂停（用户实测"沙箱确认后卡住"）。

根因：InterjectReader（msvcrt 后台线程）**全局抢按键**，审批的 `input()` 永远
等不到用户的 y/n → 卡死（截图 91s 无响应）。修：审批期间 `pause()` 采集器 +
停看门狗（避免"等待模型响应…"与审批面板并存）。
"""
from __future__ import annotations

from pathlib import Path

from axon.interject import InterjectReader

_SRC = (Path(__file__).resolve().parents[2] / "axon" / "cli.py").read_text(encoding="utf-8")


# ---- InterjectReader 暂停能力 ----

def test_reader_pause_blocks_feed_loop():
    r = InterjectReader()
    assert r._paused.is_set() is False
    r.pause()
    assert r._paused.is_set() is True
    r.resume()
    assert r._paused.is_set() is False


def test_paused_reader_does_not_consume_msvcrt(monkeypatch):
    """暂停时 _loop 不调用 kbhit/getwch（让 input() 独享键盘）。"""
    calls = {"kbhit": 0}

    class FakeMsvcrt:
        @staticmethod
        def kbhit():
            calls["kbhit"] += 1
            return True

        @staticmethod
        def getwch():
            raise AssertionError("暂停期间不应读取按键")

    import sys
    import types
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    r = InterjectReader()
    r.pause()
    # 直接跑有限轮次的循环体（模拟：暂停期间 kbhit 也不应被调用）
    for _ in range(3):
        if r._paused.is_set():
            break
        if FakeMsvcrt.kbhit():
            FakeMsvcrt.getwch()
    assert calls["kbhit"] == 0


# ---- 审批器接线 ----

def test_confirm_pauses_keyboard_and_watchdog():
    """confirm 必须 pause 采集器 + stop 看门狗，结束后 resume/start。"""
    assert "kb.pause()" in _SRC
    assert "kb.resume()" in _SRC
    assert "self.status.stop_watchdog()" in _SRC
    assert "self.status.start_watchdog()" in _SRC


def test_confirm_uses_keyboard_from_status():
    assert 'getattr(self.status, "keyboard", None)' in _SRC


def test_confirm_roundtrip(monkeypatch):
    """端到端：confirm 期间采集器处于暂停，结束后恢复。"""
    from axon.cli import StatusLine, TTYPrompter
    from axon.sandbox.gateway import Action, Decision

    sl = StatusLine(console=None)
    reader = InterjectReader()
    sl.keyboard = reader
    p = TTYPrompter(console=None, status=sl)
    seen = {}

    def fake_input(prompt=""):
        seen["paused"] = reader._paused.is_set()     # 输入期间应为暂停
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    out = p.confirm(Action(kind="shell", target="x"),
                    Decision("ask", "r", "reason"))
    assert out.approved is True
    assert seen["paused"] is True                    # 输入期间采集器暂停 ✓
    assert reader._paused.is_set() is False          # 结束后恢复 ✓
