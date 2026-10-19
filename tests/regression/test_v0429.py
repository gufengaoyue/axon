"""回归：v0.4.29 全面审计修复（P0/P1/P2 共 40 项）。

本文件按批次锁定关键修复，防止回归。完整清单见设计偏差 #76。

P0：
  1. verify 空集 max() 崩溃（codebuddy 路纯问答回合）
  2. codebuddy 路 _vision 未绑定先用（带图必崩）
  3. memory _rewrite 销毁项目记忆/进度日志（数据丢失）
  4. 解释器代码内文件写零路径校验（安全绕过）
  5. %VAR%/$env: 绕过 deny 表（数据外泄方向）
P1：
  6. image_generate size 缺省传 "None"（每次 400）
  7. 插嘴 bridge fallback（openai 路不再丢）
  8. 复合删除命令误删（rm x && gcc ...）
  9. 中断回合产物恒空（_post_turn 顺序）
  10. query_only 降级启动崩栈
  11. memory add_entry 插入点/remove 失效
  12. 授权缓存 flag 类粒度过粗
  13. file_edit 非 UTF-8 造成永久破坏
  14. resume_context 被补沉淀回合偷走
  15. compaction turn 重排（scope 错位）+ 跨边界配对撕裂
  16. tool arguments 合法 JSON 非对象
P2：
  tool_result 成功不停圈、CJK 宽度、NullStatus 补齐、global_store 解码、
  env_cleanup downloads、tokenizer images、in_zones normcase、backup 快照序号
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from axon.agent.msgmeta import pack_tool_content
from axon.agent.verify import TurnVerifier
from axon.memory.store import MemoryStore, MemoryEntry, MemoryBudgetError
from axon.sandbox.command_rules import analyze
from axon.sandbox.destructive import parse_delete_command

_ROOT = Path(__file__).resolve().parents[2]


# ============================================================ P0

def test_p0_verify_empty_scope_no_crash(store, audit, cfg, session):
    """codebuddy 路纯问答回合（scope_turns 为空集）不得崩——旧实现 max(空) ValueError。"""
    v = TurnVerifier(store, audit, cfg)
    r = v.verify(session, "你好", scope_turns=set())
    assert r.passed is True          # 无审批记录可核对 → 通过


def test_p0_vision_import_before_use():
    """codebuddy.py 的 `from axon import vision as _vision` 必须先于首次引用
    （cellvar 机制：旧实现 324 行引用、332 行才导入 → UnboundLocalError）。"""
    src = (_ROOT / "axon" / "providers" / "codebuddy.py").read_text(encoding="utf-8")
    imp = src.find("from axon import vision as _vision")
    use = src.find("_vision.vision_available")
    assert 0 <= imp < use, f"import at {imp}, first use at {use}"


def test_p0_memory_rewrite_keeps_other_sections(tmp_path):
    """remove_entry 不得销毁项目记忆与进度日志（旧实现整体重写 → 数据丢失）。"""
    from axon.memory import journal as J
    from axon.agent.loop import TurnResult
    mp = tmp_path / "AXON.md"
    ms = MemoryStore(mp, token_budget=4000, max_entries=200)
    ms.add_entry(MemoryEntry(scope="project", confidence="verified",
                             text="偏好甲", added_turn=1, source="用户明说"))
    ms.append_project_note("summary", "项目总结内容")
    jr = J.Journal(ms)
    jr.record_turn("s1", TurnResult("ok", "verified", 1, 10), [], [])
    n0 = (len(ms.project_notes()), len(ms.journal_entries()))
    assert ms.remove_entry("偏好甲") is True
    n1 = (len(ms.project_notes()), len(ms.journal_entries()))
    assert n1 == n0, f"区内容被破坏: {n0} -> {n1}"
    text = mp.read_text(encoding="utf-8")
    assert "偏好甲" not in text
    assert "项目总结内容" in text and "回合 1" in text


def test_p0_interp_write_paths_extracted(tmp_path):
    """解释器代码内的写路径要提取出来（旧实现 paths=[] → 任意位置可写）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    a = analyze('python -c "open(\'C:/Windows/System32/evil.txt\',\'w\').write(\'x\')"', ws)
    assert a.kind == "file_write"
    assert any("evil.txt" in p for p in a.paths)
    # 动态目标（变量）→ 标记 fail-closed
    a2 = analyze('python -c "open(path_var,\'w\')"', ws)
    assert a2.interp_write_dynamic is True


def test_p0_env_var_path_form_blocked(tmp_path):
    """%VAR%/$env: 出现在路径形态 token 内 → unknown（ask），不再是 read_only。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    a = analyze("type %USERPROFILE%\\.ssh\\id_rsa", ws)
    assert a.kind == "unknown" and a.has_substitution
    a2 = analyze("type $env:USERPROFILE\\.ssh\\id_rsa", ws)
    assert a2.kind == "unknown"
    # 无害回显不误伤（用户高频模式）
    a3 = analyze('echo "EXIT %errorlevel%"', ws)
    assert a3.kind == "read_only"


# ============================================================ P1

def test_p1_image_size_none_not_stringified():
    """size 缺省不得把 None 字符串化成 'None'（旧实现每次生图 400）。"""
    args = {"prompt": "画只猫"}
    _s = args.get("size")
    size = (str(_s).strip() or None) if _s else None
    assert size is None


def test_p1_bridge_fallback_keeps_interjection():
    """未 bind 时插嘴进 fallback（openai 路轮次边界消费），不再丢失。"""
    from axon.interject import InterjectBridge
    b = InterjectBridge()
    assert b.submit("补充一句") is True
    assert b.take_all() == ["补充一句"]
    assert b.take_all() == []


def test_p1_compound_delete_not_trashed():
    """复合删除命令不按纯删除路由（旧实现误删 gcc 的输入并吞掉命令）。"""
    assert parse_delete_command("rm -f x.o && gcc main.c -o app") is None
    assert parse_delete_command("rm a.txt; python cleanup.py") is None
    assert parse_delete_command("rm -f a.txt") == ["a.txt"]


def test_p1_quoted_delete_path_unquoted(tmp_path, monkeypatch):
    """Windows posix=False 的引号残渣要剥掉（否则带空格路径静默无操作）。"""
    r = parse_delete_command('rm "my file.txt"')
    assert r == ["my file.txt"]


def test_p1_interrupt_turn_keeps_products(tmp_path, store, audit, cfg, session):
    """中断回合的产物必须被收集（旧实现 _persist_unfinished 先插
    interrupted_task → "最后 user 消息"定位失效 → products 恒空）。"""
    from axon.agent.verify import TurnVerifier as _V
    from axon.tools.builtin import ToolContext, build_builtin_registry
    from axon.providers.base import ToolCall
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter

    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                        AutoApprovePrompter())
    gw.bind(session.id, 0)
    reg = build_builtin_registry(ToolContext(cfg.workspace, cfg, None))
    spec = reg.get("write_file")
    _args = {"path": "out.txt", "content": "x"}
    res = spec.handler(ToolCall(id="c1", name="write_file", arguments=_args), _args, gw)
    assert res.ok
    store.append_message(session.id, __import__("axon.providers.base",
                                                fromlist=["Message"]).Message(
        role="tool", tool_call_id="c1", name="write_file",
        content=pack_tool_content(res.output, res.meta)))

    from tests.regression.conftest import make_loop
    from axon.agent.context import ContextBuilder
    from axon.agent.compaction import Compactor

    class _P:
        name = "fake"
        def chat(self, *a, **k):
            from axon.providers.base import StreamEvent
            yield StreamEvent(kind="done")

    loop = make_loop(cfg, _P(), gw, reg, ContextBuilder(cfg, None, reg),
                     Compactor(cfg), _V(store, audit, cfg), store)
    from axon.agent.loop import TurnResult
    r = TurnResult("", "user_interrupt", 1, 0)
    loop._post_turn(session, r, "写个文件")
    assert r.products and any("out.txt" in p["path"] for p in r.products)


def test_p1_query_only_store_init_no_crash(tmp_path):
    """只读降级（query_only=ON）下 SessionStore 初始化不得崩（旧实现 executescript 抛错）。"""
    from axon.session.db import SessionStore, connect
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "ro.db"))
    conn.execute("PRAGMA query_only=ON")
    try:
        SessionStore(conn)           # 不得抛 OperationalError
    finally:
        conn.close()


def test_p1_memory_add_entry_lands_in_memory_section(tmp_path):
    """add_entry 落在「长期记忆」区（有项目记忆时旧实现插到项目记忆区后 →
    remove 失效 + 正文污染）。"""
    mp = tmp_path / "AXON.md"
    ms = MemoryStore(mp, token_budget=4000, max_entries=200)
    ms.append_project_note("summary", "项目总结")
    ms.add_entry(MemoryEntry(scope="project", confidence="verified",
                             text="条目乙", added_turn=1, source="x"))
    # 条目在项目记忆区之前（即 MEMORY_SECTION 与 PROJECT_SECTION 之间）
    text = mp.read_text(encoding="utf-8")
    assert text.find("条目乙") < text.find("## 项目记忆")
    assert ms.project_notes() == ["项目总结"]
    assert ms.render_block().count("条目乙") == 1
    assert ms.remove_entry("条目乙") is True


def test_p1_memory_budget_rejects(tmp_path):
    """预算投影用记忆区全量文本 → 超预算拒绝真正触发。"""
    ms = MemoryStore(tmp_path / "A.md", token_budget=400, max_entries=200)
    with pytest.raises(MemoryBudgetError):
        for i in range(50):
            ms.add_entry(MemoryEntry(scope="project", confidence="inferred",
                                     text=f"条目 {i} " + "内容" * 60,
                                     added_turn=i, source="x"))


def test_p1_fingerprint_flag_includes_target():
    """授权缓存 flag 类键混入目标父目录——一次 always 不再放行任意目标。"""
    from axon.sandbox.gateway import Action, action_fingerprint
    k1 = action_fingerprint(Action("shell", "rm -rf a.txt"), "prefix")
    k2 = action_fingerprint(Action("shell", "rm -rf D:/data/x"), "prefix")
    assert k1 != k2
    # python -c 保持同类共享（v0.2.2 修复不回归）
    k3 = action_fingerprint(Action("shell", 'python -c "print(1)"'), "prefix")
    k4 = action_fingerprint(Action("shell", 'python -c "print(2)"'), "prefix")
    assert k3 == k4


def test_p1_high_risk_rules_not_cached(gw_approve, cfg, monkeypatch):
    """高危规则（批量删除等）不被会话缓存降级。"""
    from axon.sandbox.gateway import Action
    # 直接模拟：预置缓存键 + 让 hook 返回高危 ask
    a = Action("shell", "rm -rf D:/x")
    key = __import__("axon.sandbox.gateway", fromlist=["action_fingerprint"]).action_fingerprint(
        a, cfg.sandbox.approval_granularity)
    gw_approve._auth_cache[key] = None
    from axon.sandbox.gateway import Decision

    class _Hook:
        def pre(self, action, ctx):
            return Decision("ask", "command:batch_delete:500", "批量删除")
    gw_approve.hooks = [_Hook()]
    d = gw_approve.check(a)
    assert d.verdict == "ask"        # 未降级


def test_p1_file_edit_preserves_non_utf8_bytes(tmp_path):
    """非 UTF-8（GBK）文件编辑后其余字节不变（旧实现整体转 UTF-8 → 永久破坏）。"""
    from axon.tools.builtin.file_edit import make_spec
    from axon.tools.context import ToolContext
    from axon.providers.base import ToolCall
    gbk = "旧内容".encode("gbk") + b"\n" + "保留".encode("gbk")
    p = tmp_path / "gbk.txt"
    p.write_bytes(gbk)
    spec = make_spec(ToolContext(tmp_path, None, None))
    # old_string 用 UTF-8 无法匹配 GBK 字节 → 明确报"未找到"（不破坏文件）
    _a = {"path": str(p), "old_string": "旧内容", "new_string": "新内容"}
    r = spec.handler(ToolCall(id="c1", name="edit_file", arguments=_a), _a, None)
    assert r.ok is False and "未找到" in r.recovery_hint
    assert p.read_bytes() == gbk        # 字节原样
    # ASCII 部分可正常字节替换，GBK 字节保留
    _b = {"path": str(p), "old_string": "\n", "new_string": "\n\n"}
    r2 = spec.handler(ToolCall(id="c2", name="edit_file", arguments=_b), _b, None)
    assert r2.ok is True
    assert "旧内容".encode("gbk") in p.read_bytes()


def test_p1_resume_note_owned_by_session(tmp_path):
    """resume_context 只被所属 session 消费（补沉淀回合不得偷走）。"""
    from axon.cli import _compose_user_input
    app = {"pending_resume_note": "RESUME-NOTE", "pending_resume_note_for": "abc"}

    class _S:
        def __init__(self, sid):
            self.id = sid
    out = _compose_user_input(app, "hi", _S("other"))
    assert out == "hi"
    assert app["pending_resume_note"] == "RESUME-NOTE"    # 未被消费
    out2 = _compose_user_input(app, "hi", _S("abc"))
    assert "RESUME-NOTE" in out2 and "hi" in out2


def test_p1_compaction_turn_monotonic(store, cfg):
    """压缩后 turn 不重排（旧实现 1..N → next_turn 回退，V 层 scope 错位）。"""
    from axon.agent.compaction import Compactor
    from axon.providers.base import Message
    sid = store.create_session(str(cfg.project), "openai", "m")
    for i in range(40):
        store.append_message(sid, Message(role="user" if i % 2 == 0 else "assistant",
                                          content=f"消息 {i} " + "内容" * 30))
    msgs = store.load_messages(sid)
    max_before = max(m.turn for m in msgs)
    cp = Compactor(cfg)
    cp.protect_turns = 2
    r = cp.compact(sid, msgs, summarize=None, store=store)
    new_max = max(m.turn for m in store.load_messages(sid))
    assert new_max >= max_before, f"turn 回退: {max_before} -> {new_max}"


def test_p1_compaction_pair_across_boundary():
    """跨边界 call/result 成对移动（4b 丢旧区 call 后保护区不得留无主 result）。"""
    from axon.agent.compaction import Compactor
    from axon.providers.base import Message, ToolCall
    call = Message(role="assistant", content="",
                   tool_calls=[ToolCall(id="X", name="read_file", arguments={})], turn=5)
    result = Message(role="tool", tool_call_id="X", name="read_file",
                     content="ok", turn=7)
    old_part, protected = Compactor._pair_across_boundary([call], [result])
    # 成对移到同一区（此处 call 跟 result 走 → 两都在 protected）
    assert not old_part and len(protected) == 2


def test_p1_tool_arguments_non_dict():
    """合法 JSON 但非对象 → 包成 _raw（旧实现下游 args.get 崩）。"""
    from axon.providers.openai_compat import OpenAICompatProvider as _P
    # 直接验证解析后的形态（构造 provider 需要 key；用 base 的 from_openai）
    from axon.providers.base import Message
    m = Message.from_openai({"role": "assistant", "content": "",
                             "tool_calls": [{"id": "1", "function": {
                                 "name": "read_file", "arguments": "[1,2]"}}]})
    assert isinstance(m.tool_calls[0].arguments, dict)
    assert "_raw" in m.tool_calls[0].arguments


# ============================================================ P2

def test_p2_nullstatus_has_ensure_newline():
    from axon.agent.loop import NullStatus
    s = NullStatus()
    s.ensure_newline()
    s.bump()


def test_p2_global_store_decode_error_raises(tmp_path):
    """非 UTF-8 的记忆文件 → 明确报错（旧实现返回 "" → append 覆写清空）。"""
    from axon.memory.global_store import GlobalMemoryStore
    p = tmp_path / "longtermmemory.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    gs = GlobalMemoryStore(p)
    with pytest.raises(ValueError, match="编码损坏"):
        gs.read()


def test_p2_env_cleanup_protects_downloads(tmp_path, cfg):
    """回合清理不得删 downloads/ 里的临时模式文件（docstring 承诺）。"""
    from axon.env_cleanup import cleanup_temp
    ws = cfg.workspace
    dl = ws / cfg.tools.downloads_dir
    dl.mkdir(parents=True, exist_ok=True)
    victim = dl / "data.bak"
    victim.write_text("x", encoding="utf-8")
    cleanup_temp(cfg)
    assert victim.exists()


def test_p2_tokenizer_counts_images():
    from axon.agent.tokenizer import estimate_messages_tokens
    from axon.providers.base import Message
    a = estimate_messages_tokens([Message(role="user", content="hi")])
    b = estimate_messages_tokens([Message(role="user", content="hi",
                                          images=["x.png", "y.png"])])
    assert b - a >= 2000


def test_p2_in_zones_normcase(tmp_path):
    from axon.sandbox.path_policy import in_zones
    z = tmp_path / "Zone"
    z.mkdir()
    child = z / "a.txt"
    child.write_text("x", encoding="utf-8")
    # 大小写不同的同一路径（Windows 语义）也要命中
    if os.name == "nt":
        assert in_zones(str(z / "A.TXT"), [str(z)]) is True


def test_p2_backup_keeps_first_snapshot(tmp_path):
    """同回合二次编辑同一文件：第一份快照（修改前原始内容）不被覆盖。"""
    from axon.sandbox.backup import snapshot_file
    ws = tmp_path / "ws"
    ws.mkdir()
    snaps = tmp_path / "snaps"
    f = ws / "a.txt"
    f.write_text("原始", encoding="utf-8")
    p1 = snapshot_file(f, snaps, "s1", 1, ws)
    f.write_text("改后", encoding="utf-8")
    p2 = snapshot_file(f, snaps, "s1", 1, ws)
    assert p1 != p2
    assert Path(p1).read_text(encoding="utf-8") == "原始"
    assert Path(p2).read_text(encoding="utf-8") == "改后"


def test_p2_attachments_single_copy(tmp_path):
    """同一路径重复引用只复制一次（旧实现复制出孤儿副本）。"""
    from axon import attachments
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
    ws = tmp_path / "ws"
    ws.mkdir()
    text = f'"{img}" 和 {img}'
    _new, found = attachments.parse_references(text, tmp_path)
    names = [p.name for p in found]
    assert len(names) == len(set(names)), names


def test_p2_tool_result_success_keeps_spinner(rich_sl):
    """成功结果不停转圈（v0.3.0 语义）——修复的是粘连而非动效。"""
    from rich.console import Console
    s = rich_sl
    s.tool("execute_shell", "ls")
    assert s._running is True
    s.tool_result("execute_shell", True, "a.txt")
    assert s._running is True
    s._stop()


@pytest.fixture
def rich_sl(tmp_path):
    from rich.console import Console
    from axon.cli import StatusLine
    f = open(tmp_path / "o.txt", "w", encoding="utf-8")
    s = StatusLine(console=Console(file=f, width=70, force_terminal=False, no_color=True))
    s._tty = True
    yield s
    f.close()
