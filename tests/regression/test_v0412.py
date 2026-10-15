"""回归：v0.4.12 两项（用户实测）。

① 工具执行期两条轮播（"执行 xxx…" / "等待执行结果…"）合并为一条
   `正在执行 xxx，请稍等...`（它们表达的是同一件事）
② 思考**流式**：SDK SSE 的 thinking_delta 逐字播出（此前只取整块
   AssistantMessage.ThinkingBlock，所以"一下子蹦出来"）
"""
from __future__ import annotations

from axon.providers.codebuddy import map_sdk_message


class StreamEvent:                     # 类名即 map_sdk_message 的门控
    def __init__(self, ev):
        self.event = ev


class AssistantMessage_:
    def __init__(self, content):
        self.content = content


class ThinkingBlock_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TextBlock_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _thinking_delta(text: str) -> dict:
    return {"type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text}}


def _text_delta(text: str) -> dict:
    return {"type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text}}


# ---- ② 流式思考 ----

def test_partial_thinking_streams_incrementally():
    st: dict = {}
    got: list = []
    for piece in ("用户要", " PPT。", "先看参考图", "风格。"):
        got += [e.text for e in map_sdk_message(StreamEvent(_thinking_delta(piece)), st)
                if e.kind == "thinking"]
    assert got == ["用户要", " PPT。", "先看参考图", "风格。"]


def test_partial_thinking_accumulative_form():
    """SDK 也可能给累积型（每次全量）→ 只发增量。"""
    st: dict = {}
    got: list = []
    for full in ("用户", "用户要", "用户要 PPT。"):
        got += [e.text for e in map_sdk_message(StreamEvent(_thinking_delta(full)), st)
                if e.kind == "thinking"]
    assert got == ["用户", "要", " PPT。"]


def test_full_block_after_stream_is_suppressed():
    """partial 已流完 → 完整 AssistantMessage 到达时抑制（不重复蹦一次）。"""
    st: dict = {}
    for piece in ("用户要", " PPT。"):
        list(map_sdk_message(StreamEvent(_thinking_delta(piece)), st))
    full = "用户要 PPT。"
    out = [e for e in map_sdk_message(
        AssistantMessage_([ThinkingBlock_(thinking=full)]), st) if e.kind == "thinking"]
    assert out == []


def test_thinking_and_text_interleaved():
    st: dict = {}
    out = map_sdk_message(StreamEvent(_thinking_delta("想")), st)
    out += map_sdk_message(StreamEvent(_text_delta("说")), st)
    kinds = [(e.kind, e.text) for e in out]
    assert ("thinking", "想") in kinds and ("text", "说") in kinds


# ---- CLI 增量渲染 ----

def test_thinking_write_appends_inline(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    for piece in ("用户要", " PPT。", "先看参考图", "风格。"):
        sl.thinking_write(piece)
    sl._close_thinking()
    out = capsys.readouterr().out
    assert out.count("💭") == 1                       # 一行内追加，不是每字一行
    assert "用户要 PPT。先看参考图风格。" in out


def test_thinking_write_newline_prefix(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_write("第一行\n第二行")
    sl._close_thinking()
    out = capsys.readouterr().out
    # v0.4.17 起：块头一次 + 内容直接输出（TTY 下走滚动框，非 TTY 全显）
    assert out.count("💭") == 1
    assert "第一行" in out and "第二行" in out


def test_thinking_closed_before_text(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_write("想一下")
    sl.stream_write("答复")
    out = capsys.readouterr().out
    assert "答复" in out
    assert sl._thinking_open is False


def test_thinking_closed_before_tool(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_write("想一下")
    sl.tool("execute_shell", "ls")
    out = capsys.readouterr().out
    assert "execute_shell" in out and sl._thinking_open is False


# ---- ① 状态行合并 ----

def test_tool_status_message_merged():
    """工具执行期只有一条轮播：正在执行 xxx，请稍等..."""
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.tool("web_fetch", "https://x")
    assert sl._notes == ["正在执行 web_fetch，请稍等..."]
    assert "等待执行结果" not in " ".join(sl._notes)
    assert "执行 web_fetch …" not in " ".join(sl._notes)
