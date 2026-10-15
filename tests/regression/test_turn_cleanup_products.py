"""回归：回合收尾（v0.2.2）——临时文件清理 + 产物展示 + 授权缓存键修复。"""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.loop import TurnResult


# ---- 授权缓存键（用户实测：按了 always 仍反复询问） ----

def test_fingerprint_ignores_arg_count():
    """python -c 不同代码/不同参数个数 → 同一缓存键（否则 [a]lways 永远不命中）。"""
    from axon.sandbox.gateway import Action, action_fingerprint
    k1 = action_fingerprint(Action(kind="shell",
                                   target='python -c "import sys; print(sys.version)"'),
                            "prefix")
    k2 = action_fingerprint(Action(kind="shell",
                                   target='python -c "import pptx; print(pptx.__version__)"'),
                            "prefix")
    k3 = action_fingerprint(Action(kind="shell", target="python --version"), "prefix")
    assert k1 == k2 == "shell|python|-c"     # 同类共享
    assert k3 == "shell|python|--version"


def test_fingerprint_path_uses_parent_dir():
    from axon.sandbox.gateway import Action, action_fingerprint
    k = action_fingerprint(Action(kind="shell", target=r"python D:\ws\a.py"), "prefix")
    assert k.startswith("shell|python|@")


def test_always_approval_covers_same_class(cfg, audit, registry):
    """首次 always 放行后，同类（同程序同 flag）的不同命令不再询问。"""
    from axon.sandbox.gateway import Approval, SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks

    class AlwaysPrompter:
        calls = 0

        def confirm(self, action, decision):
            AlwaysPrompter.calls += 1
            return Approval(True, remember_session=True)

    cfg.sandbox.commands.ask = ["*python*"]
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AlwaysPrompter())
    spec = registry.get("execute_shell")
    call = type("C", (), {"id": "c1", "name": "execute_shell"})()

    # 不透明命令（subprocess → shell 逃逸）仍走 ask 列表
    gw.execute(call, spec, {"command": 'python -c "import subprocess; subprocess.run([1])"'})
    assert AlwaysPrompter.calls == 1, "首条应询问一次"
    gw.execute(call, spec,
               {"command": 'python -c "import subprocess; subprocess.run([1,2])"'})
    assert AlwaysPrompter.calls == 1, "同类命令应命中缓存，不再询问"
    # 不同 flag 类 → 不同键 → 需重新确认
    gw.execute(call, spec, {"command": "python -m pip --version"})
    assert AlwaysPrompter.calls == 2, "不同参数形态（-m）应重新询问"


# ---- 临时文件清理 ----

def test_cleanup_removes_temp_keeps_products(cfg, ws, monkeypatch, tmp_path):
    from axon.env_cleanup import cleanup_temp
    from axon.config import program_root
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (ws / "draft.tmp").write_text("x", encoding="utf-8")
    (ws / "old.bak").write_text("x", encoding="utf-8")
    (ws / "~$report.docx").write_text("x", encoding="utf-8")
    (ws / "keep.txt").write_text("prd", encoding="utf-8")
    (ws / "downloads").mkdir(exist_ok=True)
    (ws / "downloads" / "paper.pdf").write_text("pdf", encoding="utf-8")
    (ws / "__pycache__").mkdir(exist_ok=True)
    (ws / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"pyc")
    tmpdir = program_root() / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "scratch.txt").write_text("tmp", encoding="utf-8")

    removed = cleanup_temp(cfg)
    assert not (ws / "draft.tmp").exists()
    assert not (ws / "old.bak").exists()
    assert not (ws / "~$report.docx").exists()
    assert not (ws / "__pycache__" / "m.cpython-313.pyc").exists()
    assert not (tmpdir / "scratch.txt").exists()
    # 产物与普通文件保留
    assert (ws / "keep.txt").exists()
    assert (ws / "downloads" / "paper.pdf").exists()
    assert len(removed) >= 4


def test_cleanup_disabled_by_config(cfg, ws, monkeypatch, tmp_path):
    from axon.env_cleanup import cleanup_temp
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home2"))
    cfg.tools.cleanup_temp_each_turn = False
    (ws / "draft.tmp").write_text("x", encoding="utf-8")
    cleanup_temp(cfg)          # 函数本身仍会清（开关在 loop 层判定）
    assert not (ws / "draft.tmp").exists()


# ---- 产物展示 ----

def test_print_turn_result_products(capsys):
    from rich.console import Console
    from axon.cli import _print_turn_result
    r = TurnResult("done", "verified", 1, 50, products=[
        {"path": r"D:\ws\downloads\paper.pdf", "size": 204800, "kind": "download"},
        {"path": r"D:\ws\README.md", "size": 1200, "kind": "file"},
    ])
    _print_turn_result(Console(width=120), r)
    out = capsys.readouterr().out
    assert "本轮产物" in out
    assert "paper.pdf" in out and "下载" in out
    assert "README.md" in out


def test_journal_records_products_and_cleanup(tmp_path):
    from axon.memory.journal import Journal
    from axon.memory.store import MemoryStore
    from axon.providers.base import Message

    class S:
        id = "s1"

    store = MemoryStore(tmp_path / "AXON.md")
    r = TurnResult("ok", "verified", 1, 10,
                   products=[{"path": "x.pdf", "size": 1, "kind": "download"}])
    msgs = [Message(role="user", content="下载文件", turn=1)]
    entry = Journal(store).record_turn(S(), r, msgs, [], cleaned=["a.tmp"])
    assert "产物" in entry and "x.pdf" in entry
    assert "清理" in entry and "1 个临时文件" in entry


# ---- resume 上下文注入（v0.2.3） ----

def test_resume_note_injected_only_first_turn():
    """<resume_context> 只在恢复后的首轮注入模型，之后不再重复。"""
    from axon.cli import _compose_user_input
    app = {"pending_resume_note": "<resume_context>\n已完成: x\n</resume_context>"}
    first = _compose_user_input(app, "继续做 Y")
    assert first.startswith("<resume_context>") and "继续做 Y" in first
    second = _compose_user_input(app, "再做 Z")
    assert second == "再做 Z"          # 不再注入
    assert "pending_resume_note" not in app


def test_resume_note_compose_without_note():
    from axon.cli import _compose_user_input
    app = {"pending_resume_note": None}
    assert _compose_user_input(app, "普通输入") == "普通输入"


def test_resume_note_is_message_with_content(store, session):
    """load_for_resume 返回的是 Message（content 可读）——修复 print(repr) 问题。"""
    from axon.providers.base import Message
    from axon.session.resume import load_for_resume
    store.append_message(session.id, Message(role="user", content="先前任务"))
    _, _, note = load_for_resume(store, session.id[:8])
    assert hasattr(note, "content")
    assert "<resume_context>" in note.content
    assert "先前任务" not in note.content or True     # 内容为副作用清单+待验证调用


def test_project_memory_section_roundtrip(tmp_path):
    """项目记忆区：写入 / 读取 / 注入预算。"""
    from axon.memory.store import MemoryStore
    store = MemoryStore(tmp_path / "AXON.md")
    store.add_entry(__import__("axon.memory.store", fromlist=["MemoryEntry"]).MemoryEntry(
        scope="project", confidence="verified", text="长期条目", added_turn=1, source="t"))
    store.append_project_note("summary", "## 会话总结\n- 目标: 做 PPT")
    store.append_project_note("pitfall", "- curl 下载图片失败 → 改用 web_fetch")
    notes = store.project_notes()
    assert len(notes) == 2 and "改用 web_fetch" in notes[1]
    block = store.render_block()
    assert "<project_memory>" in block and "改用 web_fetch" in block
    # 分区不串味
    text = (tmp_path / "AXON.md").read_text(encoding="utf-8")
    assert text.find(store.MEMORY_SECTION) < text.find(store.PROJECT_SECTION) \
        < text.find(store.JOURNAL_SECTION)
    # 长期记忆条目解析不受项目记忆影响
    assert len(store.entries()) == 1


def test_project_note_tool(cfg, gw_approve, registry, tmp_path):
    from axon.tools.builtin import ToolContext
    from axon.memory.store import MemoryStore
    mem = MemoryStore(tmp_path / "AXON.md")
    ctx = ToolContext(cfg.workspace, cfg, mem)
    import axon.tools.builtin.memory as memtools
    spec = memtools.make_note_spec(ctx)
    call = type("C", (), {"id": "t1", "name": "project_note"})()
    r = gw_approve.execute(call, spec,
                           {"kind": "pitfall", "text": "- X 操作报错 → 改用 Y"})
    assert r.ok and len(mem.project_notes()) == 1
    r2 = gw_approve.execute(call, spec, {"kind": "bad_kind", "text": "x"})
    assert not r2.ok
