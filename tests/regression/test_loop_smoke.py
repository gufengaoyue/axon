"""回归：loop 冒烟（端到端：脚本化 provider → 写文件 → V 层验证通过）。"""
from __future__ import annotations

from tests.regression.conftest import FakeProvider, make_loop, tc, txt


def test_loop_write_file_verified(cfg, gw_approve, registry, ctx_builder, compactor,
                                  verifier, store, session, ws):
    """FakeProvider：第 1 轮调 write_file，第 2 轮给终答 → verified。"""
    provider = FakeProvider([
        tc("write_file", {"path": "hello.txt", "content": "你好，AXON"}),
        txt("已完成：写入 hello.txt"),
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "帮我写个文件")
    assert result.stop_reason == "verified"
    # v0.3.0：产物在回合收尾时归档到项目目录（工作空间是草稿区）
    assert (cfg.project / "hello.txt").read_text(encoding="utf-8") == "你好，AXON"
    assert result.turns_used == 2


def test_loop_unknown_tool_feedback(cfg, gw_approve, registry, ctx_builder, compactor,
                                    verifier, store, session, ws):
    """未知工具 → 错误观察回喂 → 模型改用存在的工具。"""
    provider = FakeProvider([
        tc("no_such_tool", {}, cid="c1"),
        tc("write_file", {"path": "ok.txt", "content": "v"}, cid="c2"),
        txt("done"),
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "测试未知工具")
    assert result.stop_reason == "verified"
    assert (cfg.project / "ok.txt").exists()          # v0.3.0：产物归档到项目目录
    # 未知工具的观察消息已落库
    msgs = store.load_messages(session.id)
    assert any(m.role == "tool" and "工具不存在" in (m.content or "") for m in msgs)


def test_loop_shell_echo(cfg, gw_approve, registry, ctx_builder, compactor,
                         verifier, store, session, ws):
    provider = FakeProvider([
        tc("execute_shell", {"command": "echo hello-axon"}, cid="c1"),
        txt("命令已执行"),
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "跑个 echo")
    assert result.stop_reason == "verified"
    msgs = store.load_messages(session.id)
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert any("hello-axon" in (m.content or "") for m in tool_msgs)


def test_loop_denied_action_blocks(cfg, gw_deny, registry, ctx_builder, compactor,
                                   verifier, store, session, ws):
    """AutoDeny：shell 被拒 → 错误观察回喂（error_type=permanent）→ 终答 → 验证记录。"""
    provider = FakeProvider([
        tc("execute_shell", {"command": "rm -rf /"}, cid="c1"),
        txt("无法执行该命令"),
    ])
    loop = make_loop(cfg, provider, gw_deny, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "删除一切")
    # 终答非空且无 transient/resource 悬挂 → verified（拒绝已审计，符合规则 2 定义）
    assert result.stop_reason == "verified"
    from axon.sandbox.audit import AuditLog
    rows = AuditLog(store.conn).rows(session.id)
    assert any(r["verdict"] == "deny" and r["kind"] == "shell" for r in rows)


def test_loop_stuck_detection(cfg, gw_approve, registry, ctx_builder, compactor,
                              verifier, store, session, ws):
    """连续 3 次相同调用 → 打转中断。"""
    repeat = tc("read_file", {"path": "missing.txt"})
    provider = FakeProvider([repeat, repeat, repeat, repeat, repeat])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "反复读不存在的文件")
    assert result.stop_reason == "stuck_break"


def test_loop_max_turns(cfg, gw_approve, registry, ctx_builder, compactor,
                        verifier, store, session, ws):
    """每轮不同的调用（避开打转检测）→ max_turns 收尾。"""
    cfg.agent.max_turns = 3
    script = [tc("read_file", {"path": f"m{i}.txt"}, cid=f"c{i}") for i in range(10)]
    provider = FakeProvider(script)
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "无限循环")
    assert result.stop_reason == "max_turns" and result.turns_used == 3
