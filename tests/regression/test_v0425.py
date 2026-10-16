"""回归：v0.4.25 两项（用户实测 + 用户要求）。

① 中断后一直转圈：思考滚动框（rich Live）在 Ctrl+C / "只思考不输出"的回合
   结束后不关闭 → Live 自带的 ~8Hz 自动刷新线程持续重绘终端，prompt_toolkit
   输入行被反复覆盖（用户实测"中断后一直在转圈"）。
   修复：idle() 先 _close_thinking()；_run_turn 的 finally 任何出口收框；
   stop_watchdog join 看门狗线程（防停止后又重启转圈）；repl 中断清理防二次 Ctrl+C。

② 生图模型接入口（文生图/图生图）：独立工具 image_generate，仅显式生图任务调用；
   调用时 CLI 特别提醒（🖼）；未配置/失败 → 返回"放弃"话术，
   禁止用无生图能力的模型冒充生成图片。
"""
from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest
from rich.console import Console

from axon.cli import StatusLine, _run_turn
from axon.config import ImageGenConfig, load_config
from axon.prompts.system import build_system_prompt
from axon.tools.builtin import build_builtin_registry
from axon.tools.context import ToolContext

_ROOT = Path(__file__).resolve().parents[2]
_SRC = (_ROOT / "axon" / "cli.py").read_text(encoding="utf-8")


# ============================================================ ① Live 泄漏

@pytest.fixture
def rich_sl(tmp_path):
    f = open(tmp_path / "o.txt", "w", encoding="utf-8")
    s = StatusLine(console=Console(file=f, width=70, force_terminal=False, no_color=True))
    s._tty = True
    yield s
    f.close()


def test_idle_closes_thinking_live(rich_sl):
    """核心复现：思考框打开后 idle()（中断/回合结束的清理入口）必须关闭 Live。"""
    rich_sl.thinking_write("正在分析请求...\n")
    assert rich_sl._live_active is True                 # 开框 → 独占终端
    rich_sl.idle()
    assert rich_sl._live_active is False                # 关框 → 释放终端
    assert rich_sl._thinking_open is False
    assert rich_sl._live is None                        # Live 对象已销毁（刷新线程随之停止）


def test_idle_is_idempotent_for_live(rich_sl):
    """没有思考框时 idle() 反复调用无副作用（正常路径每回合都调）。"""
    rich_sl.idle()
    rich_sl.idle()
    assert rich_sl._live_active is False


def test_thinking_only_turn_closes_live_before_footer(rich_sl):
    """"只思考、无正文/工具"的回合：_run_turn 的 finally 也要收框，
    否则页脚（_print_turn_result）会被 Live 覆盖。"""
    class _Loop:
        status = rich_sl

        def run(self, *a, **k):
            rich_sl.thinking_write("只想不说\n")          # 模拟：只有 thinking 增量
            from axon.agent.loop import TurnResult
            return TurnResult(final_text="ok", stop_reason="verified",
                              turns_used=1, tokens_used=0)

    class _Agent:
        show_thinking = True
        stream_interject = False

    class _Cfg:
        agent = _Agent()

    app = {"console": rich_sl.console, "loop": _Loop(), "cfg": _Cfg(),
           "keyboard": None, "interject_bridge": None}
    _run_turn(app, None, "你好", lambda: True)
    assert rich_sl._live_active is False


def test_run_turn_finally_closes_live_on_keyboard_interrupt(rich_sl):
    """Ctrl+C 中断：即使异常穿过 loop.run，finally 也必须收思考框。"""
    class _Loop:
        status = rich_sl

        def run(self, *a, **k):
            rich_sl.thinking_write("思考到一半被打断\n")
            raise KeyboardInterrupt

    class _Agent:
        show_thinking = True
        stream_interject = False

    class _Cfg:
        agent = _Agent()

    app = {"console": rich_sl.console, "loop": _Loop(), "cfg": _Cfg(),
           "keyboard": None, "interject_bridge": None}
    with pytest.raises(KeyboardInterrupt):
        _run_turn(app, None, "你好", lambda: True)
    assert rich_sl._live_active is False                # 关键：Live 已关闭
    assert rich_sl._thinking_open is False


def test_stop_watchdog_joins_thread():
    """stop_watchdog 必须 join 看门狗线程——否则竞态下它可能在 stop 之后
    仍调用 thinking() 重启转圈，且无人再停（"停止后又转起来"）。"""
    sl = StatusLine(console=Console(file=io.StringIO(), width=70))
    sl._tty = True                                     # 看门狗只在 TTY 模式启动
    sl.start_watchdog()
    t = sl._watchdog
    assert t is not None and t.is_alive()
    sl.stop_watchdog()
    assert sl._watchdog is None
    assert not t.is_alive()                            # join 生效


def test_run_turn_finally_wires_close_thinking():
    """接线检查：_run_turn 的 finally 里包含 _close_thinking。"""
    assert "st._close_thinking()" in _SRC


def test_repl_interrupt_handler_idles_first():
    """接线检查：repl 的 KeyboardInterrupt 分支先 status.idle() 再打印提示，
    且内层 except 保证二次 Ctrl+C 时 idle() 至少再执行一次。"""
    assert "except KeyboardInterrupt:\n                status.idle()" in _SRC
    assert "except KeyboardInterrupt:\n                status.idle()\n            continue" in _SRC


# ============================================================ ② 生图接入口

def test_image_generate_tool_registered(cfg):
    reg = build_builtin_registry(ToolContext(cfg.workspace, cfg, None))
    assert "image_generate" in reg.names()
    spec = reg.get("image_generate")
    assert spec.risk is not None
    assert spec.parallel_safe is False                 # 按次计费，禁并发刷量
    assert "prompt" in spec.parameters["properties"]


class _Call:
    def __init__(self, name="image_generate"):
        self.id = "c1"
        self.name = name


def _spec_of(cfg):
    from axon.tools.builtin.image import make_spec
    return make_spec(ToolContext(cfg.workspace, cfg, None))


def test_unconfigured_image_gen_abandons(cfg):
    """未配置 → 放弃话术（不换模型硬凑）。"""
    r = _spec_of(cfg).handler(_Call(), {"prompt": "画一只猫"}, None)
    assert r.ok is False
    assert r.error_type == "config"
    assert "未配置生图模型" in r.recovery_hint
    assert "放弃生图任务" in r.recovery_hint
    assert "严禁" in r.recovery_hint                    # 禁止冒充生图


def test_disabled_image_gen_abandons(cfg):
    cfg.image_gen = ImageGenConfig(enabled=False, base_url="http://x/v1",
                                   model="m", api_key="k")
    r = _spec_of(cfg).handler(_Call(), {"prompt": "画"}, None)
    assert r.ok is False and "放弃生图任务" in r.recovery_hint


def test_empty_prompt_rejected(cfg):
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://x/v1",
                                   model="m", api_key="k")
    r = _spec_of(cfg).handler(_Call(), {"prompt": "   "}, None)
    assert r.ok is False and "prompt" in r.recovery_hint


def test_missing_base_image_rejected(cfg):
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://x/v1",
                                   model="m", api_key="k")
    r = _spec_of(cfg).handler(_Call(), {"prompt": "改图",
                                        "image_path": "/no/such.png"}, None)
    assert r.ok is False and "基准图不存在" in r.recovery_hint


def test_generate_success_b64(cfg, monkeypatch):
    """文生图成功：b64_json 响应 → 落盘 workspace/images/。"""
    import base64
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://img.test/v1",
                                   model="seedream-test", api_key="k",
                                   out_dir="images")
    png = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"

    def fake_post(base_url, key, path, *, json_body=None, files=None, data=None,
                  timeout=0):
        assert path == "/images/generations"
        assert json_body["model"] == "seedream-test"
        return {"data": [{"b64_json": base64.b64encode(png).decode()}]}

    monkeypatch.setattr("axon.image_gen._post", fake_post)
    ok, msg, meta = __import__("axon.image_gen", fromlist=["generate"]).generate(
        cfg, "一只在月亮上的猫")
    assert ok, msg
    assert meta["mode"] == "文生图" and meta["model"] == "seedream-test"
    p = Path(meta["path"])
    assert p.is_file() and p.read_bytes() == png
    assert p.parent == cfg.workspace / "images"
    assert "已调用生图模型" in msg and "seedream-test" in msg


def test_generate_success_url(cfg, monkeypatch):
    """url 形态响应 → 回源下载后落盘。"""
    png = b"\x89PNG\r\n\x1a\n" + b"from-url"
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://img.test/v1",
                                   model="m", api_key="k")
    monkeypatch.setattr("axon.image_gen._post",
                        lambda *a, **k: {"data": [{"url": "http://img.test/x.png"}]})
    monkeypatch.setattr("axon.image_gen._download", lambda url, timeout: png)
    from axon import image_gen
    ok, msg, meta = image_gen.generate(cfg, "测试")
    assert ok and Path(meta["path"]).read_bytes() == png


def test_generate_img2img_uses_edits(cfg, monkeypatch, tmp_path):
    """图生图走 /images/edits（multipart）。"""
    import base64
    src = tmp_path / "base.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\nbase")
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://img.test/v1",
                                   model="m", api_key="k")
    seen = {}

    def fake_post(base_url, key, path, *, json_body=None, files=None, data=None,
                  timeout=0):
        seen["path"] = path
        seen["files"] = files
        seen["data"] = data
        return {"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nout").decode()}]}

    monkeypatch.setattr("axon.image_gen._post", fake_post)
    from axon import image_gen
    ok, msg, meta = image_gen.generate(cfg, "把背景换成雪天", image_path=str(src))
    assert ok, msg
    assert seen["path"] == "/images/edits"
    assert seen["files"]["image"][0] == "base.png"
    assert seen["data"]["model"] == "m"
    assert meta["mode"] == "图生图"


def test_generate_failure_abandons(cfg, monkeypatch):
    """调用失败 → 放弃话术 + 具体错误。"""
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://img.test/v1",
                                   model="m", api_key="k")

    def boom(*a, **k):
        raise RuntimeError("HTTP 400: invalid model")

    monkeypatch.setattr("axon.image_gen._post", boom)
    r = _spec_of(cfg).handler(_Call(), {"prompt": "画"}, None)
    assert r.ok is False
    assert "生图模型调用失败" in r.recovery_hint
    assert "HTTP 400" in r.recovery_hint
    assert "放弃生图任务" in r.recovery_hint


def test_image_gen_config_parse(tmp_path):
    """config.yml 的 image_gen 段完整解析 + configured 判定。"""
    yaml_text = f"""
workspace: {tmp_path / "ws"}
project: {tmp_path}
image_gen:
  enabled: true
  base_url: https://ark.example.com/api/v3
  api_key_env: IMAGE_GEN_API_KEY
  model: doubao-seedream-4-0-250828
  size: 2K
  out_dir: pics
"""
    p = tmp_path / "config.yml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(p)
    ig = cfg.image_gen
    assert ig.base_url == "https://ark.example.com/api/v3"
    assert ig.model == "doubao-seedream-4-0-250828"
    assert ig.size == "2K" and ig.out_dir == "pics"
    assert ig.configured is True
    assert ig.api_key_env == "IMAGE_GEN_API_KEY"


def test_image_gen_configured_requires_model_and_base_url():
    ig = ImageGenConfig(model="", base_url="http://x")
    assert ig.configured is False
    ig2 = ImageGenConfig(model="m", base_url="")
    assert ig2.configured is False
    ig3 = ImageGenConfig(model="m", base_url="http://x")
    assert ig3.configured is True


def test_image_gen_resolve_key(monkeypatch):
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "env-secret")
    assert ImageGenConfig().resolve_key() == "env-secret"
    assert ImageGenConfig(api_key="direct").resolve_key() == "direct"


def test_prompt_has_image_gen_rules(cfg):
    sp = build_system_prompt(cfg)
    assert "mcp__axon__image_generate" in sp
    assert "只有**用户明确要求" in sp or "只有用户明确要求" in sp
    assert "放弃生图任务" in sp
    assert "严禁" in sp


@pytest.fixture
def sl_str():
    """StringIO 控制台 + 非 TTY：只验证打印内容，不起转圈线程。"""
    s = StatusLine(console=Console(file=io.StringIO(), width=70,
                                   force_terminal=False, no_color=True))
    yield s
    return s


def test_status_tool_image_reminds_user(sl_str):
    """CLI 特别提醒：调用 image_generate 时打印 🖼 提示。"""
    sl_str.image_model = "seedream-test"
    sl_str.tool("image_generate")
    out = sl_str.console.file.getvalue()
    assert "🖼 正在调用生图模型" in out
    assert "seedream-test" in out


def test_status_tool_image_reminder_without_model(sl_str):
    sl_str.tool("image_generate")
    out = sl_str.console.file.getvalue()
    assert "🖼 正在调用生图模型" in out
    assert "生图模型" in out


def test_image_generate_to_action(cfg):
    cfg.image_gen = ImageGenConfig(enabled=True, base_url="http://img.test/v1",
                                   model="m", api_key="k")
    act = _spec_of(cfg).to_action({"prompt": "画", "image_path": "/x.png"})
    assert act.kind == "network"
    assert act.detail["mode"] == "image2image"
    assert act.detail["model"] == "m"


def test_pyproject_version_matches_package():
    d = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    from axon import __version__
    assert __version__ == d["project"]["version"]
