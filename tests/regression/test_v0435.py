"""回归：`--delproj` 批量删除（v0.4.35，用户需求）。

语法：`1,3,5` / `1-5`（可反序）/ `all` / 与名称混用；兼容中文逗号。
安全：单个→输项目名；批量→输 DELETE；任一 token 未匹配→整体取消（不做部分删除）；
     批量自动排除当前项目；--with-files 需再次 DELETE 且逐个做目录守卫。
"""
from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from axon import cli as C
from axon.providers.base import Message
from axon.sandbox.audit import AuditLog


# ---------------------------------------------------------------- token 展开

def test_expand_tokens_forms():
    projs = [{"name": f"p{i}"} for i in range(1, 6)]
    assert C._expand_batch_tokens("1,3", projs) == ["1", "3"]
    assert C._expand_batch_tokens("2-4", projs) == ["2", "3", "4"]
    assert C._expand_batch_tokens("4-2", projs) == ["2", "3", "4"]      # 反序容错
    assert C._expand_batch_tokens("all", projs) == ["1", "2", "3", "4", "5"]
    assert C._expand_batch_tokens("*", projs) == ["1", "2", "3", "4", "5"]
    assert C._expand_batch_tokens("1，3", projs) == ["1", "3"]          # 中文逗号
    assert C._expand_batch_tokens("1,1,2", projs) == ["1", "2"]         # 去重保序
    assert C._expand_batch_tokens("p3, 1", projs) == ["p3", "1"]        # 名称混用
    assert C._expand_batch_tokens("1,3-4,all", projs) == ["1", "3", "4", "2", "5"]


def test_expand_all_empty_projects():
    assert C._expand_batch_tokens("all", []) == []


# ---------------------------------------------------------------- 端到端

@pytest.fixture
def three_projects(tmp_path, cfg, store, monkeypatch):
    """两个可删项目 + 当前项目（cfg.project 指向 projA）。"""
    audit = AuditLog(store.conn)
    for name in ("projA", "projB", "projC"):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        sid = store.create_session(str(d), "openai", "m")
        store.append_message(sid, Message(role="user", content="hi"))
    cfg.project = tmp_path / "projA"
    app = {"store": store, "cfg": cfg, "audit": audit}
    return app


def _run(app, arg, answers):
    lines: list[str] = []
    it = iter(answers)
    orig = builtins.input
    builtins.input = lambda *a, **k: next(it)
    try:
        C._cmd_delproj(app, None, arg, lambda t="", style="": lines.append(str(t)))
    finally:
        builtins.input = orig
    return lines


def test_batch_delete_two_by_name(three_projects):
    app = three_projects
    lines = _run(app, "projB,projC", ["DELETE"])
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA"}, left
    assert any("将删除以下 2 个项目" in x for x in lines)
    assert any("已删除 2/2 个项目" in x for x in lines)
    # 审计两条
    n = app["store"].conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE rule='project_delete:data'").fetchone()[0]
    assert n == 2


def test_batch_cancelled_on_wrong_confirm(three_projects):
    app = three_projects
    _run(app, "projB,projC", ["delete"])        # 大小写不符
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA", "projB", "projC"}


def test_batch_partial_miss_aborts_all(three_projects):
    app = three_projects
    lines = _run(app, "projB,nosuch", ["DELETE"])
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA", "projB", "projC"}   # 一个都没删
    assert any("未找到项目: nosuch" in x for x in lines)
    assert any("避免部分删除" in x for x in lines)


def test_batch_all_excludes_current(three_projects):
    app = three_projects
    lines = _run(app, "all", ["DELETE"])
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA"}
    assert any("已自动排除当前项目" in x for x in lines)


def test_batch_range_syntax(three_projects, tmp_path, store):
    """`1-3` 按列表序号；结果取决于排序 → 断言"删了 2 个且当前项目保留"。"""
    app = three_projects
    _run(app, "1-3", ["DELETE"])
    left = {p["name"] for p in store.list_projects()}
    assert "projA" in left                      # 当前项目被自动排除
    assert len(left) == 1


def test_single_target_still_needs_project_name(three_projects):
    app = three_projects
    # 输入 DELETE 而非项目名 → 取消
    lines = _run(app, "projB", ["DELETE"])
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA", "projB", "projC"}
    assert any("确认不匹配" in x for x in lines)


def test_single_target_confirm_by_name(three_projects):
    app = three_projects
    _run(app, "projB", ["projB"])
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA", "projC"}


def test_with_files_batch_needs_second_delete(three_projects, tmp_path):
    app = three_projects
    # 第一次 DELETE 通过，第二次输入 n → 目录保留、会话数据已删
    _run(app, "projB,projC --with-files", ["DELETE", "n"])
    assert (tmp_path / "projB").exists()
    assert (tmp_path / "projC").exists()
    left = {p["name"] for p in app["store"].list_projects()}
    assert left == {"projA"}


def test_with_files_batch_moves_dirs(three_projects, tmp_path):
    app = three_projects
    lines = _run(app, "projB,projC --with-files", ["DELETE", "DELETE"])
    assert not (tmp_path / "projB").exists()
    assert not (tmp_path / "projC").exists()
    assert any("已移入回收站 2/2 个目录" in x for x in lines)


def test_list_mode_hints_batch_syntax(three_projects):
    app = three_projects
    lines = _run(app, "", [])
    assert any("批量" in x for x in lines)


def test_empty_project_list(tmp_path, cfg, store):
    app = {"store": store, "cfg": cfg, "audit": AuditLog(store.conn)}
    lines = _run(app, "", [])
    assert any("没有项目记录" in x for x in lines)
