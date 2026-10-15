"""回归：v0.4.8 三项（用户连续实测）。

① 粘贴机制修正：只监听 **Ctrl+V 粘贴动作**（不再后台轮询截图）；提示走 toast
   （不再直接 print —— 那会破坏 prompt_toolkit 输入行）
② 项目目录默认：未指定 → 当前目录；当前目录是主程序目录 → `<用户目录>/axon/<时间戳>`
③ 输入补全：`/` → 斜杠命令；行首 `>` → 项目文件（≤15 个）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import (default_project_dir, is_program_dir, load_config,
                         program_dir)


# ---- ② 项目目录默认 ----

def test_default_project_is_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    assert Path(default_project_dir()) == tmp_path.resolve()


def test_default_project_creates_timestamp_dir_when_in_program_dir(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(program_dir())                 # 模拟在程序目录启动
    got = Path(default_project_dir())
    assert got.parent == Path.home() / "axon"
    assert got.is_dir()
    # 名称 YYYY-MM-dd_HH-MM-SS（Windows 不允许冒号，故用连字符）
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", got.name)


def test_load_config_project_defaults_to_cwd(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text("model:\n  active_provider: openai\n",
                                         encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    cfg = load_config()
    assert Path(cfg.project) == tmp_path.resolve()


def test_explicit_project_wins(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        f"project: {tmp_path / 'explicit'}\nmodel:\n  active_provider: openai\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    cfg = load_config()
    assert Path(cfg.project).name == "explicit"


# ---- ① 粘贴：只认 Ctrl+V，不再后台监听 ----

def test_clipboard_watch_default_off(cfg):
    assert cfg.tools.clipboard_watch is False


def test_paste_handler_inserts_reference_and_toast(tmp_path):
    from axon import attachments
    from axon.cli import _paste_from_clipboard

    proj = tmp_path / "proj"
    proj.mkdir()
    watcher = attachments.ClipboardWatcher(proj)
    state = {"clipwatch": watcher, "toast": ""}

    class _Buf:
        def __init__(self):
            self.text = ""

        def insert_text(self, s):
            self.text += s

    class _App:
        def __init__(self):
            self.invalidated = False

        def invalidate(self):
            self.invalidated = True

    class _Event:
        def __init__(self):
            self.current_buffer = _Buf()
            self.app = _App()

    # 模拟剪贴板里有一个图片文件
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    watcher.poll_once = lambda: attachments.ingest([img], proj)
    ev = _Event()
    _paste_from_clipboard(state, ev)
    assert ev.current_buffer.text.startswith(">")       # 引用进输入行（可继续打字）
    assert "已收编" in state["toast"]
    assert ev.app.invalidated is True
    assert watcher.take_pending()                       # 已入待发送队列


def test_paste_handler_empty_clipboard_toast(tmp_path):
    from axon import attachments
    from axon.cli import _paste_from_clipboard
    watcher = attachments.ClipboardWatcher(tmp_path)
    watcher.poll_once = lambda: []
    state = {"clipwatch": watcher, "toast": ""}

    class _Buf:
        def insert_text(self, s):
            raise AssertionError("空剪贴板不应插入任何文本")

    class _App:
        def invalidate(self):
            pass

    class _Event:
        current_buffer = _Buf()
        app = _App()

    _paste_from_clipboard(state, _Event())
    assert "没有图片/文件" in state["toast"]


def test_paste_handler_without_watcher_is_noop():
    from axon.cli import _paste_from_clipboard

    class _Event:
        current_buffer = None
        app = None

    _paste_from_clipboard({}, _Event())                  # 不抛异常即通过


# ---- ③ 补全器 ----

def _completions(completer, text):
    from prompt_toolkit.document import Document
    doc = Document(text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, None)]


def test_slash_completion_lists_commands():
    from axon.cli import _make_completers
    st = {"project": ".", "project_files": []}
    got = _completions(_make_completers(st), "/")
    assert "/help" in got and "/exit" in got and len(got) >= 8


def test_slash_completion_filters():
    from axon.cli import _make_completers
    st = {"project": ".", "project_files": []}
    got = _completions(_make_completers(st), "/he")
    assert got == ["/help"]


def test_slash_completion_skips_mid_text():
    from axon.cli import _make_completers
    st = {"project": ".", "project_files": []}
    assert _completions(_make_completers(st), "你好 /help") == []


def test_ref_completion_lists_project_files(tmp_path):
    from axon.cli import _make_completers
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "harness.md").write_text("x", encoding="utf-8")
    (proj / "docs" / "a.md").write_text("x", encoding="utf-8")
    (proj / ".workspace").mkdir()
    (proj / ".workspace" / "secret.md").write_text("x", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("x", encoding="utf-8")
    st = {"project": str(proj), "project_files": None}
    got = _completions(_make_completers(st), ">")
    assert ">harness.md" in got and ">docs/a.md" in got
    # v0.4.10：工作区可被引用（排序靠后）；但 .git 等仍排除
    assert any("workspace" in g for g in got)
    assert not any(".git" in g for g in got)


def test_ref_completion_filters_and_limit(tmp_path):
    from axon.cli import _make_completers, scan_project_files
    proj = tmp_path / "proj"
    proj.mkdir()
    for i in range(30):
        (proj / f"f{i:02d}.md").write_text("x", encoding="utf-8")
    files = scan_project_files(proj)
    assert len(files) == 15                              # 最多 15 个
    st = {"project": str(proj), "project_files": files}
    got = _completions(_make_completers(st), ">f0")
    assert got and all(g.startswith(">f0") for g in got)


def test_ref_completion_only_at_line_start():
    from axon.cli import _make_completers
    st = {"project": ".", "project_files": ["a.md"]}
    assert _completions(_make_completers(st), "hello >") == []


def test_scan_project_files_excludes_noise(tmp_path):
    from axon.cli import scan_project_files
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("x", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (proj / ".hidden.md").write_text("x", encoding="utf-8")
    files = scan_project_files(proj)
    assert files == ["src/main.py"]
