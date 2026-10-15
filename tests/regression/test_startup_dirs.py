"""回归：启动目录确保（v0.1.3：workspace 缺失 → CLI 子进程 WinError 267）。"""
from __future__ import annotations


def test_ensure_dirs_creates_missing_workspace(tmp_path):
    from axon.config import ensure_dirs, load_config
    ws = tmp_path / "deep" / "ws"          # 多层不存在目录
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"workspace: {ws}\nproject: {tmp_path}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert not ws.exists()
    ensure_dirs(cfg)
    assert ws.is_dir()


def test_ensure_dirs_creates_missing_project(tmp_path):
    from axon.config import ensure_dirs, load_config
    proj = tmp_path / "Project1"           # --project 传了不存在的目录
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"workspace: {tmp_path / 'ws'}\nproject: {proj}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {proj / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    ensure_dirs(cfg)
    assert proj.is_dir()


def test_ensure_dirs_idempotent(tmp_path):
    from axon.config import ensure_dirs, load_config
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"workspace: {ws}\nproject: {tmp_path}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    ensure_dirs(cfg)
    ensure_dirs(cfg)   # 二次调用不炸
    assert ws.is_dir()


def test_workspace_defaults_under_project(tmp_path):
    """v0.1.5：不配置 workspace 时默认 <project>/.workspace。"""
    from axon.config import load_config
    proj = tmp_path / "Project1"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {proj}\n"
        "model:/n  active_provider: openai\n"
        f"session:/n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.workspace == (proj / ".workspace").resolve()
    assert cfg.project == proj.resolve()


def test_workspace_explicit_still_wins(tmp_path):
    from axon.config import load_config
    ws = tmp_path / "custom_ws"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {tmp_path}\nworkspace: {ws}\n"
        "model:/n  active_provider: openai\n"
        f"session:/n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.workspace == ws.resolve()


def test_ensure_dirs_creates_tmp(tmp_path):
    from axon.config import ensure_dirs, load_config
    ws = tmp_path / "ws"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {tmp_path}\nworkspace: {ws}\n"
        "model:/n  active_provider: openai\n"
        f"session:/n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    from axon.config import program_root
    cfg = load_config(cfg_path)
    ensure_dirs(cfg)
    assert (program_root() / "tmp").is_dir()


def test_workspace_defaults_under_project(tmp_path):
    """v0.1.5：不配置 workspace 时默认 <project>/.workspace。"""
    from axon.config import load_config
    proj = tmp_path / "Project1"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {proj}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.workspace == (proj / ".workspace").resolve()
    assert cfg.project == proj.resolve()


def test_workspace_explicit_still_wins(tmp_path):
    from axon.config import load_config
    ws = tmp_path / "custom_ws"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {tmp_path}\nworkspace: {ws}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.workspace == ws.resolve()


def test_ensure_dirs_creates_tmp_legacy_alias(tmp_path):
    """（v0.1.5 旧断言，保留以记录目录结构变化：tmp 已移程序级）"""
    from axon.config import ensure_dirs, load_config, program_root
    ws = tmp_path / "ws"
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(
        f"project: {tmp_path}\nworkspace: {ws}\n"
        "model:\n  active_provider: openai\n"
        f"session:\n  db: {tmp_path / 's.db'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_path)
    ensure_dirs(cfg)
    assert (program_root() / "tmp").is_dir()
    assert not (ws / ".axon").exists()   # 工作空间内不再建 .axon 隐藏目录
