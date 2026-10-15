"""回归：命令规则 + 命令安全分析器（v0.1.5 重写）。

新语义：
- 读/搜索类命令（含复合命令）→ 免询问；deny 路径仍硬拦
- 工作空间（全权限区）内：写/删/跑脚本免询问；批量删除超阈值（100）仍询问
- 信任区（+项目目录）内：文件写/删免确认
- 区外：读放行；写/删/不透明命令 → fail-closed 询问
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from axon.sandbox.command_rules import analyze
from axon.sandbox.gateway import Action


def _check(gw, cmd: str):
    return gw.check(Action(kind="shell", target=cmd))


# ---- 分析器单元 ----

def test_analyze_read_only_compound():
    """用户实测的复合只读命令：cd && dir | findstr | head → read_only。"""
    cmd = ('cd /d "D:\\Python\\AI\\Project1" && dir /b /s /a-d 2>nul | '
           'findstr /v /i /c:"node_modules __pycache__ .git .venv" | head -100')
    a = analyze(cmd, "C:/ws")
    assert a.kind == "read_only"
    assert not a.has_redirection          # 2>nul 不算写文件
    assert len(a.atoms) == 4


def test_analyze_redirection_is_write():
    a = analyze("echo hi > out.txt", "C:/ws")
    assert a.kind == "file_write"
    assert any("out.txt" in p for p in a.paths)


def test_analyze_powershell_unwrap():
    a = analyze('powershell -Command "Get-ChildItem -Recurse -Name"', "C:/ws")
    assert a.kind == "read_only"


def test_analyze_cmd_unwrap():
    a = analyze('cmd /c "dir /b"', "C:/ws")
    assert a.kind == "read_only"


def test_analyze_git_read_vs_write():
    assert analyze("git status --short", "C:/ws").kind == "read_only"
    assert analyze("git diff HEAD~1", "C:/ws").kind == "read_only"
    assert analyze("git push origin main", "C:/ws").kind == "system_mutate"
    assert analyze("git commit -m x", "C:/ws").kind == "system_mutate"


def test_analyze_delete_counts():
    a = analyze("rm a.txt b.txt", "C:/ws")
    assert a.kind == "delete" and a.delete_count == 2


def test_analyze_delete_recursive_dir(tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    for i in range(5):
        (d / f"f{i}.txt").write_text("x")
    (d / "sub" / "g.txt").write_text("x")
    a = analyze(f"rm -rf {d}", str(tmp_path))
    assert a.kind == "delete" and a.delete_count == 6


def test_analyze_find_delete_is_write():
    assert analyze("find . -name '*.tmp' -delete", "C:/ws").kind == "file_write"


def test_analyze_substitution_is_unknown():
    assert analyze("echo $(rm -rf /)", "C:/ws").kind == "unknown"


def test_analyze_cd_tracks_base(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    a = analyze(f'cd /d "{proj}" && rm foo.txt', str(tmp_path / "ws"))
    assert a.kind == "delete"
    assert a.paths and all(str(proj) in p for p in a.paths)


# ---- Hook 决策 ----

def test_read_only_compound_allowed(gw_approve, ws, tmp_path):
    (ws / "a.txt").write_text("x", encoding="utf-8")
    cmd = (f'cd /d "{ws}" && dir /b /s /a-d 2>nul | findstr /v /i /c:".git" | head -50')
    d = _check(gw_approve, cmd)
    assert d.verdict == "allow" and d.rule == "command:read_only"


def test_read_only_denied_path(gw_approve):
    d = _check(gw_approve, "cat ~/.ssh/id_rsa")
    assert d.verdict == "deny" and d.rule == "command:deny_path"


def test_workspace_delete_allowed(gw_approve, ws):
    (ws / "gone.txt").write_text("x", encoding="utf-8")
    d = _check(gw_approve, "rm gone.txt")
    assert d.verdict == "allow" and d.rule == "command:zone_delete"


def test_workspace_python_script_allowed(gw_approve, ws):
    """v0.2.2：脚本经危险信号扫描（print/import → 安全）→ 直接放行。"""
    (ws / "build.py").write_text("print(1)", encoding="utf-8")
    d = _check(gw_approve, "python build.py")
    assert d.verdict == "allow" and d.rule in ("command:read_only",
                                              "command:workspace_full")


def test_python_inline_code_with_remove_in_workspace_allowed(gw_approve, ws):
    """v0.2.2：单文件删除且在工作空间内 → 放行（不涉及批量删除）。"""
    d = _check(gw_approve, "python -c \"import os; os.remove('x')\"")
    assert d.verdict == "allow" and d.rule == "command:zone_delete"


def test_python_inline_subprocess_asks(gw_approve):
    """shell 逃逸（subprocess/os.system）→ 保持不透明 → 询问。"""
    d = _check(gw_approve, 'python -c "import subprocess; subprocess.run([\'ls\'])"')
    assert d.verdict == "ask"


def test_python_rmtree_large_dir_asks(gw_approve, ws):
    """rmtree 大目录 → 展开计数超阈值 → 询问（防低估绕过）。"""
    big = ws / "build"
    big.mkdir()
    for i in range(120):
        (big / f"f{i}.o").write_text("x", encoding="utf-8")
    d = _check(gw_approve, "python -c \"import shutil; shutil.rmtree('build')\"")
    assert d.verdict == "ask" and "batch_delete" in d.rule


def test_python_version_and_print_allowed(gw_approve):
    """用户截图场景：--version / import+print → 直接放行。"""
    assert _check(gw_approve, "python --version").verdict == "allow"
    assert _check(gw_approve,
                  'python -c "import pptx; print(pptx.__version__)"').verdict == "allow"


def _narrowed_gw(cfg, audit):
    """把 project 收窄到子目录，使 tmp_path 本身成为"区外"（测试辅助）。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.project = cfg.project / "proj"
    cfg.project.mkdir(exist_ok=True)
    return SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                          AutoApprovePrompter())


def test_write_outside_asks(cfg, audit, tmp_path):
    gw = _narrowed_gw(cfg, audit)
    d = _check(gw, f"echo hi > {tmp_path / 'outside.txt'}")
    assert d.verdict == "ask" and d.rule == "command:write_outside"


def test_delete_outside_asks(cfg, audit, tmp_path):
    gw = _narrowed_gw(cfg, audit)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    d = _check(gw, f"rm {outside}")
    assert d.verdict == "ask" and d.rule == "command:delete_outside"


def test_write_in_workspace_allowed(gw_approve, ws):
    d = _check(gw_approve, "echo hi > out.txt")
    assert d.verdict == "allow" and d.rule == "command:workspace_full"


def test_git_push_asks(gw_approve):
    d = _check(gw_approve, "git push origin main")
    assert d.verdict == "ask"


def test_batch_delete_over_threshold_asks(gw_approve, ws):
    """101 个文件超阈值（默认 100）→ 即使在工作空间也询问。

    只测判定（check），不真执行删除——执行会触发外层沙箱的批量删除拦截。
    """
    for i in range(101):
        (ws / f"f{i}.txt").write_text("x", encoding="utf-8")
    cmd = "rm " + " ".join(f"f{i}.txt" for i in range(101))
    d = _check(gw_approve, cmd)
    assert d.verdict == "ask" and "batch_delete" in d.rule


def test_batch_delete_under_threshold_allowed(gw_approve, ws):
    """50 个文件在阈值内 → 工作空间内直接放行（只测判定）。"""
    for i in range(50):
        (ws / f"f{i}.txt").write_text("x", encoding="utf-8")
    cmd = "rm " + " ".join(f"f{i}.txt" for i in range(50))
    d = _check(gw_approve, cmd)
    assert d.verdict == "allow" and d.rule == "command:zone_delete"


def test_deny_rule_wins(gw_approve):
    assert _check(gw_approve, "rm -rf /").verdict == "deny"


def test_bypass_whitelist(cfg, audit, ws):
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.command_bypass_whitelist = ["docker"]
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = _check(gw, "docker ps")
    assert d.verdict == "allow" and d.rule.startswith("command:bypass")


def test_unknown_command_default_ask(gw_approve):
    d = _check(gw_approve, "some-unknown-binary --flag")
    assert d.verdict == "ask" and d.rule == "command:default_ask"


# ---- v0.3.1：cmd 开关误判与 2>&1 切分（用户实测两条命令不再询问） ----

def test_redirect_strip_prevents_amp_split():
    """`2>&1` 的 & 不再参与原子切分（否则 `dir … 2>` 被当写文件）。"""
    from axon.sandbox.command_rules import strip_void_redirects, _split_atoms
    cmd = "dir /s /b output assets scripts 2>&1"
    assert _split_atoms(strip_void_redirects(cmd)) == ["dir /s /b output assets scripts"]
    assert strip_void_redirects("x 2>nul >nul &>nul 2>/dev/null") .count(">") == 0


def test_dir_switches_not_paths(gw_approve, ws):
    """`/s` `/b` 曾是 `D://s`、`D://b`（盘根）→ 误判信任区外写。"""
    (ws / "output").mkdir(exist_ok=True)
    (ws / "assets").mkdir(exist_ok=True)
    d = _check(gw_approve, "dir /s /b output assets scripts 2>&1")
    assert d.verdict == "allow" and d.rule == "command:read_only"


def test_findstr_c_switch_not_path(gw_approve, ws):
    """`findstr /C:"slide"` 曾是 `C:slide`（盘根）→ 误判。"""
    (ws / "verify_layout.py").write_text("print(1)", encoding="utf-8")
    d = _check(gw_approve,
               'python verify_layout.py 2>&1 | findstr /C:"slide"')
    assert d.verdict == "allow"


def test_read_only_query_commands_allowed(gw_approve, ws):
    for cmd in ("type README.md", "findstr /s /i slide *.txt", "where python",
                "tree /f", "more README.md"):
        assert _check(gw_approve, cmd).verdict == "allow", cmd


def test_delete_switch_still_delete(gw_approve, ws):
    """开关被正确排除后，del 仍按删除判定（工作空间内 → 放行）。"""
    d = _check(gw_approve, "del /q old.tmp")
    assert d.verdict == "allow" and d.rule == "command:zone_delete"


def test_outside_write_still_asks(gw_approve):
    """安全边界强化：写系统目录 → deny（v0.3.1 起系统目录硬拦）。"""
    d = _check(gw_approve, "copy a.txt C://Windows//x.txt")
    assert d.verdict == "deny"


def test_outside_write_non_system_still_asks(cfg, audit, tmp_path):
    """非系统/非盘根的信任区外写入 → 仍为询问（收紧 project 以制造"区外"）。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.project = tmp_path / "proj"
    cfg.project.mkdir(exist_ok=True)
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                        AutoApprovePrompter())
    outside = tmp_path / "other" / "x.txt"
    d = _check(gw, f"echo hi > {outside}")
    assert d.verdict == "ask" and d.rule == "command:write_outside"


# ---- v0.3.1：盘符根目录 / 系统目录重点审查（用户补充要求） ----

def test_fs_root_delete_denied_high(gw_approve):
    d = gw_approve.check(__import__("axon.sandbox.gateway", fromlist=["Action"]).Action(
        kind="fs_delete", target="D://"))
    assert d.verdict == "deny" and d.rule == "path:fs_root" and d.severity == "high"


def test_fs_root_child_write_asks_high(gw_approve):
    d = gw_approve.check(__import__("axon.sandbox.gateway", fromlist=["Action"]).Action(
        kind="fs_write", target=r"D:/out.txt"))
    assert d.verdict == "ask" and d.rule == "path:fs_root_child" and d.severity == "high"


def test_system_dir_write_denied(gw_approve):
    for target in (r"C:/ProgramData/x.txt", r"C:/Windows/a.dll"):
        d = gw_approve.check(__import__("axon.sandbox.gateway", fromlist=["Action"]).Action(
            kind="fs_write", target=target))
        assert d.verdict == "deny", target


def test_fs_root_read_allowed(gw_approve):
    """读盘根（列目录）无害 → 放行。"""
    d = gw_approve.check(__import__("axon.sandbox.gateway", fromlist=["Action"]).Action(
        kind="fs_read", target="D://"))
    assert d.verdict == "allow"


def test_shell_root_write_asks_high(gw_approve):
    d = _check(gw_approve, "echo hi > D://out.txt")
    assert d.verdict == "ask" and d.rule == "command:fs_root_child" and d.severity == "high"


def test_shell_dir_root_allowed(gw_approve):
    assert _check(gw_approve, "dir D://").verdict == "allow"
