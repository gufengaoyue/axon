"""回归：状态行渲染与异常结束补沉淀（v0.2.9，用户实测）。"""
from __future__ import annotations

import pytest

from axon.providers.base import Message


def _reader_with(text: str):
    from axon.interject import InterjectReader
    r = InterjectReader()
    r._enabled = True
    for ch in text:
        r.feed(ch)
    return r


# ---- 残影修复：写短行必须擦掉上一行长出的部分 ----

def test_write_erases_previous_longer_line(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl._write("执行 execute_shell … (46s)")     # 长行
    sl._write("等待执行结果… (47s)")             # 短行 → 必须补空格擦尾
    out = capsys.readouterr().out
    last = out.split("\r")[-1]
    assert last.startswith("等待执行结果… (47s)")
    assert last.endswith(" ")                    # 有擦除填充
    assert len(last) >= len("执行 execute_shell … (46s)")


def test_write_same_length_no_padding(capsys):
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl._write("abc")
    sl._write("def")
    out = capsys.readouterr().out
    assert out.split("\r")[-1] == "def"          # 等长不补空格（无痕）


# ---- 常驻输入行 ----

def test_input_line_placeholder_and_interject():
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl.keyboard = _reader_with("")
    assert "axon" in sl._input_line()
    assert "补充" in sl._input_line()            # 空闲时显示占位说明
    sl.keyboard = _reader_with("改用竖版封面")
    line = sl._input_line()
    assert "axon" in line and "插嘴" in line and "改用竖版封面" in line


def test_two_line_render_keeps_prompt_visible(capsys):
    """双行模式：状态行 + 常驻输入行；插嘴内容出现在输入行（确定性渲染）。"""
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl.keyboard = _reader_with("加一页总结")
    sl._two_line = True
    sl._notes = ["执行 execute_shell …"]
    rendered = sl._render_once(0, el=46.0)
    out = capsys.readouterr().out
    assert "执行 execute_shell … (46s)" in rendered
    assert "axon" in rendered and "加一页总结" in rendered
    assert "\x1b[2K" in out                     # 每行都清行尾（防残影）


def test_single_line_fallback(capsys):
    """VT 不可用 → 单行回退：插嘴挂在状态行尾。"""
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl.keyboard = _reader_with("补充")
    sl._two_line = False
    sl._notes = ["思考中"]
    rendered = sl._render_once(0, el=3.0)
    assert "思考中 (3s)" in rendered and "插嘴" in rendered and "补充" in rendered


# ---- 会话生命周期 / 异常结束补沉淀 ----

def test_session_status_roundtrip(store, session):
    assert store.get_session(session.id).status == "active"
    store.set_status(session.id, "ended")
    assert store.get_session(session.id).status == "ended"


def test_list_stale_sessions_filters(store):
    old = store.create_session("D:/p", "openai", "m")
    store.conn.execute("UPDATE sessions SET updated_at='2020-01-01T00:00:00.000+00:00'"
                       " WHERE id=?", (old,))
    store.conn.commit()
    stale = store.list_stale_sessions("D:/p", "2026-01-01T00:00:00.000+00:00")
    assert [s.id for s in stale] == [old]
    # 标记 ended 后不再是 stale
    store.set_status(old, "ended")
    assert store.list_stale_sessions("D:/p", "2026-01-01T00:00:00.000+00:00") == []


def test_mark_ended_on_normal_exit(cfg, store):
    from axon.cli import _mark_ended
    sid = store.create_session(str(cfg.project), "openai", "m")
    _mark_ended({"store": store}, store.get_session(sid))
    assert store.get_session(sid).status == "ended"


def test_recover_stale_promotes_and_archives(cfg, store, monkeypatch, capsys):
    """启动检测：异常结束的会话 → 询问补沉淀；拒绝则仅归档为 ended（不再重复打扰）。"""
    from axon.cli import _recover_stale_sessions
    sid = store.create_session(str(cfg.project), "openai", "m")
    for i in range(6):
        store.append_message(sid, Message(role="user", content=f"任务 {i}"))
    store.conn.execute("UPDATE sessions SET updated_at='2020-01-01T00:00:00.000+00:00'"
                       " WHERE id=?", (sid,))
    store.conn.commit()
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")   # 不补沉淀
    app = {"cfg": cfg, "store": store, "console": None}
    _recover_stale_sessions(app, None)
    assert store.get_session(sid).status == "ended"


def test_recover_skips_short_sessions(cfg, store, monkeypatch):
    """消息太少 → 不打扰用户，直接归档。"""
    from axon.cli import _recover_stale_sessions
    sid = store.create_session(str(cfg.project), "openai", "m")
    store.append_message(sid, Message(role="user", content="hi"))
    store.conn.execute("UPDATE sessions SET updated_at='2020-01-01T00:00:00.000+00:00'"
                       " WHERE id=?", (sid,))
    store.conn.commit()
    called: list = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: called.append(1) or "n")
    _recover_stale_sessions({"cfg": cfg, "store": store, "console": None}, None)
    assert store.get_session(sid).status == "ended"
    assert not called                                  # 未打扰用户


def test_recover_disabled_by_config(cfg, store, monkeypatch):
    from axon.cli import _recover_stale_sessions
    cfg.memory.recover_prompt = False
    sid = store.create_session(str(cfg.project), "openai", "m")
    for i in range(6):
        store.append_message(sid, Message(role="user", content=f"t{i}"))
    store.conn.execute("UPDATE sessions SET updated_at='2020-01-01T00:00:00.000+00:00'"
                       " WHERE id=?", (sid,))
    store.conn.commit()
    _recover_stale_sessions({"cfg": cfg, "store": store, "console": None}, None)
    assert store.get_session(sid).status == "active"     # 开关关闭 → 不动


def test_replay_history_fallback_hint(capsys, store, session):
    """旧会话无提问文本 → 明确提示，而不是让用户困惑。"""
    from axon.cli import _replay_history
    store.append_message(session.id, Message(role="assistant", content="已沉淀项目记忆",
                                             turn=1))
    _replay_history({"console": None, "store": store}, session)
    out = capsys.readouterr().out
    assert "未记录提问文本" in out or "axon ❯" in out


# ---- v0.3.0：转圈持续 / 剪贴板显式 / 插嘴可见 / 副作用折叠 / 内部消息过滤 ----

def test_spinner_keeps_running_after_successful_tool_result():
    """连续同类工具期间转圈不能停（原 bug：成功结果停转圈 + 合并分支不重启）。"""
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = False                      # 不真起线程，只看状态机
    sl.tool("execute_shell", "ls")
    sl._running = True
    sl._evt.set()
    sl.tool_result("execute_shell", ok=True)          # 成功 → 不停
    assert sl._running is True
    sl.tool_result("execute_shell", ok=False, detail="boom")
    assert sl._running is False                       # 失败 → 停


def test_same_tool_merge_restarts_spinner():
    from axon.cli import StatusLine
    sl = StatusLine(console=None)
    sl._tty = True
    sl._running = False
    sl._last_tool = "execute_shell"
    sl._last_tool_count = 3
    sl._start = lambda: setattr(sl, "started", True)   # 打桩
    sl.started = False
    sl.tool("execute_shell", "again")
    assert sl.started is True                          # 合并分支必须重启


def test_clipboard_not_auto_read_by_default(cfg):
    assert cfg.tools.clipboard_auto_attach is False


def test_is_internal_message_filters_summary_prompt():
    from axon.session.resume import is_internal_message
    assert is_internal_message("请为本次会话沉淀「项目记忆」——目的是让后续会话…")
    assert is_internal_message("<resume_context>\nsession: x\n</resume_context>")
    assert is_internal_message("<interrupted_task>\nstatus: 未完成\n</interrupted_task>")
    assert not is_internal_message("帮我做一份 agent harness 的 PPT")


def test_extract_task_state_skips_summary_prompt():
    """last_user_request 不能被"沉淀项目记忆"指令污染（用户实测）。"""
    from axon.providers.base import Message
    from axon.session.resume import extract_task_state
    msgs = [
        Message(role="user", content="帮我做一份 agent harness 的 PPT", turn=1),
        Message(role="assistant", content="好的，我先检索资料。", turn=2),
        Message(role="user", content="请为本次会话沉淀「项目记忆」——目的是…", turn=3),
        Message(role="assistant", content="已沉淀项目记忆", turn=4),
    ]
    req, unfinished = extract_task_state(msgs)
    assert req == "帮我做一份 agent harness 的 PPT"
    assert unfinished is False


def test_fold_side_effects_compacts_shell_commands():
    """几十条 python -c 记录 → 压平/截断/同类聚合，不再刷屏。"""
    from axon.session.resume import fold_side_effects
    effects = [{"kind": "shell", "target": f'python -c "import os; print({i})" '
                                           f'{"x" * 200}',
                "verdict": "allow", "result": "ok"} for i in range(30)]
    effects += [{"kind": "fs_write", "target": "/ws/README.md",
                 "verdict": "allow", "result": "ok"}]
    lines = fold_side_effects(effects)
    assert len(lines) <= 24                     # 有上限
    assert any("共 30 条" in ln for ln in lines)  # 聚合提示
    assert all(len(ln) <= 140 for ln in lines)    # 单行截断
    assert any("README.md" in ln for ln in lines)


def test_build_resume_message_uses_folded_effects():
    from axon.providers.base import Message  # noqa: F401
    from axon.session.db import Session
    from axon.session.resume import build_resume_message
    sess = Session(id="s1", project_dir="D:/p", title="", provider="openai",
                   model="m", status="active",
                   created_at="2026-01-01T00:00:00.000+00:00",
                   updated_at="2026-01-01T00:00:00.000+00:00")
    eff = [{"kind": "shell", "target": "python -c " + "y" * 300,
            "verdict": "allow", "result": "ok"} for _ in range(10)]
    note = build_resume_message(sess, eff, [], "做 PPT", True)
    body = note.content
    assert "共 10 条" in body and "task_status: 未完成" in body
    assert max(len(ln) for ln in body.splitlines()) <= 220
