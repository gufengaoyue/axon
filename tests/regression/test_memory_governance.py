"""回归：记忆治理（设计 §4.6 test_memory_governance）。"""
from __future__ import annotations

import pytest

from axon.memory.store import (MemoryBudgetError, MemoryEntry, MemoryStore)


def test_add_and_render(memory):
    memory.add_entry(MemoryEntry(scope="project", confidence="verified",
                                 text="用户偏好中文回复", added_turn=1, source="用户明说"))
    block = memory.render_block()
    assert "用户偏好中文回复" in block
    assert "[project/verified/turn=1/src=用户明说]" in block


def test_inferred_marked(memory):
    memory.add_entry(MemoryEntry(scope="project", confidence="inferred",
                                 text="推测：用户在跑回归测试", added_turn=2, source="推断"))
    assert "inferred" in memory.render_block()


def test_budget_rejects(memory, tmp_path):
    store = MemoryStore(tmp_path / "m.md", token_budget=50)
    with pytest.raises(MemoryBudgetError):
        store.add_entry(MemoryEntry(scope="project", confidence="verified",
                                    text="x" * 500, added_turn=1, source="t"))


def test_dedup(memory):
    e = MemoryEntry(scope="project", confidence="verified", text="同一条目",
                    added_turn=1, source="t")
    memory.add_entry(e)
    memory.add_entry(e)
    assert len(memory.entries()) == 1


def test_remove(memory):
    memory.add_entry(MemoryEntry(scope="project", confidence="verified",
                                 text="待删除", added_turn=1, source="t"))
    assert memory.remove_entry("待删除") is True
    assert memory.remove_entry("待删除") is False
    assert len(memory.entries()) == 0


def test_max_entries(memory, tmp_path):
    store = MemoryStore(tmp_path / "m2.md", max_entries=2)
    for i in range(2):
        store.add_entry(MemoryEntry(scope="project", confidence="verified",
                                    text=f"条目{i}", added_turn=i, source="t"))
    with pytest.raises(MemoryBudgetError):
        store.add_entry(MemoryEntry(scope="project", confidence="verified",
                                    text="条目3", added_turn=3, source="t"))


def test_bad_scope_rejected(memory):
    with pytest.raises(Exception):
        memory.add_entry(MemoryEntry(scope="universe", confidence="verified",
                                     text="x", added_turn=1, source="t"))
