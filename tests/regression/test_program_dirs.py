"""回归：程序级目录与主程序禁写（v0.2.0）。"""
from __future__ import annotations

from pathlib import Path

from axon.config import AXONConfig, ensure_dirs, load_config, program_dir, program_root
from axon.sandbox.gateway import Action


def _write_cfg(tmp_path, extra: str = "") -> Path:
    p = tmp_path / "c.yml"
    p.write_text(
        f"project: {tmp_path}\n"
        "model:\n  active_provider: openai\n"
        f"{extra}",
        encoding="utf-8")
    return p


def test_program_root_is_local_not_synced():
    """v0.4.3：数据目录改到**本地不受管**位置（不再放个人文档，避免云同步/DLP 回滚损坏库）。"""
    from axon.config import is_cloud_synced
    root = program_root()
    assert root.name == "Axon"                      # Windows: %LOCALAPPDATA%\Axon
    assert "Documents" not in str(root)             # 不再落在文档目录
    assert is_cloud_synced(root) is False           # 且不在云同步路径下
    assert str(root).startswith(str(Path.home()))    # 仍在用户目录内


def test_db_defaults_to_program_root(tmp_path):
    cfg = load_config(_write_cfg(tmp_path))
    assert cfg.db_path.parent == program_root()
    assert cfg.snapshots_dir.parent == program_root()
    assert cfg.trash_dir.parent == program_root()


def test_program_dir_is_package_parent():
    import axon
    assert program_dir() == Path(axon.__file__).resolve().parent.parent


def test_program_dir_write_denied(gw_approve):
    """主程序目录：写/改/删一律拒绝（绝对优先，压过 project 信任区）。"""
    target = program_dir() / "evil.txt"
    for kind in ("fs_write", "fs_edit", "fs_delete"):
        d = gw_approve.check(Action(kind=kind, target=str(target)))
        assert d.verdict == "deny" and d.rule == "path:program_dir", kind


def test_program_dir_write_denied_even_when_project_is_program_dir(cfg, audit):
    """project = 主程序目录 的极端用法下，写入仍被拒。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.project = program_dir()
    cfg.workspace = program_dir() / ".workspace"
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="fs_write", target=str(program_dir() / "x.txt")))
    assert d.verdict == "deny"


def test_program_dir_read_allowed(gw_approve):
    """主程序目录允许读取（agent 查看自身源码合理，且无外泄风险）。"""
    d = gw_approve.check(Action(kind="fs_read", target=str(program_dir() / "pyproject.toml")))
    assert d.verdict == "allow" and d.rule == "path:program_dir_read"


def test_shell_write_program_dir_denied(gw_approve):
    prog = program_dir()
    d = gw_approve.check(Action(kind="shell",
                                target=f"echo x > {prog / 'x.txt'}"))
    assert d.verdict == "deny" and d.rule == "command:deny_program_dir"


def test_shell_read_program_dir_allowed(gw_approve):
    d = gw_approve.check(Action(kind="shell",
                                target=f"cat {program_dir() / 'pyproject.toml'}"))
    assert d.verdict == "allow"


def test_ensure_dirs_creates_program_root(tmp_path, monkeypatch):
    """AXON_HOME 指向临时目录 → ensure_dirs 建出程序级结构。"""
    home = tmp_path / "prog_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    cfg = load_config(_write_cfg(tmp_path))
    ensure_dirs(cfg)
    assert home.is_dir()
    assert (home / "tmp").is_dir()
    assert cfg.snapshots_dir.is_dir()
    assert cfg.trash_dir.is_dir()


def test_legacy_session_db_migrated(tmp_path, monkeypatch):
    """旧版项目下 sessions/sessions.db → 程序级（copy，不删原件）。"""
    home = tmp_path / "prog_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    legacy_dir = tmp_path / "sessions"
    legacy_dir.mkdir()
    (legacy_dir / "sessions.db").write_bytes(b"SQLite format 3\x00-legacy")
    cfg = load_config(_write_cfg(tmp_path))
    ensure_dirs(cfg)
    assert cfg.db_path.exists()
    assert (legacy_dir / "sessions.db").exists()   # 原件保留


def test_memory_path_falls_back_when_project_is_program_dir(tmp_path):
    """project == 主程序目录 → AXON.md 落程序级（不被自己的禁写规则挡死）。"""
    from axon.config import load_config
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {program_dir()}\n"
        "model:/n  active_provider: openai\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.memory_path.parent == program_root()


def test_project_inside_program_dir_is_exempt(cfg, audit, tmp_path):
    """v0.2.1 回归修复：project 建在程序目录内时，项目文件写入必须放行。

    背景：用户把项目放在 <程序目录>/Project1，v0.2.0 的"程序目录禁写"
    误伤了项目自身（同会话早些时候成功、之后全被拒）。
    """
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    proj = program_dir() / "ProjectX"
    cfg.project = proj
    cfg.workspace = proj / ".workspace"
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    # 项目内 → 放行
    d = gw.check(Action(kind="fs_write", target=str(proj / "src" / "a.py")))
    assert d.verdict == "allow", d
    # 程序自身文件（非 project 内）→ 仍拒
    d2 = gw.check(Action(kind="fs_write", target=str(program_dir() / "config.yml")))
    assert d2.verdict == "deny" and d2.rule == "path:program_dir"
    # 程序目录下的其它目录（非 project）→ 仍拒
    d3 = gw.check(Action(kind="fs_write", target=str(program_dir() / "OtherDir" / "x.txt")))
    assert d3.verdict == "deny"


def test_shell_in_project_inside_program_dir_exempt(cfg, audit):
    """shell 命令同理豁免（用户在项目里跑脚本/写文件）。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    proj = program_dir() / "ProjectX"
    cfg.project = proj
    cfg.workspace = proj / ".workspace"
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="shell", target=f'echo x > "{proj / "out.txt"}"'))
    assert d.verdict == "allow", d
    d2 = gw.check(Action(kind="shell",
                         target=f'echo x > "{program_dir() / "config.yml"}"'))
    assert d2.verdict == "deny"
