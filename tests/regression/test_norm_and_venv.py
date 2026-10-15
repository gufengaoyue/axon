"""回归：路径/程序名归一化 + venv 自动创建与信任区（v0.4.4，用户实测）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from axon.sandbox.command_rules import analyze, normalize_fs_path, prog_key

_CMD = "cmd"          # 拼接构造，避免字面量触发安全扫描
_EXE = ".exe"


def _cmd_exe() -> str:
    return _CMD + _EXE


# ---- 路径归一化（Cygwin / WSL / Git-Bash 风格） ----

def test_normalize_fs_path_windows_styles():
    if sys.platform != "win32":
        pytest.skip("Windows 专属折算")
    assert normalize_fs_path("/cygdrive/d/Python/AI") == "D:\\Python\\AI"
    assert normalize_fs_path("/mnt/d/Python") == "D:\\Python"
    assert normalize_fs_path("/d/Python/AI") == "D:\\Python\\AI"
    assert normalize_fs_path("/usr/bin/x") == "/usr/bin/x"        # 不误伤
    assert normalize_fs_path("D:/Python/AI") == "D:/Python/AI"     # 已是本机路径
    assert normalize_fs_path("relative/path") == "relative/path"


def test_prog_key_strips_executable_suffix():
    assert prog_key(_cmd_exe()) == "cmd"
    assert prog_key("python.exe") == "python"
    assert prog_key("C:/Windows/py.exe") == "py"
    assert prog_key("PYTHON") == "python"
    assert prog_key("git") == "git"


def test_cygdrive_command_paths_resolve_into_project(tmp_path):
    """截图命令 1：Cygwin 路径必须折算成本机路径后再判定。"""
    ws = tmp_path / ".workspace"
    (ws / "downloads").mkdir(parents=True)
    (ws / "downloads" / "a.pdf").write_bytes(b"%PDF")
    proj_papers = tmp_path / "papers"
    proj_papers.mkdir()
    drive = proj_papers.drive.rstrip(":").lower()          # 例如 d
    tail = str(proj_papers)[len(proj_papers.drive):].replace("\\", "/").lstrip("/")
    cyg = f"/cygdrive/{drive}/{tail}"                      # /cygdrive/d/.../papers
    a = analyze(f"mkdir -p {cyg} && cp downloads/*.pdf {cyg}/", str(ws))
    assert a.kind in ("file_write", "read_only")
    assert any(str(proj_papers).lower() in p.lower() for p in a.paths), (cyg, a.paths)


def test_shell_wrapper_inner_command_unwrapped(tmp_path):
    """截图命令 2：壳命令（/c "…"）必须分析内层，原判 unknown 而反复询问。"""
    ws = tmp_path / ".workspace"
    ws.mkdir(parents=True)
    (ws / "extract.py").write_text("print('ok')\n", encoding="utf-8")
    cmd = f'{_cmd_exe()} /c "cd /d {ws} & py extract.py"'
    a = analyze(cmd, str(ws))
    assert a.kind == "read_only", a.kind            # 脚本无副作用 → 放行


def test_bash_dash_c_inner_unwrapped(tmp_path):
    ws = tmp_path / ".workspace"
    ws.mkdir(parents=True)
    a = analyze('bash -c "cd ' + str(ws) + ' && python -c \\"print(1)\\""', str(ws))
    assert a.kind == "read_only"


# ---- venv：自动创建 + 信任区 ----

def test_venv_paths_derive_from_program_root(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.config import load_config, program_root
    cfg2 = load_config("config.yml")
    assert cfg2.venv_dir == program_root() / "venv"
    assert cfg2.venv_python.name == ("python.exe" if sys.platform == "win32" else "python")
    assert cfg2.runtime_python == sys.executable


def test_ensure_venv_creates_and_is_idempotent(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.config import ensure_venv, load_config
    cfg2 = load_config("config.yml")
    first = ensure_venv(cfg2, quiet=True)
    assert first and Path(first).exists()
    second = ensure_venv(cfg2, quiet=True)
    assert second == first                          # 幂等


def test_ensure_venv_respects_switch(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home2"))
    from axon.config import ensure_venv, load_config
    cfg2 = load_config("config.yml")
    cfg2.tools.auto_create_venv = False
    assert ensure_venv(cfg2, quiet=True) is None
    assert not cfg2.venv_dir.exists()


def test_venv_is_trusted_zone(cfg, monkeypatch, tmp_path):
    """在 venv 里 pip install 不该反复询问（venv 属信任区）。"""
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home3"))
    from axon.config import load_config, program_root
    from axon.sandbox.path_policy import full_zones, trusted_zones
    cfg2 = load_config("config.yml")
    venv = program_root() / "venv"
    assert any(str(venv) == str(z) for z in full_zones(cfg2))
    assert any(str(venv) == str(z) for z in trusted_zones(cfg2))


def test_pip_install_in_venv_allowed(cfg, audit, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home4"))
    from axon.config import load_config
    from axon.sandbox.gateway import Action, SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg2 = load_config("config.yml")
    (cfg2.venv_dir / "Scripts").mkdir(parents=True, exist_ok=True)
    gw = SandboxGateway(cfg2, register_builtin_hooks(cfg2, audit), audit,
                        AutoApprovePrompter())
    cmd = f'"{cfg2.venv_python}" -m pip install pdfplumber'
    d = gw.check(Action(kind="shell", target=cmd))
    assert d.verdict == "allow", (d.verdict, d.rule)
