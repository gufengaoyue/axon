"""回归：插嘴模态输入（v0.4.31，用户需求）。

背景：旧实现"可打印字符持续累积、回车即提交"——
  ① 思考（rich Live）期间没有入口（回显挂在 spinner 状态行尾缀，而 Live 期间
     状态行不渲染）；
  ② 入口出现后，thinking() 重启转圈会清行/重排，视觉上入口消失、焦点丢失。

新语义（用户拍板）：
  - 执行期（含思考）按 **Enter** 唤起 `⌨ 插嘴> ` 独占输入行（与思考/执行隔离）；
  - 输入中 **Enter** 确认提交；**空白**确认视为取消；**Esc** 取消；
  - 编辑期间工具结果照常**打印到输入行上方**（Live 活跃时改为排队，退出编辑后补打）；
  - 思考 Live 期间 Panel **冻结不刷新**，输入行始终是**独立行**（v0.4.32：
    并入 Panel 会在 Panel 定格式时留下幽灵行，用户截图实测）。
"""
from __future__ import annotations

import io
import sys

import pytest

from axon.interject import InterjectReader


@pytest.fixture
def reader():
    r = InterjectReader()
    r._enabled = True
    return r


# ============================================================ 输入状态机

def test_idle_discards_text(reader):
    """IDLE 下可打印字符一律丢弃（只有 Enter 能唤起）——防误输入。"""
    for ch in "改一下":
        reader.feed(ch)
    assert reader.mode == "idle" and reader.current_input() == ""


def test_enter_opens_editor(reader):
    events: list[str] = []
    reader.on_mode_change = lambda m: events.append(m)
    reader.feed("\r")
    assert reader.mode == "editing"
    assert events == ["editing"]


def test_typing_echoes_and_backspace(reader):
    seen: list[str] = []
    reader.on_render = lambda t: seen.append(t)
    reader.feed("\r")
    for ch in "改用B":
        reader.feed(ch)
    assert reader.current_input() == "改用B"
    assert seen[-1] == "改用B"
    reader.feed("\x08")
    assert reader.current_input() == "改用"
    assert seen[-1] == "改用"


def test_enter_confirms_and_submits(reader):
    got: list[str] = []
    reader.on_submit = got.append
    reader.feed("\r")
    for ch in "先别做 PPT":
        reader.feed(ch)
    reader.feed("\r")
    assert got == ["先别做 PPT"]
    assert reader.mode == "idle" and reader.current_input() == ""


def test_blank_confirm_is_cancel(reader):
    got: list[str] = []
    reader.on_submit = got.append
    reader.feed("\r")
    for ch in "   ":
        reader.feed(ch)
    reader.feed("\r")
    assert got == []
    assert reader.mode == "idle"


def test_esc_cancels(reader):
    got: list[str] = []
    reader.on_submit = got.append
    reader.feed("\r")
    for ch in "算了":
        reader.feed(ch)
    reader.feed("\x1b")
    assert got == [] and reader.current_input() == "" and reader.mode == "idle"


def test_extended_keys_swallowed(reader):
    """方向键等扩展键（\\x00/\\xe0 前缀 + 第二字节）不得落成正文。"""
    reader.feed("\r")
    for ch in "ab":
        reader.feed(ch)
    reader.feed("\xe0"); reader.feed("H")     # ↑
    reader.feed("\x00"); reader.feed("K")     # ←
    assert reader.current_input() == "ab"


def test_ctrl_c_not_swallowed(reader):
    reader.feed("\r")
    reader.feed("x")
    reader.feed("\x03")                       # 交主线程（中断语义不变）
    assert reader.mode == "editing" and reader.current_input() == "x"


def test_queue_mode_without_on_submit(reader):
    reader.feed("\r")
    reader.feed("q")
    reader.feed("\r")
    assert reader.queue == ["q"] and reader.has_pending()


def test_stop_exits_editing_but_keeps_buffer(reader):
    """回合结束：mode 回 IDLE（渲染收尾），buffer 保留供"未提交"提示。"""
    reader.feed("\r")
    reader.feed("没来得及提交")
    reader.stop()
    assert reader.mode == "idle"
    assert reader.current_input() == "没来得及提交"


def test_start_clears_previous_buffer(reader):
    reader.feed("\r")
    reader.feed("上回合残留")
    reader._thread = None
    reader.start()                            # 新回合
    assert reader.current_input() == ""


# ============================================================ 渲染层

def _sl():
    from axon.cli import StatusLine
    s = StatusLine(console=None)
    s._tty = True
    s._running = True
    return s


def test_input_line_begin_update_end():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        sl = _sl()
        sl.begin_interject_input()
        sl.update_interject_input("补充一句")
        sl.end_interject_input()
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "⌨ 插嘴> " in out and "补充一句" in out
    # 结束时要清行（擦除 + 回到行首）
    assert out.rstrip().endswith("\r") or "\r" in out


def test_print_goes_above_input_line():
    """编辑态下工具结果打印在输入行上方，且打印后输入行被重绘。"""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        sl = _sl()
        sl.begin_interject_input()
        sl.update_interject_input("正在打字")
        sl._print("✓ 完成 · read_file")
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "✓ 完成 · read_file" in out
    # print 之后必须有输入行重绘（说明焦点回到编辑行）
    tail = out.split("✓ 完成 · read_file", 1)[1]
    assert "⌨ 插嘴> 正在打字" in tail


def test_render_panel_does_not_embed_input_line_when_live():
    """v0.4.32：输入行**不**并入 Panel。

    旧实现把输入行追加到 Panel body——Panel 是非 transient 的，关闭时内容
    定格 → 输入行永久留在屏幕（用户截图实测的幽灵行）。
    """
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._scroll_lines = ["思考中…"]
    sl._interject_editing = True
    sl._interject_text = "边想边补"
    panel = sl._render_panel()
    assert panel.height == sl.SCROLL_N + 2
    assert "⌨ 插嘴> 边想边补" not in str(panel.renderable)


def test_live_print_is_deferred_and_does_not_resume_spinner():
    """v0.4.32 回归：Live + 编辑态打印必须排队，且不得重置 `_render_paused`。

    旧实现条件 `_interject_editing and not _live_active` → Live 期间的打印
    走普通分支，`finally: _resume_render()` 把 paused 置 False → spinner
    恢复写帧，与输入行抢同一行（用户截图：状态行尾部拼着输入内容）。
    """
    import io
    import sys as _s
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl._running = True
    sl._live_active = True
    buf = io.StringIO()
    old = _s.stdout
    _s.stdout = buf
    try:
        sl.begin_interject_input()
        sl._print("✓ 完成 · x")
        phase1 = buf.getvalue()
        paused, n_deferred = sl._render_paused, len(sl._deferred)
        sl.end_interject_input()
        pending = list(sl._pending_echo or [])
    finally:
        _s.stdout = old
    assert "✓ 完成" not in phase1          # 编辑期间不上屏
    assert paused is True and n_deferred == 1
    assert any("✓ 完成" in m for m, _ in pending)   # 转面板队列，关闭时补打


def test_live_editing_shows_independent_input_line():
    """v0.4.32：Live 期间输入行仍是独立行（旧实现走冻结的 redraw → 不回显）。"""
    import io
    import sys as _s
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl._live_active = True
    buf = io.StringIO()
    old = _s.stdout
    _s.stdout = buf
    try:
        sl.begin_interject_input()
        sl.update_interject_input("思考中补充")
        out = buf.getvalue()
    finally:
        _s.stdout = old
    assert "⌨ 插嘴> 思考中补充" in out


def test_scroll_redraw_frozen_while_editing():
    """编辑期间 Panel 不刷新（防覆盖输入行）。"""
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    calls = []

    class _Live:
        def update(self, *a, **k):
            calls.append(1)
    sl._live = _Live()
    sl._interject_editing = True
    sl._scroll_redraw()
    assert calls == []


def test_status_line_hint_mentions_enter():
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl.keyboard = InterjectReader()
    sl._notes = ["思考中"]
    frame = sl._render_once(0, el=1.0)
    assert "回车插嘴" in frame


def test_full_flow_reader_plus_render():
    """端到端：Enter 唤起 → 打字 → Enter 确认 → 输出含提交内容与恢复提示。"""
    from axon.cli import StatusLine
    buf = io.StringIO()
    old = sys.stdout
    submitted: list[str] = []
    r = InterjectReader(on_submit=submitted.append)
    r._enabled = True
    sl = StatusLine(console=None)
    sl._tty = True
    sl._running = True
    sl.keyboard = r
    r.on_mode_change = lambda m: (sl.begin_interject_input() if m == "editing"
                                  else sl.end_interject_input())
    r.on_render = sl.update_interject_input
    sys.stdout = buf
    try:
        r.feed("\r")
        for ch in "改用 PDF":
            r.feed(ch)
        r.feed("\r")
    finally:
        sys.stdout = old
        r.on_mode_change = None
        r.on_render = None
    out = buf.getvalue()
    assert submitted == ["改用 PDF"]
    assert "⌨ 插嘴> 改用 PDF" in out
    assert sl._interject_editing is False


def test_run_turn_teardown_clears_editor(tmp_path, cfg, store, audit, session):
    """回合 finally 强制退出编辑态（防输入行残留到下一回合）。"""
    from axon import cli as climod
    from axon.agent.loop import TurnResult
    from axon.cli import StatusLine

    st = StatusLine(console=None)
    st._tty = False                    # 非 TTY：所有渲染为空操作

    class _Loop:
        status = st
        def run(self, *a, **k):
            return TurnResult("ok", "verified", 1, 1)

    class _Cfg:
        class agent:
            show_thinking = False
            stream_interject = True
    app = {"cfg": _Cfg(), "console": None, "loop": _Loop(),
           "keyboard": InterjectReader(), "interject_bridge": None,
           "store": store, "gateway": None}
    app["keyboard"]._enabled = False   # 不起键盘线程（无 TTY）
    # 模拟"回合结束时仍在编辑态"
    st._interject_editing = True
    climod._run_turn(app, session, "x", None)
    assert st._interject_editing is False
