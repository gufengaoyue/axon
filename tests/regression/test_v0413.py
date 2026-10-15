"""回归：v0.4.13 模块化输出 + 折叠 + 中文第一语言（用户参照 CodeBuddy 样式提的需求）。

模块标签：⚙ 运行 · / ✓ 完成 · / ✗ 失败 · / 💭 深度思考（块头 + 缩进）
折叠：终端无法回删已打印的行 → "打印前折叠 + 详细模式开关"（Ctrl+O / /verbose）
"""
from __future__ import annotations

import pytest

from axon.cli import StatusLine


@pytest.fixture
def sl():
    s = StatusLine(console=None)
    s._tty = False
    return s


# ---- 折叠 ----

def test_block_folds_long_content(sl):
    got = sl.block([f"第{i}行" for i in range(1, 8)])
    assert len(got) == 3
    assert "共 7 行" in got[-1] and "Ctrl+O" in got[-1]


def test_block_keeps_short_content(sl):
    got = sl.block(["a", "b", "c"])
    assert got == ["a", "b", "c"]


def test_block_verbose_shows_all(sl):
    sl.set_verbose(True)
    got = sl.block([f"第{i}行" for i in range(1, 8)])
    assert len(got) == 7


def test_set_verbose_toggle(sl):
    assert sl.verbose is False
    sl.set_verbose(True)
    assert sl.verbose is True


# ---- 模块标签 ----

def test_tool_line_has_run_label(sl, capsys):
    sl.tool("execute_shell", "ls")
    out = capsys.readouterr().out
    assert "⚙ 运行 · execute_shell" in out


def test_tool_result_ok_shows_done_label(sl, capsys):
    sl.tool_result("write_file", True, "12 行写入")
    out = capsys.readouterr().out
    assert "✓ 完成 · write_file" in out and "12 行写入" in out


def test_tool_result_fail_shows_fail_label(sl, capsys):
    sl.tool_result("read_file", False, "文件不存在")
    out = capsys.readouterr().out
    assert "✗ 失败 · read_file" in out and "文件不存在" in out


def test_thinking_block_header_and_indent(sl, capsys):
    sl.thinking_write("第一句思考。")
    sl.thinking_write("\n第二句思考。")
    sl._close_thinking()
    out = capsys.readouterr().out
    assert "💭 深度思考" in out
    # v0.4.17：滚动框（TTY）或直接全显（非 TTY），内容都可见
    assert "第一句思考。" in out and "第二句思考。" in out


def test_thinking_single_header_for_stream(sl, capsys):
    for piece in ("用户", "要", " PPT。"):
        sl.thinking_write(piece)
    sl._close_thinking()
    out = capsys.readouterr().out
    assert out.count("💭 深度思考") == 1          # 一个流只一个块头
    assert "用户要 PPT。" in out


# ---- /verbose 命令与键位 ----

def test_verbose_command_in_help():
    src = (StatusLine.__module__ and __file__)
    text = open(__file__, encoding="utf-8").read()
    cli_src = open(str(__import__("pathlib").Path(__file__).resolve().parents[2]
                       / "axon" / "cli.py"), encoding="utf-8").read()
    assert "/verbose" in cli_src
    assert 'kb.add("c-o"' in cli_src


# ---- 中文第一语言 ----

def test_prompt_chinese_first():
    from pathlib import Path
    from axon.prompts.system import build_system_prompt
    from axon.config import load_config
    cfg = load_config("config.yml")
    prompt = build_system_prompt(cfg)
    assert "简体中文" in prompt
    assert "除非用户明确指定其他语言" in prompt
