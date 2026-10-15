"""回归：v0.4.9 三项（用户连续实测）。

① 收编键扩展：Ctrl+G / Alt+V / F2 / Ctrl+V（Ctrl+V 在 Windows Terminal 被系统接管，
   必须提供终端不吞的键，否则"按了没反应"）
② 启动横幅改为 `/` 与 `>` 的使用说明（旧文案还在宣传已移除的"自动收编"）
③ `>` 补全与引用支持**目录**
"""
from __future__ import annotations

from pathlib import Path

from axon.cli import scan_project_entries, scan_project_files, _make_completers
from axon.attachments import parse_references


def _comps(state, text):
    from prompt_toolkit.document import Document
    doc = Document(text, cursor_position=len(text))
    return [c.text for c in _make_completers(state).get_completions(doc, None)]


# ---- ③ 目录支持 ----

def _mkproj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "PPT").mkdir()
    (proj / "docs" / "a.md").write_text("x", encoding="utf-8")
    (proj / "PPT" / "p.pptx").write_bytes(b"x")
    (proj / "harness.md").write_text("x", encoding="utf-8")
    (proj / ".workspace").mkdir()
    (proj / ".workspace" / "s.md").write_text("x", encoding="utf-8")
    return proj


def test_entries_include_dirs_and_files(tmp_path):
    proj = _mkproj(tmp_path)
    got = scan_project_entries(proj)
    assert "docs/" in got and "PPT/" in got            # 目录带尾斜杠
    assert "docs/a.md" in got and "harness.md" in got
    # v0.4.10：工作区保留在补全里（排序靠后）；.git/其他隐藏项仍排除
    assert any(".workspace" in e for e in got)
    assert not any(".git" in e for e in got)


def test_files_alias_returns_only_files(tmp_path):
    proj = _mkproj(tmp_path)
    got = scan_project_files(proj)
    assert got and all(not e.endswith("/") for e in got)
    assert "harness.md" in got


def test_ref_completion_offers_dirs(tmp_path):
    proj = _mkproj(tmp_path)
    st = {"project": str(proj), "project_entries": None}
    got = _comps(st, ">")
    assert ">docs/" in got and ">PPT/" in got
    assert ">docs/a.md" in got


def test_ref_completion_dir_prefix_drills_down(tmp_path):
    proj = _mkproj(tmp_path)
    st = {"project": str(proj), "project_entries": scan_project_entries(proj)}
    got = _comps(st, ">docs/")
    assert got == [">docs/a.md"]


def test_parse_references_accepts_directory(tmp_path):
    proj = _mkproj(tmp_path)
    text, refs = parse_references(">docs\n>harness.md\n看看这些", proj)
    assert len(refs) == 2
    assert refs[0][1] == str(proj / "docs")            # 目录也收编
    assert Path(refs[0][1]).is_dir()
    assert "看看这些" in text and ">docs" not in text


def test_attachment_note_marks_directory(tmp_path):
    """目录引用在给模型的清单里标注（目录），模型可据此遍历。"""
    proj = _mkproj(tmp_path)
    _t, refs = parse_references(">docs", proj)
    note = "\n".join(f"- {r} → {p}" + ("（目录）" if Path(p).is_dir() else "")
                     for r, p in refs)
    assert "（目录）" in note


# ---- ② 横幅文案 ----

def test_banner_mentions_slash_and_gt():
    src = Path(__file__).resolve().parents[2] / "axon" / "cli.py"
    text = src.read_text(encoding="utf-8")
    assert "输入 / 补全命令" in text
    assert "输入 > 补全" in text
    assert "Ctrl+G" in text                            # 收编键说明
    # 旧文案（宣传已移除的"自动收编"）必须下线
    assert "截图或复制文件会被自动收编" not in text


# ---- ① 收编键 ----

def test_paste_keys_registered():
    """c-v / c-g / Alt+V / F2 四个键都走同一收编 handler。"""
    src = Path(__file__).resolve().parents[2] / "axon" / "cli.py"
    text = src.read_text(encoding="utf-8")
    for key in ('@kb.add("c-v"', '@kb.add("c-g"',
                '@kb.add("escape", "v"', '@kb.add("f2"'):
        assert key in text, key
    assert "_paste_from_clipboard(state, event)" in text


# ---- v0.4.10：工作区纳入 `>` 补全（排序靠后） ----

def test_workspace_included_but_ranked_last(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".workspace" / "downloads").mkdir(parents=True)
    (proj / ".workspace" / "tmp" / "attach").mkdir(parents=True)
    (proj / ".workspace" / "downloads" / "a.pdf").write_bytes(b"%PDF")
    (proj / ".workspace" / "tmp" / "attach" / "shot.png").write_bytes(b"x")
    (proj / "docs").mkdir()
    (proj / "docs" / "guide.md").write_text("x", encoding="utf-8")
    (proj / "README.md").write_text("x", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("x", encoding="utf-8")
    got = scan_project_entries(proj, limit=20)
    assert ".workspace/" in got and ".workspace/downloads/a.pdf" in got
    assert ".workspace/tmp/attach/shot.png" in got
    # 项目资产全部排在任一工作区条目之前
    first_ws = next(i for i, e in enumerate(got) if ".workspace" in e)
    assert got.index("docs/guide.md") < first_ws
    assert got.index("README.md") < first_ws
    assert not any(".git" in e for e in got)


def test_workspace_ref_parses(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".workspace" / "downloads").mkdir(parents=True)
    pdf = proj / ".workspace" / "downloads" / "a.pdf"
    pdf.write_bytes(b"%PDF")
    _t, refs = parse_references(">.workspace/downloads/a.pdf\n总结这篇", proj)
    assert refs and refs[0][1] == str(pdf.resolve())
