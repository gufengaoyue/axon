"""回归：压缩（设计 §4.6 test_compaction）。

context_window=1000 → force 水位 900 tokens；批量造消息越过阈值。
"""
from __future__ import annotations

from tests.regression.conftest import (assistant_msg, tool_msg, user_msg)


def _fake_summary(prompt: str) -> str:
    return ("goal: 测试压缩\nconstraints: 无\nprogress: 已完成若干工具调用\n"
            "decisions: 采用默认方案\nfiles_touched: a.txt b.txt\n"
            "errors_and_fixes: 无\nopen_questions: 无\n"
            "user_preferences: 中文回复\nnext_steps: 继续验证")


def _fat_messages(n: int = 40):
    msgs = []
    for i in range(n):
        msgs.append(user_msg(f"第 {i} 轮问题：" + "内容" * 100, turn=i * 3 + 1))
        msgs.append(assistant_msg(f"第 {i} 轮回答：" + "回答" * 100, turn=i * 3 + 2))
        msgs.append(tool_msg(f"c{i}", "read_file", "结果：" + "x" * 300, turn=i * 3 + 3))
    return msgs


def test_force_compaction_triggers(compactor, store, session, cfg):
    msgs = _fat_messages(40)
    for m in msgs:
        store.append_message(session.id, m)
    loaded = store.load_messages(session.id)
    from axon.agent.tokenizer import estimate_messages_tokens
    window = cfg.model.context_window
    before = estimate_messages_tokens(loaded)
    assert before >= cfg.context.compact_force_ratio * window, \
        "测试前提：消息需越过 90% 水位"

    result = compactor.compact(session.id, loaded, summarize=_fake_summary, store=store)

    # 9 字段摘要
    for field in ("goal", "constraints", "progress", "decisions", "files_touched",
                  "errors_and_fixes", "open_questions", "user_preferences", "next_steps"):
        assert field in result.summary, f"摘要缺字段: {field}"
    # 压缩生效且记账
    assert result.after_tokens < result.before_tokens
    rows = store.conn.execute(
        "SELECT trigger, before_tokens, after_tokens FROM compaction_events"
        " WHERE session_id=?", (session.id,)).fetchall()
    assert len(rows) == 1 and rows[0]["trigger"] == "force"
    assert rows[0]["before_tokens"] == result.before_tokens


def test_orphan_tool_pairs_cleaned(compactor, store, session):
    msgs = _fat_messages(30)
    # 孤立 tool 消息（无配对 call）插在「老区」——按设计 stage 4a 全区丢弃
    msgs.insert(3, tool_msg("no-such-call", "read_file", "孤立结果", turn=4))
    for m in msgs:
        store.append_message(session.id, m)
    loaded = store.load_messages(session.id)
    result = compactor.compact(session.id, loaded, summarize=_fake_summary, store=store)
    call_ids = {tc.id for m in result.messages if m.role == "assistant"
                for tc in m.tool_calls}
    for m in result.messages:
        if m.role == "tool":
            assert m.tool_call_id in call_ids, "孤立 tool 消息未被清理"


def test_warn_cleanup_elides_old_tool_outputs(ctx_builder, session):
    from axon.providers.base import Message
    history = []
    for i in range(30):
        history.append(Message(role="user", content=f"u{i}", turn=i * 2 + 1))
        history.append(Message(role="tool", content=f"结果{i}" + "y" * 50,
                               tool_call_id=f"c{i}", name="read_file", turn=i * 2 + 2))
    cleaned, n = ctx_builder.cleanup(history)
    assert n > 0
    elided = [m for m in cleaned if m.content and m.content.startswith("[tool output elided")]
    assert len(elided) == n
    # 保护边界内的工具输出不被清理
    tail = cleaned[-12:]
    assert not any(m.content and m.content.startswith("[tool output elided")
                   for m in tail if m.role == "tool")


def test_fallback_summary_when_provider_fails(compactor, store, session):
    msgs = _fat_messages(20)
    for m in msgs:
        store.append_message(session.id, m)
    loaded = store.load_messages(session.id)

    def boom(_prompt):
        raise RuntimeError("provider down")

    result = compactor.compact(session.id, loaded, summarize=boom, store=store)
    assert "goal" in result.summary  # 确定性回退摘要仍含 9 字段
