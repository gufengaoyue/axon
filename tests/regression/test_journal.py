"""回归：AXON.md 进度日志（v0.2.0 需求 3）。"""
from __future__ import annotations

import pytest

from axon.agent.loop import TurnResult
from axon.memory.journal import Journal
from axon.memory.store import MemoryEntry, MemoryStore
from axon.providers.base import Message, ToolCall


def _session_stub(sid="s1"):
    class S:
        id = sid
    return S()


def _turn_msgs():
    return [
        Message(role="user", content="帮我把 README 丰富一下", turn=1),
        Message(role="assistant", content=None, turn=2, tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})]),
        Message(role="tool", content="README 内容", turn=3, tool_call_id="c1",
                name="read_file"),
        Message(role="assistant", content=None, turn=4, tool_calls=[
            ToolCall(id="c2", name="write_file", arguments={"path": "README.md"})]),
        Message(role="tool", content="已写入", turn=5, tool_call_id="c2",
                name="write_file"),
    ]


def test_record_turn_appends_journal(tmp_path):
    store = MemoryStore(tmp_path / "AXON.md")
    j = Journal(store)
    r = TurnResult("完成", "verified", 2, 100)
    entry = j.record_turn(_session_stub(), r, _turn_msgs(),
                          [{"kind": "fs_write", "target": "README.md",
                            "result": "ok", "verdict": "allow"}])
    assert "请求" in entry and "read_file" in entry and "write_file" in entry
    assert "README.md" in entry          # 变更清单
    journals = store.journal_entries()
    assert len(journals) == 1
    assert "README" in journals[0][1]


def test_dual_section_layout(tmp_path):
    """记忆条目进「长期记忆」区，日志进「进度日志」区，互不串区。"""
    store = MemoryStore(tmp_path / "AXON.md")
    store.add_entry(MemoryEntry(scope="project", confidence="verified",
                                text="用户偏好中文", added_turn=1, source="用户明说"))
    j = Journal(store)
    j.record_turn(_session_stub(), TurnResult("ok", "verified", 1, 10),
                  _turn_msgs(), [])
    text = (tmp_path / "AXON.md").read_text(encoding="utf-8")
    mem_idx = text.find(store.MEMORY_SECTION)
    j_idx = text.find(store.JOURNAL_SECTION)
    assert 0 < mem_idx < j_idx
    assert "用户偏好中文" in text[mem_idx:j_idx]     # 条目在日志区之前
    assert "进度" in text[j_idx:]


def test_render_block_includes_progress(tmp_path):
    store = MemoryStore(tmp_path / "AXON.md")
    store.add_entry(MemoryEntry(scope="project", confidence="verified",
                                text="重要事实X", added_turn=1, source="t"))
    j = Journal(store)
    j.record_turn(_session_stub(), TurnResult("ok", "verified", 1, 10),
                  _turn_msgs(), [])
    block = store.render_block()
    assert "<long_term_memory>" in block
    assert "重要事实X" in block
    assert "<project_progress>" in block
    assert "read_file" in block


def test_journal_budget_keeps_recent(tmp_path):
    """进度日志超预算 → 保留最近的，丢弃更早的。"""
    store = MemoryStore(tmp_path / "AXON.md", token_budget=600)
    j = Journal(store)
    for i in range(30):
        j.record_turn(_session_stub(), TurnResult("ok", "verified", 1, 10),
                      _turn_msgs(), [])
    block = store.render_block()
    assert "更早的" in block              # 有未注入提示
    assert "read_file" in block           # 最近的仍注入


def test_auto_journal_creates_file_skeleton(tmp_path):
    store = MemoryStore(tmp_path / "nested" / "AXON.md")
    Journal(store).record_turn(_session_stub(), TurnResult("ok", "verified", 1, 1),
                               _turn_msgs(), [])
    text = (tmp_path / "nested" / "AXON.md").read_text(encoding="utf-8")
    assert store.MEMORY_SECTION in text and store.JOURNAL_SECTION in text
