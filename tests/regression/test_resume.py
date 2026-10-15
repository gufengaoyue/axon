"""回归：会话恢复（设计 §4.6 test_resume）。"""
from __future__ import annotations

from axon.providers.base import Message, ToolCall
from axon.sandbox.audit import AuditLog
from axon.sandbox.gateway import Action
from axon.session.resume import build_resume_message, find_pending_tool_calls, load_for_resume


def test_resume_reloads_messages(store, session, cfg):
    store.append_message(session.id, Message(role="user", content="第一问"))
    store.append_message(session.id, Message(role="assistant", content="第一答"))
    sess, messages, note = load_for_resume(store, "last")
    assert sess is not None and sess.id == session.id
    assert len(messages) == 2
    assert "<resume_context>" in note.content


def test_resume_lists_side_effects(store, session, cfg):
    audit = AuditLog(store.conn)
    audit.record(Action(kind="fs_write", target="/ws/a.txt", session_id=session.id),
                 "allow", "path:workspace", "ok")
    audit.record(Action(kind="shell", target="echo hi", session_id=session.id),
                 "allow", "command:allow:echo", "ok")
    sess, _, note = load_for_resume(store, "last")
    assert "fs_write" in note.content and "echo hi" in note.content
    assert "completed_side_effects" in note.content


def test_resume_marks_pending_tool_calls(store, session, cfg):
    store.append_message(session.id, Message(
        role="assistant", content=None,
        tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})]))
    sess, messages, note = load_for_resume(store, "last")
    pending = find_pending_tool_calls(messages)
    assert pending == ["read_file(c1)"]
    assert "pending_tool_calls" in note.content
    assert "请勿重复执行" in note.content


def test_resume_no_reexecution(store, session, cfg):
    """恢复只重放消息，不产生任何新的 audit 行（不重放执行）。"""
    audit = AuditLog(store.conn)
    store.append_message(session.id, Message(role="user", content="q"))
    before = len(audit.rows())
    load_for_resume(store, "last")
    assert len(audit.rows()) == before


def test_resume_by_id_prefix(store, session, cfg):
    sess, _, _ = load_for_resume(store, session.id[:6])
    assert sess is not None and sess.id == session.id


def test_resume_unknown_id_raises(store, session, cfg):
    import pytest
    with pytest.raises(ValueError):
        load_for_resume(store, "nonexistent")
