"""回归：v0.4.26 openai 路异常可读化（用户实测裸 traceback）。

现象：`axon --provider openai --model qwen3.7-flash` →
`raise APIConnectionError(request=request) from err`
`openai.APIConnectionError: Connection error.` 以**裸 traceback** 泼屏。

根因：openai 路的 `provider.chat` 异常在 `AgentLoop._run` 无人捕获
（只捕 KeyboardInterrupt），`main()` 也无顶层兜底 → 一路裸奔。
而 qwen3.7-flash 不是 OpenAI 官方模型，端点却仍是配置里的
api.openai.com（国内网络直连必然连接失败）——**端点/模型错配**。

修复：
- `_run` 的 chat 消费处 `except Exception` → `provider_error` TurnResult
  （保留已产出的部分文本；KeyboardInterrupt 不受影响，仍走 user_interrupt）
- `_friendly_provider_error()` 按异常类型给可操作中文话术：连接错误指出
  当前 base_url、「端点 vs 模型名」错配（qwen 系 → DashScope 兼容端点）、
  代理环境变量陷阱（httpx 默认读 HTTP(S)_PROXY）；401/404/429/400 分型
- 新增 `--base-url` 临时覆盖端点（换服务商试模型不必改 config.yml）
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest
import openai
from rich.console import Console

from axon.agent.loop import TurnResult, _friendly_provider_error
from axon.cli import _print_turn_result
from axon.config import load_config
from axon.providers.base import StreamEvent

from tests.regression.conftest import make_loop

_ROOT = Path(__file__).resolve().parents[2]
_SRC_LOOP = (_ROOT / "axon" / "agent" / "loop.py").read_text(encoding="utf-8")
_SRC_CLI = (_ROOT / "axon" / "cli.py").read_text(encoding="utf-8")


def _conn_error(url: str = "https://api.openai.com/v1/chat/completions") -> Exception:
    """真实形态的 openai.APIConnectionError（与用户实测同型）。"""
    return openai.APIConnectionError(request=httpx.Request("POST", url))


class _RaisingProvider:
    """脚本化 provider：首个 next() 即抛异常（与真实 provider 建连时机一致）。"""

    name = "fake"

    def __init__(self, exc: Exception, pre_text: str = ""):
        self._exc = exc
        self._pre_text = pre_text

    def chat(self, messages, tools, *, model: str, timeout: float = 180.0):
        if self._pre_text:
            yield StreamEvent(kind="text", text=self._pre_text)
        raise self._exc
        yield  # pragma: no cover - 仅为成为生成器


# ============================================================ loop 层转换

def test_loop_converts_connection_error(cfg, gw_approve, registry, ctx_builder,
                                        compactor, verifier, store, session):
    """连接错误 → provider_error + 排查话术（不再裸 traceback）。"""
    session.model = "qwen3.7-flash"
    provider = _RaisingProvider(_conn_error())
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "你好")
    assert result.stop_reason == "provider_error"
    assert "APIConnectionError" in result.note
    assert "端点" in result.note                       # 指出当前 base_url
    assert "qwen3.7-flash" in result.note              # 指出模型名


def test_loop_preserves_text_before_error(cfg, gw_approve, registry, ctx_builder,
                                          compactor, verifier, store, session):
    """流式中途抛错：已产出的部分文本不丢。"""
    provider = _RaisingProvider(_conn_error(), pre_text="前半段输出")
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "你好")
    assert result.stop_reason == "provider_error"
    assert result.final_text == "前半段输出"


def test_loop_keyboard_interrupt_not_swallowed(cfg, gw_approve, registry,
                                               ctx_builder, compactor, verifier,
                                               store, session):
    """KeyboardInterrupt 是 BaseException——不被 except Exception 吃掉，
    仍走外层 except → user_interrupt（中断语义不被错误处理破坏）。"""
    provider = _RaisingProvider(KeyboardInterrupt())
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "你好")
    assert result.stop_reason == "user_interrupt"


def test_loop_provider_error_persists_unfinished(cfg, gw_approve, registry,
                                                 ctx_builder, compactor, verifier,
                                                 store, session):
    """provider_error 属未完成原因 → <interrupted_task> 落库（resume 可续）。"""
    provider = _RaisingProvider(_conn_error())
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    loop.run(session, "帮我看一下这个项目")
    msgs = store.load_messages(session.id)
    assert any(m.role == "user" and "<interrupted_task>" in (m.content or "")
               for m in msgs)


# ============================================================ 话术分型

def test_friendly_connection_error_mismatch_hint(cfg):
    """qwen 系模型 + api.openai.com 端点 → 明确告知错配并给 DashScope 端点。"""
    cfg.model.providers["openai"].base_url = "https://api.openai.com/v1"
    msg = _friendly_provider_error(_conn_error(), cfg, "qwen3.7-flash")
    assert "APIConnectionError" in msg
    assert "api.openai.com" in msg                     # 当前端点
    assert "端点/模型不匹配" in msg
    assert "dashscope.aliyuncs.com/compatible-mode/v1" in msg


def test_friendly_connection_error_no_false_mismatch(cfg):
    """端点本身就是 DashScope → 不报错配（避免误报）。"""
    cfg.model.providers["openai"].base_url = \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    msg = _friendly_provider_error(_conn_error(), cfg, "qwen3.7-flash")
    assert "端点/模型不匹配" not in msg
    assert "dashscope" in msg                          # 仍显示当前端点


def test_friendly_connection_error_detects_proxy(cfg, monkeypatch):
    """代理环境变量陷阱：检测到即提示清空（httpx 默认读 HTTP(S)_PROXY）。"""
    cfg.model.providers["openai"].base_url = "https://api.openai.com/v1"
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:57450")
    msg = _friendly_provider_error(_conn_error(), cfg, "qwen3.7-flash")
    assert "代理环境变量" in msg
    assert "127.0.0.1:57450" in msg


def test_friendly_auth_error(cfg):
    exc = openai.AuthenticationError(
        "Incorrect API key provided",
        response=httpx.Response(401, request=httpx.Request(
            "POST", "https://api.openai.com/v1/chat/completions")),
        body=None)
    msg = _friendly_provider_error(exc, cfg, "gpt-4o-mini")
    assert "401" in msg and "OPENAI_API_KEY" in msg


def test_friendly_not_found_error(cfg):
    exc = openai.NotFoundError(
        "model not found",
        response=httpx.Response(404, request=httpx.Request(
            "POST", "https://api.openai.com/v1/chat/completions")),
        body=None)
    msg = _friendly_provider_error(exc, cfg, "no-such-model")
    assert "404" in msg and "no-such-model" in msg


def test_friendly_rate_limit_error(cfg):
    exc = openai.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request(
            "POST", "https://api.openai.com/v1/chat/completions")),
        body=None)
    msg = _friendly_provider_error(exc, cfg, "gpt-4o-mini")
    assert "429" in msg and "额度" in msg


def test_friendly_generic_error(cfg):
    msg = _friendly_provider_error(RuntimeError("boom"), cfg, "m")
    assert "RuntimeError: boom" in msg
    assert "模型调用异常" in msg


# ============================================================ 页脚展示

def test_provider_error_note_shown_in_footer(capsys):
    r = TurnResult("", "provider_error", 1, 0,
                   note="APIConnectionError: Connection error.\n① 端点：...")
    _print_turn_result(Console(width=120), r)
    out = capsys.readouterr().out
    assert "模型调用失败" in out
    assert "APIConnectionError" in out                 # note 原文上屏


# ============================================================ --base-url 覆盖

def test_base_url_override(tmp_path):
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
model:
  active_provider: openai
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(p, overrides={
        "active_provider": "openai", "model": "qwen3.7-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    assert cfg.active_model() == "qwen3.7-flash"
    assert cfg.active_provider_cfg().base_url == \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_base_url_override_wins_over_yaml(tmp_path):
    """config.yml 里写死的 base_url 被 --base-url 覆盖（用户场景：
    yml 是 api.openai.com，命令行临时切到 DashScope）。"""
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
model:
  active_provider: openai
  providers:
    openai:
      base_url: https://api.openai.com/v1
      default_model: gpt-4o-mini
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(p, overrides={
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    assert cfg.active_provider_cfg().base_url == \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"


# ============================================================ base_url 环境变量间接

def test_resolve_base_url_passthrough_url():
    from axon.config import resolve_base_url
    assert resolve_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert resolve_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    assert resolve_base_url("") == ""
    assert resolve_base_url("localhost:8000") == "localhost:8000"   # 小写主机名不误判


def test_resolve_base_url_env_name(monkeypatch):
    """用户实测根因：`base_url: OPENAI_BASE_URL` 旧实现不展开 → 字面量当
    主机名 → DNS 失败 → APIConnectionError。现在按变量名展开。"""
    from axon.config import resolve_base_url
    monkeypatch.setenv("OPENAI_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert resolve_base_url("OPENAI_BASE_URL") == \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_resolve_base_url_unset_env_passthrough(monkeypatch):
    """变量未设置 → 原样返回（不在加载期 fail-fast——配置加载不该被单字段
    炸掉；由 OpenAICompatProvider 构造时检查，见下）。"""
    from axon.config import resolve_base_url, looks_like_unset_env_name
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert resolve_base_url("OPENAI_BASE_URL") == "OPENAI_BASE_URL"
    assert looks_like_unset_env_name("OPENAI_BASE_URL") is True
    assert looks_like_unset_env_name("https://api.openai.com/v1") is False
    assert looks_like_unset_env_name("localhost:8000") is False


def test_provider_rejects_unset_env_name_base_url(monkeypatch):
    """provider 构造期 fail-fast：可操作错误，而不是把变量名当主机名去连。"""
    from axon.providers.openai_compat import OpenAICompatProvider
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="形如环境变量名，但该变量未设置"):
        OpenAICompatProvider(api_key="sk-x", base_url="OPENAI_BASE_URL")


def test_provider_accepts_real_url(monkeypatch):
    from axon.providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(api_key="sk-x",
                             base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert p.name == "openai"


def test_load_config_base_url_unset_env_no_raise(tmp_path, monkeypatch):
    """加载期不炸（变量名原样进配置）；错误推迟到 provider 构造。"""
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
model:
  active_provider: openai
  providers:
    openai:
      base_url: OPENAI_BASE_URL
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    cfg = load_config(p)
    assert cfg.active_provider_cfg().base_url == "OPENAI_BASE_URL"


def test_yaml_literal_url_wins_over_env(tmp_path, monkeypatch):
    """优先级语义（用户实测困惑点）：yml 里写死 URL 时，OPENAI_BASE_URL
    环境变量被 shadow——数据目录已播种配置旧版写死 api.openai.com，导致
    用户 set 了变量却不生效。v0.4.26 把播种配置改为变量名形式修复此事。"""
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
model:
  active_provider: openai
  providers:
    openai:
      base_url: https://api.openai.com/v1
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1")
    cfg = load_config(p)
    assert cfg.active_provider_cfg().base_url == "https://api.openai.com/v1"


def test_env_name_form_in_yaml_picks_up_env(tmp_path, monkeypatch):
    """yml 写变量名形式 → 环境变量生效（v0.4.26 起；用户 `set
    OPENAI_BASE_URL=...` 的预期用法）。"""
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
model:
  active_provider: openai
  providers:
    openai:
      base_url: OPENAI_BASE_URL
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1")
    cfg = load_config(p)
    assert cfg.active_provider_cfg().base_url == \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"


# ============================================================ 接线检查

def test_loop_wraps_chat_consumption():
    assert "except Exception as e:  # noqa: BLE001 - SDK/传输异常 → 可操作的失败结论" in _SRC_LOOP
    assert 'note=_friendly_provider_error(' in _SRC_LOOP


def test_cli_has_base_url_flag():
    assert '"--base-url"' in _SRC_CLI
    assert '"base_url": args.base_url' in _SRC_CLI


def test_pyproject_version_matches_package():
    d = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    from axon import __version__
    assert __version__ == d["project"]["version"]
