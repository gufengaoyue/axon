"""回归：会话索引与跨项目恢复（v0.2.0 需求 1 + resume bug 修复）。"""
from __future__ import annotations

import pytest

from axon.providers.base import Message


@pytest.fixture
def two_projects(store):
    """两个项目目录各建一个会话。"""
    sid_a = store.create_session("D:/proj/Alpha", "openai", "gpt-4o-mini")
    sid_b = store.create_session("D:/proj/Beta", "openai", "gpt-4o-mini")
    store.append_message(sid_a, Message(role="user", content="Alpha 项目的第一个任务"))
    store.append_message(sid_b, Message(role="user", content="Beta 项目的调试任务"))
    return sid_a, sid_b


def test_list_sessions_all_cross_project(store, two_projects):
    items = store.list_sessions_all(10)
    assert len(items) == 2
    names = {i["project_name"] for i in items}
    assert names == {"Alpha", "Beta"}
    assert all("title" in i and "msg_count" in i for i in items)


def test_auto_title_from_first_user_message(store, two_projects):
    sid_a, _ = two_projects
    sess = store.get_session(sid_a)
    assert sess.title.startswith("Alpha 项目的第一个任务")


def test_find_session_by_prefix_cross_project(store, two_projects):
    from axon.session.resume import find_session
    sid_a, sid_b = two_projects
    assert find_session(store, sid_a[:6]).id == sid_a
    assert find_session(store, sid_b[:6]).id == sid_b
    assert find_session(store, sid_a).id == sid_a       # 完整 id


def test_find_session_last_global(store, two_projects):
    from axon.session.resume import find_session
    latest = find_session(store, "last")
    assert latest is not None


def test_find_session_missing_raises_with_hint(store, two_projects):
    from axon.session.resume import load_for_resume
    with pytest.raises(ValueError) as ei:
        load_for_resume(store, "deadbeef")
    assert "最近的会话" in str(ei.value)     # 报错带索引提示（原 bug：干巴巴一句找不到）


def test_resume_rebuilds_context(store, two_projects):
    from axon.session.resume import load_for_resume
    sid_a, _ = two_projects
    sess, messages, note = load_for_resume(store, sid_a[:8])
    assert sess.id == sid_a
    assert len(messages) == 1
    assert "<resume_context>" in note.content
