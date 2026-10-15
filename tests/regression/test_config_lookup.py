"""回归：配置查找链与启动可用性（v0.4.7，用户实测非程序目录启动失败）。

场景：`pip install -e .` 后在 `D:\\Python\\AI\\HarnessPPT` 敲 `axon` 报
`CodebuddyUnavailable: 环境变量未设置: ck_g12kknyp`。
根因：① `load_config` 只在 cwd 找 config.yml → 非程序目录落到坏默认值；
② `ProviderCfg.env_api_key` 默认存的是 **key 的值**而非**变量名** →
   检查时拿 key 值去 `os.environ.get(...)` 必然失败。
"""
from __future__ import annotations

import os

import pytest

from axon.config import (config_search_paths, load_config, resolve_api_key,
                         seed_user_config)


# ---- 查找链 ----

def test_search_paths_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    paths = config_search_paths()
    names = [str(p) for p in paths]
    assert any(n.endswith("home\\config.yml") or "/home/config.yml" in n for n in names)
    # 顺序：cwd → 数据目录 → 安装模板
    assert str(tmp_path / "config.yml") == names[0]


def test_explicit_path_short_circuits(tmp_path):
    p = tmp_path / "my.yml"
    p.write_text("model:\n  active_provider: openai\n", encoding="utf-8")
    assert config_search_paths(p) == [p]


def test_load_config_picks_up_user_config_anywhere(tmp_path, monkeypatch):
    """非程序目录启动也能读到数据目录里的配置（v0.4.7 核心）。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yml").write_text(
        "model:\n  active_provider: openai\n  providers:\n"
        "    openai:\n      default_model: gpt-4o-mini\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                      # 这里没有 config.yml
    monkeypatch.setenv("AXON_HOME", str(home))
    cfg = load_config()
    assert cfg.model.active_provider == "openai"


def test_seed_user_config_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    monkeypatch.setattr("axon.config.Path.cwd", lambda: tmp_path)
    # 模板不存在时静默跳过（不抛异常）
    monkeypatch.setattr(
        "axon.config.Path.exists", lambda self: False if "config.yml" in str(self) else True)
    assert seed_user_config() is None or seed_user_config() is None


# ---- key 解析（两种写法都支持） ----

def test_resolve_api_key_env_name(monkeypatch):
    monkeypatch.setenv("CODEBUDDY_API_KEY", "ck_secret_value")
    assert resolve_api_key("CODEBUDDY_API_KEY") == "ck_secret_value"


def test_resolve_api_key_direct_value():
    """兼容：有人把 key 值直接填进 env_api_key（旧默认值的坑）。"""
    assert resolve_api_key("ck_g12kknypABCDEFGH") == "ck_g12kknypABCDEFGH"
    assert resolve_api_key("sk-abcdef123456") == "sk-abcdef123456"


def test_resolve_api_key_unknown_env_returns_none(monkeypatch):
    monkeypatch.delenv("TOTALLY_MISSING_KEY", raising=False)
    assert resolve_api_key("TOTALLY_MISSING_KEY") is None
    assert resolve_api_key(None) is None
    assert resolve_api_key("") is None


def test_provider_defaults_use_env_names():
    """默认值必须是**变量名**（回归：以前存的是 key 值）。"""
    cfg = load_config("config.yml")
    cb = cfg.model.providers["codebuddy"]
    assert cb.env_api_key == "CODEBUDDY_API_KEY"
    assert cb.env_environment == "CODEBUDDY_INTERNET_ENVIRONMENT"


# ---- 启动失败时的友好指引 ----

def test_missing_key_error_is_actionable(monkeypatch, tmp_path):
    """报错要给出可操作的 3 条路径，而不是只报变量名/泄露 key 值。"""
    from axon.providers import codebuddy as cb_mod
    monkeypatch.delenv("CODEBUDDY_API_KEY", raising=False)
    monkeypatch.delenv("CODEBUDDY_INTERNET_ENVIRONMENT", raising=False)

    class _Cfg:
        class model:
            pass

    from axon.config import load_config
    cfg = load_config("config.yml")
    with pytest.raises(cb_mod.CodebuddyUnavailable) as ei:
        cb_mod.CodebuddyRunner.from_config(cfg, None, None, None, None, None)
    msg = str(ei.value)
    assert "CODEBUDDY_API_KEY" in msg
    assert "set CODEBUDDY_API_KEY" in msg and "--provider openai" in msg
    # 不泄露任何 key 值
    assert "ck_" not in msg
