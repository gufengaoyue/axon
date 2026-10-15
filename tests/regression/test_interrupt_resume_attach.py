"""回归：中断续做 + 历史重放 + 附件（v0.2.5 用户实测三需求）。"""
from __future__ import annotations

import pytest

from axon.providers.base import Message, ToolCall


# ---- 需求 3：中断后"继续未完成任务" ----

def test_interrupt_persists_unfinished_marker(store, session, cfg):
    """Ctrl+C 中断 → 落库 <interrupted_task>（含原始请求），供 resume 续做。"""
    from axon.agent.loop import AgentLoop, TurnResult
    loop = AgentLoop.__new__(AgentLoop)      # 只测收尾逻辑，不建完整依赖
    loop.store = store
    loop.journal = None
    loop.config = cfg
    r = TurnResult("", "user_interrupt", 3, 100, note="用户中断（Ctrl+C）")
    loop._persist_unfinished(session, r, "生成一份 agent harness 的 PPT")
    msgs = store.load_messages(session.id)
    marker = [m for m in msgs if m.role == "user"
              and str(m.content).startswith("<interrupted_task>")]
    assert marker, "应写入未完成标记"
    assert "生成一份 agent harness 的 PPT" in marker[0].content
    assert "未完成" in marker[0].content


def test_interrupt_marker_not_written_on_success(store, session, cfg):
    from axon.agent.loop import AgentLoop, TurnResult
    loop = AgentLoop.__new__(AgentLoop)
    loop.store = store
    loop.journal = None
    loop.config = cfg
    loop._persist_unfinished(session, TurnResult("ok", "verified", 1, 10), "任务A")
    msgs = store.load_messages(session.id)
    assert not [m for m in msgs if str(m.content or "").startswith("<interrupted_task>")]


def test_resume_context_carries_task_and_status(store, session):
    """resume 上下文必须含最近请求 + 未完成状态（否则"继续"无从下手）。"""
    from axon.session.resume import build_resume_message, extract_task_state
    msgs = [
        Message(role="user", content="做 PPT", turn=1),
        Message(role="assistant", content=None, turn=2,
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={})]),
        Message(role="user", content=(
            "<interrupted_task>\nstatus: 未完成（用户中断（Ctrl+C））\n"
            "original_request: 做 PPT\n</interrupted_task>"), turn=3),
    ]
    req, unfinished = extract_task_state(msgs)
    assert req == "做 PPT" and unfinished is True
    note = build_resume_message(session, [], [], req, unfinished)
    assert "last_user_request: 做 PPT" in note.content
    assert "task_status: 未完成" in note.content


def test_extract_task_state_without_reply_is_unfinished(store):
    """最后一条请求之后没有 assistant 文本 → 判为未完成。"""
    from axon.session.resume import extract_task_state
    msgs = [Message(role="user", content="帮我重构模块", turn=1),
            Message(role="assistant", content=None, turn=2,
                    tool_calls=[ToolCall(id="c1", name="read_file", arguments={})])]
    req, unfinished = extract_task_state(msgs)
    assert req == "帮我重构模块" and unfinished is True


def test_extract_task_state_replied_is_finished(store):
    from axon.session.resume import extract_task_state
    msgs = [Message(role="user", content="查一下版本", turn=1),
            Message(role="assistant", content="Python 是 3.11.8", turn=2)]
    req, unfinished = extract_task_state(msgs)
    assert req == "查一下版本" and unfinished is False


# ---- 需求 2：assistant 文本落库 + 历史重放 ----

def test_codebuddy_mirrors_assistant_prose(cfg, audit, store, session):
    """codebuddy 路：叙述文本随工具调用落库（原实现只落 tool_calls）。"""
    import axon.providers.codebuddy as cb
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    runner = cb.CodebuddyRunner.__new__(cb.CodebuddyRunner)
    runner.store = store
    runner.gateway = gw
    runner._tool_names = {}
    runner._pending_text = "我先看一下环境，然后开始检索。"
    tc = ToolCall(id="c1", name="mcp__axon__execute_shell", arguments={"command": "ls"})
    runner._mirror_tool_use(session, tc, set())
    msgs = store.load_messages(session.id)
    assert any("我先看一下环境" in (m.content or "") for m in msgs), \
        "assistant 叙述应落库"
    # 尾部文本 flush
    runner._pending_text = "收尾说明。"
    runner._flush_pending_text(session)
    assert any((m.content or "") == "收尾说明。" for m in store.load_messages(session.id))


def test_replay_history_shows_user_and_assistant(capsys, cfg, store, session):
    from axon.cli import _replay_history
    store.append_message(session.id, Message(role="user", content="第一问：做PPT", turn=1))
    store.append_message(session.id, Message(role="assistant", content="好的，我先看环境。",
                                             turn=2))
    app = {"console": None, "store": store}
    _replay_history(app, session)
    out = capsys.readouterr().out
    assert "第一问：做PPT" in out and "好的，我先看环境。" in out


# ---- 需求 1：附件（粘贴图片 + @引用） ----

def test_attach_copy_and_reference(tmp_path):
    from axon import attachments
    ws = tmp_path / "ws"
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG fake")
    copied = attachments.copy_files(ws, [src])
    assert copied and copied[0].name == "图片1.png"
    assert (ws / "tmp" / "attach" / "图片1.png").exists()
    # @引用解析
    text, refs = attachments.parse_input("看看 @图片1.png 的内容", ws)
    assert any(r.name == "图片1.png" for r in refs)
    # 清单
    note = attachments.build_attachment_note(refs)
    assert "图片1.png" in note and str(ws) in note


def test_attach_path_in_input_rewritten(tmp_path):
    """输入里粘贴的绝对路径 → 收编为 @名称（避免超长路径进提示）。"""
    from axon import attachments
    ws = tmp_path / "ws"
    src = tmp_path / "Screenshot 2026.png"
    src.write_bytes(b"\x89PNG")
    text, refs = attachments.parse_input(f"参考这张图 {src}", ws)
    assert "@图片1.png" in text and str(src) not in text
    assert len(refs) == 1


def test_attach_dedup_names(tmp_path):
    from axon import attachments
    ws = tmp_path / "ws"
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    names = [p.name for p in attachments.copy_files(ws, [a, b])]
    assert names == ["图片1.png", "图片2.png"]


def test_attach_dir_not_cleaned(cfg, ws, monkeypatch, tmp_path):
    """env_cleanup 必须保护 tmp/attach（否则回合结束附件就没了）。"""
    from axon.env_cleanup import cleanup_temp
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    attach = ws / "tmp" / "attach"
    attach.mkdir(parents=True)
    (attach / "图片1.png").write_bytes(b"png")
    (ws / "junk.tmp").write_text("x", encoding="utf-8")
    cleanup_temp(cfg)
    assert (attach / "图片1.png").exists(), "附件不能被清理"
    assert not (ws / "junk.tmp").exists()


# ---- v0.3.1：只有"主动粘贴"才收编剪贴板 ----

def test_clipboard_changed_detection(monkeypatch):
    from axon import attachments
    seq = {"n": 10}
    monkeypatch.setattr(attachments, "clipboard_sequence", lambda: seq["n"])
    assert attachments.clipboard_changed(10) is False    # 没动过 → 不收编
    seq["n"] = 11
    assert attachments.clipboard_changed(10) is True     # 动过（刚截图/复制）→ 收编
    assert attachments.clipboard_changed(-1) is False    # 平台不支持 → 不收编


def test_clipboard_sequence_never_raises():
    from axon.attachments import clipboard_sequence
    assert isinstance(clipboard_sequence(), int)


# ---- v0.4.1：工具短名错误自愈 ----

def test_hint_tool_names_persists_correction(cfg, audit, store, session):
    """SDK 报短名 → 落库 <tool_name_hint>（含正确全名），供下一轮模型改正。"""
    import axon.providers.codebuddy as cb
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                        AutoApprovePrompter())
    runner = cb.CodebuddyRunner.__new__(cb.CodebuddyRunner)
    runner.store = store
    runner.gateway = gw
    runner._tool_names = {}
    runner._pending_text = ""
    runner._hint_tool_names(
        session, "ErrorExecution: Tool 'execute_shell' not found in the current tool set.")
    msgs = store.load_messages(session.id)
    hints = [m for m in msgs if str(m.content or "").startswith("<tool_name_hint>")]
    assert hints, "应落库纠正提示"
    assert "mcp__axon__execute_shell" in hints[0].content
    # 无该错误 → 不打扰
    n0 = len(store.load_messages(session.id))
    runner._hint_tool_names(session, "some other provider failure")
    assert len(store.load_messages(session.id)) == n0


def test_hint_is_internal_message():
    """纠正提示不能被当成用户任务（resume 的 last_user_request）。"""
    from axon.session.resume import is_internal_message
    assert is_internal_message("<tool_name_hint>\n你刚才调用…\n</tool_name_hint>")
