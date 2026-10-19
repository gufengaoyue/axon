"""回归：v0.4.30 第二轮审计修复（复审发现的 P0/P1）。

第二轮复审（对 v0.4.29 修复的回归审）发现并修复：
  1. shell.py 缺 Path 导入 → 所有纯删除命令 NameError（v0.4.29 引入）
  2. file_edit 字节域与 CRLF 冲突 → Windows "自写→自读→自改"闭环断裂
  3. image/memory 调不存在的 gw.check → 图生图/global_memory_write 必失败
  4. verify 规则 2 的 scope 口径恒不匹配 → deny 记录从不被发现
  5. 插嘴注入 <user_interjection> 后"最后 user 消息"定位失准 →
     产物/进度日志/ resume 全线错位
  6. 压缩旧 summary 被当最新消息保留 → 摘要叠加（2 次压缩 2 份）
  7. gateway fut.result(timeout) 假超时（with 退出仍 wait）
  8. db PRAGMA 在坏库上先抛 → 修复指引永不触发
  9. 插嘴 settle 非流式分支丢文本；reset_queue 丢 asyncio 队列残留
 10. %VAR% 子串形态绕过；open('x') 误判为写；正则二次回溯
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from axon.agent.msgmeta import pack_tool_content
from axon.agent.verify import TurnVerifier
from axon.providers.base import Message, ToolCall


# ============================================================ 1) 删除链路

def test_shell_delete_routes_to_trash(tmp_path, cfg):
    """纯删除命令（含 glob）必须走回收站且不抛 NameError。"""
    from axon.tools.builtin.shell import make_spec
    from axon.tools.context import ToolContext
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.log").write_text("x", encoding="utf-8")
    (ws / "b.log").write_text("y", encoding="utf-8")
    cfg.workspace = ws
    spec = make_spec(ToolContext(ws, cfg, None))

    class _GW:
        session_id = "s"
        turn = 1
        def note_subaction(self, a):
            pass
    args = {"command": "rm -f *.log"}
    r = spec.handler(ToolCall(id="c1", name="execute_shell", arguments=args), args, _GW())
    assert r.ok, r.recovery_hint
    assert "回收站" in r.output
    assert not list(ws.glob("*.log"))


def test_shell_delete_tilde_expand(tmp_path, cfg):
    """~ 展开（与 command_rules._resolve_one 口径一致）——不误删、明确报未找到。"""
    from axon.tools.builtin.shell import make_spec
    from axon.tools.context import ToolContext
    spec = make_spec(ToolContext(tmp_path, cfg, None))

    class _GW:
        session_id = "s"
        turn = 1
        def note_subaction(self, a):
            pass
    args = {"command": "rm -f ~/this_file_should_not_exist_axon.txt"}
    r = spec.handler(ToolCall(id="c1", name="execute_shell", arguments=args), args, _GW())
    assert r.ok is False and "未找到" in r.recovery_hint


# ============================================================ 2) CRLF 编辑闭环

def test_file_edit_crlf_roundtrip(tmp_path, cfg):
    """write_file（Windows 落 CRLF）→ read（显示 LF）→ edit（LF old_string）通。"""
    from axon.tools.builtin.file_write import make_spec as wspec
    from axon.tools.builtin.file_edit import make_spec as espec
    from axon.tools.context import ToolContext
    ws = tmp_path
    w = wspec(ToolContext(ws, cfg, None))
    _a = {"path": "a.txt", "content": "x = 1\ny = 2\nz = 3\n"}
    w.handler(ToolCall(id="1", name="write_file", arguments=_a), _a, None)
    assert b"\r\n" in (ws / "a.txt").read_bytes()
    e = espec(ToolContext(ws, cfg, None))
    _b = {"path": str(ws / "a.txt"), "old_string": "x = 1\ny = 2",
          "new_string": "x = 10\ny = 20"}
    r = e.handler(ToolCall(id="2", name="edit_file", arguments=_b), _b, None)
    assert r.ok, r.recovery_hint
    assert b"x = 10\r\ny = 20" in (ws / "a.txt").read_bytes()


# ============================================================ 3) 规则 2 语义

def _mk_store(tmp_path, name):
    from axon.session.db import SessionStore, connect
    from axon.sandbox.audit import AuditLog
    st = SessionStore(connect(tmp_path / name))
    return st, AuditLog(st.conn)


def test_rule2_deny_then_finish_passes(tmp_path):
    """用户拒绝后模型解释结题 → 通过（正常交互，v0.1 语义保留）。"""
    from axon.sandbox.gateway import Action
    st, au = _mk_store(tmp_path, "r1.db")
    sid = st.create_session(str(tmp_path), "openai", "m")
    st.append_message(sid, Message(role="user", content="删"))
    st.append_message(sid, Message(role="assistant", content="", tool_calls=[
        ToolCall(id="c1", name="execute_shell", arguments={"command": "rm x"})]))
    au.record(Action(kind="shell", target="rm x", session_id=sid, turn=2),
              "deny", "command:delete_outside", "blocked")
    st.append_message(sid, Message(role="tool", tool_call_id="c1",
                                   name="execute_shell", content="拒绝"))
    st.append_message(sid, Message(role="assistant", content="好的"))
    msgs = st.load_messages(sid)
    r = TurnVerifier(st, au, None).verify(
        st.get_session(sid), "好的", scope_turns={m.turn for m in msgs})
    assert r.passed, r.failed_rules


def test_rule2_repeated_deny_fails(tmp_path):
    """同类动作被拒 ≥2 次（反复重试）→ 判负。"""
    from axon.sandbox.gateway import Action
    st, au = _mk_store(tmp_path, "r2.db")
    sid = st.create_session(str(tmp_path), "openai", "m")
    st.append_message(sid, Message(role="user", content="删"))
    st.append_message(sid, Message(role="assistant", content="", tool_calls=[
        ToolCall(id="c1", name="execute_shell", arguments={"command": "rm x"})]))
    au.record(Action(kind="shell", target="rm x", session_id=sid, turn=2),
              "deny", "command:delete_outside", "blocked")
    st.append_message(sid, Message(role="tool", tool_call_id="c1",
                                   name="execute_shell", content="拒绝"))
    st.append_message(sid, Message(role="assistant", content="", tool_calls=[
        ToolCall(id="c2", name="execute_shell", arguments={"command": "rm y"})]))
    au.record(Action(kind="shell", target="rm y", session_id=sid, turn=4),
              "ask_denied", "command:delete_outside", "blocked")
    st.append_message(sid, Message(role="tool", tool_call_id="c2",
                                   name="execute_shell", content="又拒绝"))
    st.append_message(sid, Message(role="assistant", content="好的"))
    msgs = st.load_messages(sid)
    r = TurnVerifier(st, au, None).verify(
        st.get_session(sid), "好的", scope_turns={m.turn for m in msgs})
    assert not r.passed and "red_actions_approved" in r.failed_rules


# ============================================================ 4) 插嘴注入后定位

def test_collect_products_after_interjection(tmp_path, cfg):
    """<user_interjection> 也是 user 角色——产物定位必须跳过它。"""
    from axon.agent.loop import AgentLoop
    from axon.agent.compaction import Compactor
    st, au = _mk_store(tmp_path, "p1.db")
    sid = st.create_session(str(tmp_path), "openai", "m")
    st.append_message(sid, Message(role="user", content="写文件"))
    st.append_message(sid, Message(role="assistant", content="", tool_calls=[
        ToolCall(id="c9", name="write_file", arguments={"path": "o.txt"})]))
    out = tmp_path / "o.txt"
    out.write_text("x", encoding="utf-8")
    st.append_message(sid, Message(role="tool", tool_call_id="c9", name="write_file",
                                   content=pack_tool_content(
                                       "ok", {"path": str(out), "sha256": "ab"})))
    st.append_message(sid, Message(role="user", content=(
        "<user_interjection>\n补充一句\n</user_interjection>")))
    st.append_message(sid, Message(role="assistant", content="完成"))
    loop = AgentLoop.__new__(AgentLoop)
    loop.store = st
    loop.config = cfg
    prods = loop._collect_products(st.get_session(sid))
    assert prods and prods[0]["path"].endswith("o.txt")


def test_journal_request_skips_interjection(tmp_path, cfg):
    from axon.memory.store import MemoryStore
    from axon.memory import journal as J
    from axon.agent.loop import TurnResult
    st, au = _mk_store(tmp_path, "p2.db")
    sid = st.create_session(str(tmp_path), "openai", "m")
    st.append_message(sid, Message(role="user", content="真正请求"))
    st.append_message(sid, Message(role="tool", tool_call_id="c",
                                   name="read_file", content="ok"))
    st.append_message(sid, Message(role="user", content=(
        "<user_interjection>\n插嘴内容\n</user_interjection>")))
    st.append_message(sid, Message(role="assistant", content="完成"))
    ms = MemoryStore(tmp_path / "AXON_j.md")
    entry = J.Journal(ms).record_turn(
        sid, TurnResult("完成", "verified", 1, 10), st.load_messages(sid), [])
    assert "真正请求" in entry
    assert "<user_interjection>" not in entry


# ============================================================ 5) 压缩不叠摘要

def test_compaction_no_summary_stacking(tmp_path, cfg):
    from axon.agent.compaction import Compactor
    st, au = _mk_store(tmp_path, "c1.db")
    sid = st.create_session(str(tmp_path), "openai", "m")
    for i in range(30):
        st.append_message(sid, Message(role="user" if i % 2 == 0 else "assistant",
                                       content=f"消息 {i} " + "内容" * 40))
    cp = Compactor(cfg)
    cp.protect_turns = 2
    cp.compact(sid, st.load_messages(sid), summarize=None, store=st)
    for i in range(4):
        st.append_message(sid, Message(role="user" if i % 2 == 0 else "assistant",
                                       content=f"新消息 {i} " + "内容" * 40))
    cp.compact(sid, st.load_messages(sid), summarize=None, store=st)
    sums = [m for m in st.load_messages(sid)
            if str(m.content or "").startswith("<conversation_summary")]
    assert len(sums) == 1, f"摘要叠加 {len(sums)} 份"


# ============================================================ 6) config null

def test_config_null_sections_with_overrides(tmp_path):
    from axon.config import load_config
    p = tmp_path / "cfg.yml"
    p.write_text(f"workspace: {tmp_path / 'w'}\nproject: {tmp_path}\n"
                 "model:\nagent:\n", encoding="utf-8")
    c = load_config(p, overrides={"active_provider": "openai", "model": "m1"})
    assert c.model.active_provider == "openai" and c.active_model() == "m1"


# ============================================================ 7) 工具不再调不存在的接口

def test_image_gen_base_must_be_inside_workspace(tmp_path, cfg):
    """图生图基准图限制在工作空间/项目内（结构性约束，替代不存在的 gw.check）。"""
    from axon.tools.builtin.image import make_spec
    from axon.tools.context import ToolContext
    from axon.config import ImageGenConfig
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://x/v1",
                                   model="m", api_key="k")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    cfg.workspace = ws
    cfg.project = proj          # outside 既不在 ws 也不在 proj → 应被拒
    spec = make_spec(ToolContext(ws, cfg, None))
    _a = {"prompt": "改图", "image_path": str(outside)}
    r = spec.handler(ToolCall(id="c1", name="image_generate", arguments=_a), _a, None)
    assert r.ok is False and "工作空间或项目目录内" in r.recovery_hint


def test_image_gen_fake_png_rejected(tmp_path, cfg):
    from axon.tools.builtin.image import make_spec
    from axon.tools.context import ToolContext
    from axon.config import ImageGenConfig
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://x/v1",
                                   model="m", api_key="k")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    fake = ws / "x.png"
    fake.write_text("not an image", encoding="utf-8")
    cfg.workspace = ws
    spec = make_spec(ToolContext(ws, cfg, None))
    _a = {"prompt": "改图", "image_path": str(fake)}
    r = spec.handler(ToolCall(id="c1", name="image_generate", arguments=_a), _a, None)
    assert r.ok is False and "伪装" in r.recovery_hint


def test_global_memory_write_works(tmp_path, cfg, monkeypatch):
    """global_memory_write 不再因 gw.check 不存在而必失败。"""
    from axon.tools.builtin.memory import make_global_spec
    from axon.memory.global_store import GlobalMemoryStore
    from axon.tools.context import ToolContext
    gs = GlobalMemoryStore(tmp_path / "longterm.md")
    gs.ensure_seeded()
    ctx = ToolContext(tmp_path, cfg, None, global_memory=gs)
    spec = make_global_spec(ctx)
    _a = {"section": "测试", "text": "跨项目事实一条"}
    r = spec.handler(ToolCall(id="c1", name="global_memory_write", arguments=_a), _a, None)
    assert r.ok, r.recovery_hint
    assert "跨项目事实一条" in gs.read()


# ============================================================ 8) 删除符 / 变量

def test_single_amp_delete_not_routed(tmp_path, cfg):
    from axon.sandbox.destructive import parse_delete_command
    assert parse_delete_command("del a.txt & del b.txt") is None
    assert parse_delete_command("rm a.txt\nrm b.txt") is None


def test_env_var_substring_form_blocked(tmp_path):
    from axon.sandbox.command_rules import analyze
    ws = tmp_path / "ws"
    ws.mkdir()
    a = analyze("type %USERPROFILE:~0,2%\\Users\\x\\.ssh\\id_rsa", ws)
    assert a.kind == "unknown"
    a2 = analyze("type ${HOME}/.ssh/id_rsa", ws)
    assert a2.kind == "unknown"


def test_read_open_not_treated_as_write(tmp_path):
    from axon.sandbox.command_rules import analyze
    ws = tmp_path / "ws"
    ws.mkdir()
    a = analyze("python -c \"print(open('x').read())\"", ws)
    assert a.kind == "read_only" and not a.interp_write_dynamic
    b = analyze("python -c \"open('a.csv')\"", ws)
    assert b.kind == "read_only"


def test_interp_write_analysis_fast(tmp_path):
    """大脚本不得二次回溯（旧实现 23KB 570ms）。"""
    import time
    from axon.sandbox.command_rules import analyze
    ws = tmp_path / "ws"
    ws.mkdir()
    big = "x = 1; " * 5000 + "open('a', 'w')"
    t0 = time.time()
    analyze('python -c "' + big + '"', ws)
    assert time.time() - t0 < 0.5, "分析耗时异常（可能二次回溯）"


# ============================================================ 9) 插嘴收尾

def test_settle_non_stream_uses_pending(cfg, monkeypatch):
    """stream_interject=false 时也能拿到 bridge fallback 的插嘴（不再静默丢）。"""
    from unittest.mock import MagicMock
    from axon import cli
    from axon.interject import InterjectBridge
    cfg.agent.stream_interject = False
    bridge = InterjectBridge()
    bridge.submit("非流式插嘴")
    app = {"keyboard": MagicMock(), "console": None, "cfg": cfg,
           "interject_bridge": bridge}
    app["keyboard"].has_pending.return_value = False
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    sent = []
    monkeypatch.setattr(cli, "_run_turn", lambda *a, **k: sent.append(a[2]))
    cli._settle_interjections(app, None)
    assert sent and "非流式插嘴" in sent[0]


def test_reset_queue_drains_to_fallback():
    from axon.interject import InterjectBridge
    b = InterjectBridge()

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)
    b.bind(_Loop())
    assert b.submit("队列内") is True
    b.reset_queue()
    assert b.take_all() == ["队列内"]      # 不随队列对象丢弃
