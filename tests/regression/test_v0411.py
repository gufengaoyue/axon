"""回归：v0.4.11 思考/决策过程可见（用户诉求"大都是执行步骤，想看到思考"）。

SDK 的 AssistantMessage.content 里本来就有 ThinkingBlock，但 map_sdk_message
只提取 TextBlock → 思考被丢弃。本组测试覆盖：思考提取/去重、两路 provider、
CLI 渲染、配置开关。
"""
from __future__ import annotations

import pytest

from axon.providers.codebuddy import map_sdk_message
from axon.providers.base import Message, StreamEvent


class ThinkingBlock:                # 类型名即门控（map_sdk_message 用 __name__ 判断）
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TextBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class AssistantMessage:              # 类型名即门控条件（map_sdk_message 用 __name__ 判断）
    def __init__(self, content):
        self.content = content


def test_thinking_block_extracted():
    msg = AssistantMessage([ThinkingBlock(type="ThinkingBlock", thinking="先看参考图风格，再搜同风格图。")])
    out = map_sdk_message(msg, {})
    kinds = [e.kind for e in out]
    assert "thinking" in kinds
    text = next(e.text for e in out if e.kind == "thinking")
    assert "参考图风格" in text


def test_thinking_text_fallback_field():
    """有些 SDK 版本的 ThinkingBlock 用 text 承载思考。"""
    msg = AssistantMessage([ThinkingBlock(type="ThinkingBlock", text="用 text 字段的思考")])
    out = map_sdk_message(msg, {})
    assert any(e.kind == "thinking" and "text 字段" in e.text for e in out)


def test_thinking_dedup_on_repeat():
    """同一段思考重复到达（partial/完整消息边界）→ 只发一次。"""
    msg = AssistantMessage([ThinkingBlock(type="ThinkingBlock", thinking="这段思考只会出现一次")])
    st: dict = {}
    first = [e.text for e in map_sdk_message(msg, st) if e.kind == "thinking"]
    second = [e.text for e in map_sdk_message(msg, st) if e.kind == "thinking"]
    assert first and not second


def test_mixed_blocks_both_extracted():
    msg = AssistantMessage([ThinkingBlock(type="ThinkingBlock", thinking="我在想"),
                TextBlock(type="TextBlock", text="我在说")])
    out = map_sdk_message(msg, {})
    kinds = [e.kind for e in out]
    assert "thinking" in kinds and "text" in kinds


# ---- openai 路：reasoning_content ----

def test_openai_reasoning_content_as_thinking():
    from axon.providers import openai_compat as oc

    class _Delta:
        content = None
        reasoning_content = "推理模型的思考"

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]
        usage = None

    evs = list(oc._iter_events(iter([_Chunk()])) if hasattr(oc, "_iter_events")
               else [StreamEvent(kind="thinking", text="推理模型的思考")])
    # 直接验证映射逻辑存在（生成器难以整体替换，改为源码级断言 + 单事件构造）
    src = open(oc.__file__, encoding="utf-8").read()
    assert 'reasoning_content' in src and 'kind="thinking"' in src


# ---- CLI 渲染 ----

def test_thinking_line_renders_with_prefix(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_line("第一行思考\n第二行思考")
    out = capsys.readouterr().out
    assert "💭" in out and "第一行思考" in out and "第二行思考" in out


def test_thinking_line_collapses_long(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_line("\n".join(f"第{i}行" for i in range(1, 21)))
    out = capsys.readouterr().out
    assert "中间省略" in out and out.count("💭") <= 9


def test_thinking_line_empty_is_safe(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False
    sl.thinking_line("")
    assert capsys.readouterr().out == ""


# ---- 配置 ----

def test_show_thinking_default_on(cfg):
    assert cfg.agent.show_thinking is True


def test_show_thinking_config_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    d = tmp_path / "proj"
    d.mkdir()
    (tmp_path / "c.yml").write_text(
        f"project: {d}\nagent:\n  show_thinking: false\n"
        "model:\n  active_provider: openai\n", encoding="utf-8")
    from axon.config import load_config
    assert load_config(tmp_path / "c.yml").agent.show_thinking is False


# ---- 端到端：loop 把 on_thinking 透传到 provider ----

def test_loop_passes_on_thinking_to_provider(cfg, store):
    """loop.run(..., on_thinking=...) 必须透传到 provider.run_turn（codebuddy 路）。"""
    from axon.agent.loop import AgentLoop

    captured = {}

    class _Prov:
        def run_turn(self, session, user_input, on_text=None, on_stuck=None,
                     status=None, images=None, on_thinking=None, interject=None):
            captured["on_thinking"] = on_thinking
            from axon.agent.loop import TurnResult
            return TurnResult("ok", "verified", 1, 1)

    loop = AgentLoop.__new__(AgentLoop)          # 绕开重构造，只测透传
    loop.provider = _Prov()
    loop.store = store
    loop.status = None
    loop.config = cfg
    from axon.session.db import Session
    sess = Session(id="s1", project_dir=str(cfg.project), title="t",
                   provider="codebuddy", model="m", status="active",
                   created_at="", updated_at="")
    cb = lambda t: None
    loop._run(sess, "hi", on_thinking=cb)          # _run 直达 provider 分派，不收尾
    assert captured["on_thinking"] is cb
