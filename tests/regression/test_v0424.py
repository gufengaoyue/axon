"""回归：v0.4.24 三层记忆沉淀（L0 规则式教训 / L1 触发式 / L2 退出总结）。

用户讨论确认（2026-10-16）：每回合都应沉淀，避免重复踩上回合的坑；
但有变化才值得调模型——故分三层，规则式零成本优先。
"""
from __future__ import annotations

import pytest

from axon.agent.loop import TurnResult
from axon.agent.msgmeta import pack_tool_content
from axon.memory.journal import Journal, extract_lessons
from axon.providers.base import Message, ToolCall


def _msgs(*events):
    """events: ('call', name) | ('tool', name, error_type|None)"""
    out = [Message(role="user", content="帮我抓取官网新闻")]
    for ev in events:
        if ev[0] == "call":
            out.append(Message(role="assistant", content="",
                               tool_calls=[ToolCall(id=f"t{len(out)}", name=ev[1],
                                                    arguments={})]))
        else:
            content = (pack_tool_content("502 Bad Gateway", None, ev[2])
                       if ev[2] else "ok: found")
            out.append(Message(role="tool", name=ev[1], content=content))
    return out


# ---- L0：规则式教训配对 ----

def test_lesson_switch_tool():
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", None))
    lessons = extract_lessons(msgs)
    assert lessons == ["web_fetch 失败(http_5xx) → 改用 web_search 成功"]


def test_lesson_retry_same_tool():
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "timeout"),
                 ("call", "web_fetch"), ("tool", "web_fetch", None))
    assert extract_lessons(msgs) == ["web_fetch 失败(timeout)后重试成功"]


def test_no_lesson_when_no_failure():
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", None))
    assert extract_lessons(msgs) == []


def test_no_lesson_when_failure_never_recovered():
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", "http_5xx"))
    assert extract_lessons(msgs) == []          # 没有成功点 → 不生成教训


def test_lessons_capped_at_two():
    evs = []
    for i in range(4):
        evs += [("call", f"tool{i}"), ("tool", f"tool{i}", "err"),
                ("call", "fix"), ("tool", "fix", None)]
    assert len(extract_lessons(_msgs(*evs))) == 2


def test_journal_entry_contains_lesson_line(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "AXON.md", 4000, 200)
    j = Journal(store, None)
    tr = TurnResult(final_text="done", stop_reason="verified",
                    turns_used=1, tokens_used=500)
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", None))
    entry = j.record_turn(None, tr, msgs, [], None, None)
    assert "- 教训: web_fetch 失败(http_5xx) → 改用 web_search 成功" in entry


# ---- L1：触发信号与策略 ----

def _tr(**kw):
    base = dict(final_text="", stop_reason="verified", turns_used=1, tokens_used=0)
    base.update(kw)
    return TurnResult(**base)


def test_signals_lesson():
    from axon.cli import _note_signals
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", None))
    assert "lesson" in _note_signals(None, None, msgs, _tr())


def test_signals_stuck():
    from axon.cli import _note_signals
    assert "stuck" in _note_signals(None, None, _msgs(), _tr(stop_reason="stuck_break"))


def test_signals_corrected_by_interjection():
    from axon.cli import _note_signals
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", None))
    msgs.append(Message(role="user", content="换成竖版封面"))
    assert "corrected" in _note_signals(None, None, msgs, _tr())


def test_signals_none_on_clean_turn():
    from axon.cli import _note_signals
    assert _note_signals(None, None, _msgs(), _tr()) == []


def test_signals_warn():
    from axon.cli import _note_signals
    tr = _tr()
    tr.acceptance = (False, ["⚠ 请求要求 3 篇，本回合产物 1 项"])
    assert "warn" in _note_signals(None, None, _msgs(), tr)


# ---- L1 策略门控 ----

def _app(mode):
    from types import SimpleNamespace
    return {"cfg": SimpleNamespace(agent=SimpleNamespace(auto_note=mode)),
            "console": None}


def test_mode_off_skips(monkeypatch):
    from axon import cli
    called = []
    monkeypatch.setattr(cli, "_run_turn", lambda *a, **k: called.append(1))
    cli._maybe_note_turn(_app("off"), None, _msgs(), _tr())
    assert called == []


def test_mode_trigger_skips_without_signals(monkeypatch):
    from axon import cli
    called = []
    monkeypatch.setattr(cli, "_run_turn", lambda *a, **k: called.append(1))
    cli._maybe_note_turn(_app("trigger"), None, _msgs(), _tr())
    assert called == []


def test_mode_trigger_fires_with_signal(monkeypatch):
    from axon import cli
    called = []
    monkeypatch.setattr(cli, "_run_turn", lambda *a, **k: called.append(1))
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", None))
    cli._maybe_note_turn(_app("trigger"), None, msgs, _tr())
    assert len(called) == 1


def test_mode_always_fires(monkeypatch):
    from axon import cli
    called = []
    monkeypatch.setattr(cli, "_run_turn", lambda *a, **k: called.append(1))
    cli._maybe_note_turn(_app("always"), None, _msgs(), _tr())
    assert len(called) == 1


def test_note_failure_does_not_raise(monkeypatch):
    from axon import cli

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(cli, "_run_turn", boom)
    msgs = _msgs(("call", "web_fetch"), ("tool", "web_fetch", "http_5xx"),
                 ("call", "web_search"), ("tool", "web_search", None))
    cli._maybe_note_turn(_app("trigger"), None, msgs, _tr())   # 不抛


# ---- 配置与提示词 ----

def test_config_default_trigger(cfg):
    assert cfg.agent.auto_note == "trigger"


def test_config_parse_always(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (tmp_path / "c.yml").write_text(
        "agent:\n  auto_note: always\nmodel:\n  active_provider: openai\n",
        encoding="utf-8")
    from axon.config import load_config
    assert load_config(tmp_path / "c.yml").agent.auto_note == "always"


def test_prompt_teaches_lesson_consumption():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] /
           "axon" / "prompts" / "system.py").read_text(encoding="utf-8")
    assert "教训消费" in src and "禁止重复已失败的路径" in src
