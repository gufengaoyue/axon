"""回归：删除项目 /delproj（v0.2.7，破坏性操作安全约定）。"""
from __future__ import annotations

import pytest

from axon.providers.base import Message
from axon.session.db import SessionStore, connect


@pytest.fixture
def two_projects(store):
    a = store.create_session("D:/proj/Alpha", "openai", "m")
    b = store.create_session("D:/proj/Beta", "openai", "m")
    store.append_message(a, Message(role="user", content="Alpha 任务"))
    store.append_message(a, Message(role="assistant", content="Alpha 回答"))
    store.append_message(b, Message(role="user", content="Beta 任务"))
    return a, b


def test_list_projects_aggregates(store, two_projects):
    rows = store.list_projects()
    names = {r["name"] for r in rows}
    assert names == {"Alpha", "Beta"}
    alpha = next(r for r in rows if r["name"] == "Alpha")
    assert alpha["sessions"] == 1 and alpha["messages"] == 2
    assert len(alpha["sid_prefix"]) == 8


def test_delete_project_data_only_target(store, two_projects):
    a, b = two_projects
    stats = store.delete_project_data("D:/proj/Alpha")
    assert stats["sessions"] == 1 and stats["messages"] == 2
    assert store.list_projects()[0]["name"] == "Beta"       # Alpha 没了
    assert store.load_messages(b)                            # Beta 完好
    assert store.get_session(b) is not None


def test_delete_project_data_missing(store):
    assert store.delete_project_data("D:/nope")["sessions"] == 0


# ---- CLI 层：列表 / 强确认 / 拒绝危险目标 ----

class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text, style=""):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _app(store, cfg, audit):
    return {"store": store, "cfg": cfg, "audit": audit}


def test_delproj_list(store, cfg, audit, two_projects):
    from axon.cli import _cmd_delproj
    out = _Out()
    _cmd_delproj(_app(store, cfg, audit), None, "", out)
    assert "Alpha" in out.text and "Beta" in out.text and "会话" in out.text


def test_delproj_requires_exact_name(store, cfg, audit, two_projects, monkeypatch):
    from axon.cli import _cmd_delproj
    out = _Out()
    monkeypatch.setattr("builtins.input", lambda *a, **k: "alpha-wrong")
    _cmd_delproj(_app(store, cfg, audit), None, "Alpha", out)
    assert "已取消" in out.text
    assert any(p["name"] == "Alpha" for p in store.list_projects())   # 未被删除


def test_delproj_deletes_after_confirmation(store, cfg, audit, two_projects, monkeypatch):
    from axon.cli import _cmd_delproj
    out = _Out()
    monkeypatch.setattr("builtins.input", lambda *a, **k: "Alpha")
    _cmd_delproj(_app(store, cfg, audit), None, "Alpha", out)
    assert "已删除会话数据" in out.text
    assert not any(p["name"] == "Alpha" for p in store.list_projects())


def test_delproj_match_by_session_prefix(store, cfg, audit, two_projects, monkeypatch):
    from axon.cli import _cmd_delproj
    a, _ = two_projects
    out = _Out()
    monkeypatch.setattr("builtins.input", lambda *a, **k: "Alpha")
    _cmd_delproj(_app(store, cfg, audit), None, a[:8], out)
    assert "已删除会话数据" in out.text


def test_delproj_with_files_requires_second_confirm(store, cfg, audit, two_projects,
                                                    monkeypatch, tmp_path):
    """--with-files 需二次输入 DELETE；未输入则只删数据、不动文件。"""
    from axon.cli import _cmd_delproj
    proj = tmp_path / "Alpha"
    proj.mkdir()
    (proj / "keep.txt").write_text("x", encoding="utf-8")
    # 把 Alpha 的项目目录改到真实路径，便于观察文件
    store.conn.execute("UPDATE sessions SET project_dir=? WHERE project_dir='D:/proj/Alpha'",
                       (str(proj),))
    store.conn.commit()
    out = _Out()
    answers = iter([str(proj.name), "no-delete"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    _cmd_delproj(_app(store, cfg, audit), None, f"{proj} --with-files", out)
    assert (proj / "keep.txt").exists()          # 二次确认未通过 → 文件保留
    assert "已删除会话数据" in out.text


def test_delproj_refuses_dangerous_target(store, cfg, audit, monkeypatch, tmp_path):
    """盘根/用户根/程序目录 → 拒绝移入回收站（即使 --with-files）。"""
    from axon.cli import _cmd_delproj
    from axon.config import program_dir
    sid = store.create_session(str(program_dir()), "openai", "m")
    store.append_message(sid, Message(role="user", content="x"))
    out = _Out()
    answers = iter([program_dir().name, "DELETE"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    _cmd_delproj(_app(store, cfg, audit), None,
                 f"{program_dir()} --with-files", out)
    assert "拒绝" in out.text
    assert program_dir().exists()


# ---- --delproj 命令行模式（v0.2.8，用户要求） ----

def test_run_delproj_mode_lists_without_api_key(capsys, cfg, monkeypatch, tmp_path):
    """命令行模式只依赖会话库：无需 API key、不进 REPL、不需要 workspace 存在。"""
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.cli import run_delproj_mode
    rc = run_delproj_mode(cfg, None, "__list__")
    out = capsys.readouterr().out
    assert rc == 0
    assert "项目列表" in out or "(没有项目记录)" in out


def test_run_delproj_mode_deletes_by_index(capsys, cfg, monkeypatch, tmp_path):
    """支持按列表序号删除（同名项目场景）。"""
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.cli import run_delproj_mode
    from axon.session.db import SessionStore, connect
    store = SessionStore(connect(cfg.db_path))
    sid = store.create_session(str(tmp_path / "Alpha"), "openai", "m")
    store.append_message(sid, Message(role="user", content="hi"))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "Alpha")
    rc = run_delproj_mode(cfg, None, "1")
    out = capsys.readouterr().out
    assert rc == 0 and "已删除会话数据" in out
    assert store.list_projects() == []


def test_run_delproj_mode_name_mismatch_keeps_data(capsys, cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.cli import run_delproj_mode
    from axon.session.db import SessionStore, connect
    store = SessionStore(connect(cfg.db_path))
    store.create_session(str(tmp_path / "Beta"), "openai", "m")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "wrong-name")
    run_delproj_mode(cfg, None, "Beta")
    assert any(p["name"] == "Beta" for p in store.list_projects())


def test_cli_parser_has_delproj_flags():
    import argparse
    from axon.cli import main
    import inspect
    src = inspect.getsource(main)
    assert '"--delproj"' in src and '"--with-files"' in src
    # argparse 层：--delproj 不带值 → __list__
    parser_src_ok = "--delproj" in src
    assert parser_src_ok
