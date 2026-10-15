"""回归：v0.4.20 两项（用户实测）。

① pyproject 依赖补 `codebuddy-agent-sdk`（PyPI 0.4.19 的 requires_dist 缺失，
   pip 装完跑 codebuddy 路会报"SDK 未安装"）
② rich Live（思考滚动框）活动期间禁止裸 ANSI 重绘——否则 spinner 的
   `\\x1b[1A\\r\\x1b[2K` 与 Live 抢终端，刷出一屏 "[2Kaxon ❯ …" 字面序列
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from rich.console import Console

from axon.cli import StatusLine

_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_has_sdk_dependency():
    d = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = d["project"]["dependencies"]
    assert "codebuddy-agent-sdk>=0.3.274" in deps
    assert deps.count("codebuddy-agent-sdk>=0.3.274") == 1      # 不重复


def test_version_matches_package():
    """pyproject 与包内版本必须一致（不硬编码具体版本号）。"""
    d = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    from axon import __version__
    assert __version__ == d["project"]["version"]


# ---- Live 独占终端守卫 ----

@pytest.fixture
def rich_sl(tmp_path):
    f = open(tmp_path / "o.txt", "w", encoding="utf-8")
    s = StatusLine(console=Console(file=f, width=70, force_terminal=False, no_color=True))
    s._tty = True
    yield s
    f.close()


def test_live_active_blocks_spinner_start(rich_sl):
    rich_sl._live_active = True
    rich_sl._notes = ["x"]
    rich_sl._start()
    assert rich_sl._running is False            # Live 期间不启动转圈


def test_live_active_blocks_render(rich_sl):
    rich_sl._live_active = True
    assert rich_sl._render_once(0) == ""        # 不写任何东西


def test_live_active_set_and_cleared(rich_sl):
    rich_sl.thinking_write("想一下\n")
    assert rich_sl._live_active is True         # 开框 → 独占
    rich_sl._close_thinking()
    assert rich_sl._live_active is False        # 关框 → 释放


def test_no_live_fallback_keeps_spinner_available(rich_sl, monkeypatch):
    """Live 不可用（降级路径）→ 不设 _live_active，转圈照常。"""
    monkeypatch.setattr("rich.live.Live.__enter__",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    rich_sl.thinking_write("降级测试\n")
    assert rich_sl._live_active is False
    rich_sl._close_thinking()


def test_input_line_uses_current_prompt_symbol(rich_sl):
    line = rich_sl._input_line()
    assert "axon ❯" in line and "➤" not in line   # 与 PromptSession 提示符统一
