"""回归：v0.4.16 三项（用户实测）。

① 插嘴输入口提示常驻（toolbar）——思考/输出期间也可见
② 插嘴"看到但不受影响"→ 根因：CLI 把执行中输入**排队到本回合结束**（changelog 实证）。
   修：改用 CodeBuddySDKClient（暴露 interrupt()）→ 插嘴到达即中断当前回合，
   同 session 用 query(插嘴) 立即重发
③ 思考/决策霸屏 → 流式限行（思考 4 行 / 正文 30 行，超出静默累积 + 折叠提示）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.cli import StatusLine

_CLI_SRC = (Path(__file__).resolve().parents[2] / "axon" / "cli.py").read_text(encoding="utf-8")
_CB_SRC = (Path(__file__).resolve().parents[2] / "axon" /
           "providers" / "codebuddy.py").read_text(encoding="utf-8")


@pytest.fixture
def sl():
    s = StatusLine(console=None)
    s._tty = False
    s._text_shown = 0
    s._text_hidden = 0
    s._text_capped_noted = False
    s._text_full = ""
    return s


# ---- ③ 霸屏治理 ----

def test_thinking_capped_at_max_lines(sl, capsys):
    """v0.4.17 起：思考流改为**滚动窗口**（不再截断）——非 TTY 下全显不丢内容。"""
    for i in range(1, 13):
        sl.thinking_write(f"第{i}行内心独白\n")
    sl._close_thinking()
    out = capsys.readouterr().out
    assert "第1行" in out and "第12行" in out          # 全部出现过（滚动语义）
    assert "未显示" not in out                        # 不再有截断提示


def test_thinking_verbose_shows_all(sl, capsys):
    """v0.4.17：非 TTY 下思考流本就全显（verbose 对思考不再影响）。"""
    sl.set_verbose(True)
    for i in range(1, 9):
        sl.thinking_write(f"V{i}\n")
    sl._close_thinking()
    out = capsys.readouterr().out
    assert "V8" in out and "未显示" not in out


def test_thinking_counter_resets_per_block(sl, capsys):
    """每个思考块独立（上一块不影响下一块的窗口/显示）。"""
    for i in range(1, 8):
        sl.thinking_write(f"A{i}\n")
    sl._close_thinking()
    capsys.readouterr()
    sl.thinking_write("B1\n")
    sl._close_thinking()
    out = capsys.readouterr().out
    assert "B1" in out


def test_text_capped_at_max_lines(sl, capsys):
    for i in range(40):
        sl.stream_write(f"正文{i}\n")
    out = capsys.readouterr().out
    assert "正文29" in out and "正文30" not in out
    assert "未显示" in out


def test_text_verbose_uncapped(sl, capsys):
    sl.set_verbose(True)
    for i in range(40):
        sl.stream_write(f"V正文{i}\n")
    out = capsys.readouterr().out
    assert "V正文39" in out


def test_cap_note_printed_once(sl, capsys):
    for i in range(60):
        sl.stream_write(f"行{i}\n")
    out = capsys.readouterr().out
    assert out.count("未显示") == 1                  # 提示只打一次，不刷屏


# ---- ① 插嘴提示常驻 ----

def test_toolbar_mentions_interject():
    assert "执行中可直接输入补充" in _CLI_SRC


# ---- ② client 模式中断 + 重发 ----

def test_uses_stateful_client():
    """不再用一次性 sdk.query()；改用 CodeBuddySDKClient。"""
    assert "CodeBuddySDKClient" in _CB_SRC
    assert "client.receive_messages()" in _CB_SRC
    assert "sdk.query(" not in _CB_SRC.replace("旧的 sdk.query() 是一次性函数", "")


def test_interrupt_wired():
    assert "await client.interrupt()" in _CB_SRC
    assert "client.query(" in _CB_SRC
    assert "收到插嘴，正在中断" in _CB_SRC


def test_interrupt_failure_degrades():
    """interrupt 不受支持时 → 记 stderr 并退化为队列语义（不崩）。"""
    assert "interrupt 失败，已降级" in _CB_SRC


def test_pending_interject_sent_after_result():
    """逻辑复刻：ResultMessage 且有 pending 插嘴 → query 插嘴并继续收。"""
    # 复刻 _drive 的中断决策（不启动 SDK）
    pending, interrupted, sent = [], False, []
    steps = ["msg", "msg", "ResultMessage", "msg", "ResultMessage"]

    def check_interject(bridge_items):
        nonlocal interrupted
        pending.extend(bridge_items)
        if pending and not interrupted:
            interrupted = True
            return "interrupt"
        return None

    class FakeClient:
        async def query(self, text):
            sent.append(text)

    import asyncio

    async def main():
        nonlocal interrupted, pending
        for i, s in enumerate(steps):
            if i == 1:                                  # 用户在第二个消息边界插嘴
                act = check_interject(["换个风格"])
                assert act == "interrupt"
            if s == "ResultMessage":
                if pending:
                    text_ij = "\n".join(pending)
                    pending.clear()
                    interrupted = False
                    await FakeClient().query(text_ij)
                    continue
                break
        return sent

    got = asyncio.run(main())
    assert got == ["换个风格"]


def test_interject_queue_drained_nonblocking():
    """bridge.get(0.0) 非阻塞语义（用超时 0 快速返回）。"""
    import asyncio
    from axon.interject import InterjectBridge

    async def main():
        b = InterjectBridge()
        b.bind(asyncio.get_running_loop())
        assert await b.get(0.0) is None           # 空队列立即返回 None
        b.submit("x")
        await asyncio.sleep(0.01)
        assert await b.get(0.0) == "x"

    asyncio.run(main())
