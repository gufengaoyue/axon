"""回归：v0.4.17 思考滚动窗口（v0.4.19 改为 rich Live 渲染）+ 强去重。

滚动窗语义：固定 SCROLL_N 行可见，新内容推进（1-6 → 2-7），历史滚出屏幕；
渲染交给 rich Live（手写 ANSI 重绘在真实终端错位——用户实测 20+ 重复框）。
"""
from __future__ import annotations

import pytest
from rich.console import Console

from axon.cli import StatusLine
from axon.providers.codebuddy import _dedup_tail_overlap


@pytest.fixture
def rich_sl(tmp_path):
    """带 rich console 的 StatusLine（输出到文件，可读回断言）。"""
    f = open(tmp_path / "out.txt", "w", encoding="utf-8")
    con = Console(file=f, width=70, force_terminal=False, no_color=True)
    s = StatusLine(console=con)
    s._tty = True
    s._out_file = f
    yield s
    try:
        f.close()
    except OSError:
        pass


def _read(sl) -> str:
    sl._out_file.flush()
    with open(sl._out_file.name, encoding="utf-8") as fh:
        return fh.read()


# ---- 滚动窗口（rich Live） ----

def test_panel_shows_frame_and_title(rich_sl):
    rich_sl.thinking_write("第一行思考\n")
    rich_sl._close_thinking()
    out = _read(rich_sl)
    assert "深度思考" in out
    assert "┌" in out or "│" in out            # rich Panel 边框


def test_panel_keeps_tail_lines(rich_sl):
    for i in range(1, 10):
        rich_sl.thinking_write(f"第{i}行\n")
    rich_sl._close_thinking()
    out = _read(rich_sl)
    assert "第9行" in out                       # 最新可见（定格保留）


def test_scroll_window_size_is_six():
    assert StatusLine.SCROLL_N == 6


def test_no_ansi_redraw_sequences(rich_sl):
    """不再手写 ANSI 光标上移（v0.4.19：那会在真实终端错位）。"""
    for i in range(1, 5):
        rich_sl.thinking_write(f"行{i}\n")
    rich_sl._close_thinking()
    out = _read(rich_sl)
    assert "\x1b[8A" not in out


def test_non_tty_shows_all_lines(capsys):
    sl = StatusLine(console=None)
    sl._tty = False
    for i in range(1, 9):
        sl.thinking_write(f"行{i}\n")
    sl._close_thinking()
    out = capsys.readouterr().out
    for i in range(1, 9):
        assert f"行{i}" in out                  # 非 TTY：不截断、全显


# ---- 强去重（整段重放） ----

def test_dedup_full_replay_from_start():
    first = ("已加好配色，文件结构正常。\n**改动说明**\n- 新增一组 10 色低饱和素雅调色板\n"
             "- 每个分类统一用该分类柔色\n刷新浏览器里的 ai-nav.html 即可看到效果。\n"
             "✅ 完成：已为页面加入素雅配色。\n⚠偏差或未完成：无。")
    out = _dedup_tail_overlap(first, first + "\n（后面还有新内容）")
    assert out.strip().startswith("（后面还有新内容）")


def test_dedup_replay_longer_than_old_window():
    seg = "这部分内容会被完整重放一遍。" * 40            # 560 字符（超旧 400 尾窗）
    out = _dedup_tail_overlap("开头内容。" + seg, seg + "尾部新内容")
    assert out == "尾部新内容"


def test_dedup_short_repeat_still_kept():
    assert _dedup_tail_overlap("你好世界", "世界你好") == "世界你好"


def test_dedup_no_overlap_untouched():
    assert _dedup_tail_overlap("抓取失败。", "换几个来源") == "换几个来源"
