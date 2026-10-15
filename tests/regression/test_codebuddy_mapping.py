"""回归：codebuddy SDK 适配（设计 §16 偏差 #8；2026-10-13 实地勘察后重写）。

纯映射测试不依赖 SDK 安装；SDK 在场时加测 _build_options / 端口挑选。
"""
from __future__ import annotations

import pytest

from axon.providers.codebuddy import (CodebuddyRunner, map_sdk_message,
                                        _partial_text)


# ---- 假 SDK 消息对象（类型名必须与 SDK 真实类名一致） ----
class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, usage=None, session_id="sdk-123", is_error=False):
        self.usage = usage
        self.session_id = session_id
        self.is_error = is_error


class StreamEvent:
    """假 SDK StreamEvent（类名必须与 SDK 真实类名一致才会被 map 识别）。"""

    def __init__(self, event):
        self.event = event


def _state():
    return {"partial": "", "seen_partial": False}


# ---- 纯映射 ----

def test_map_assistant_text():
    evs = map_sdk_message(AssistantMessage([TextBlock("你好")]), _state())
    assert len(evs) == 1 and evs[0].kind == "text" and evs[0].text == "你好"


def test_map_partial_then_full_dedup():
    """部分增量 + 完整消息 → 不重复输出（差分补缺）。"""
    state = _state()
    evs1 = map_sdk_message(StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}),
        state)
    evs2 = map_sdk_message(StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}),
        state)
    evs3 = map_sdk_message(AssistantMessage([TextBlock("Hello")]), state)
    assert [e.text for e in evs1 + evs2] == ["Hel", "lo"]
    assert evs3 == []  # 完整文本已被增量覆盖，差分后无剩余


def test_map_full_without_partial():
    evs = map_sdk_message(AssistantMessage([TextBlock("完整输出")]), _state())
    assert evs[0].kind == "text" and evs[0].text == "完整输出"


def test_map_tool_use_block():
    evs = map_sdk_message(AssistantMessage([
        ToolUseBlock(id="tu1", name="mcp__axon__read_file", input={"path": "a.txt"})]),
        _state())
    tc_evs = [e for e in evs if e.kind == "tool_call"]
    assert len(tc_evs) == 1
    assert tc_evs[0].tool_call.name == "mcp__axon__read_file"
    assert tc_evs[0].tool_call.arguments == {"path": "a.txt"}


def test_map_result_usage_and_done():
    evs = map_sdk_message(ResultMessage(usage={"input_tokens": 100,
                                               "output_tokens": 20}), _state())
    kinds = [e.kind for e in evs]
    assert kinds == ["usage", "done"]
    assert evs[0].usage["prompt_tokens"] == 100
    assert evs[0].usage["completion_tokens"] == 20


def test_map_result_usage_none():
    evs = map_sdk_message(ResultMessage(usage=None), _state())
    assert evs[0].usage == {}


def test_partial_text_non_text_delta_ignored():
    assert _partial_text({"type": "content_block_delta",
                          "delta": {"type": "input_json_delta", "partial_json": "{"}}) == ""
    assert _partial_text({"type": "message_start"}) == ""
    assert _partial_text(None) == ""


def test_map_user_message_noop():
    """UserMessage 的工具结果镜像由 runner 处理，map 不产事件。"""
    evs = map_sdk_message(UserMessage([ToolResultBlock("tu1", "ok")]), _state())
    assert evs == []


# ---- 端口挑选（无需 SDK） ----

def test_pick_port_auto(cfg, gw_approve, registry, store, memory):
    import socket
    runner = CodebuddyRunner(cfg, gw_approve, registry, _AutoApprove(), store, memory)
    port = runner._pick_port()
    assert 1024 < port < 65536
    # 自动端口不应是被占用的 57521（除非恰好…概率极低，跳过精确断言）
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))  # 能再次绑定 = 确实是空闲端口
    finally:
        s.close()


def test_pick_port_configured(cfg, gw_approve, registry, store, memory):
    cfg.model.active_provider = "codebuddy"
    cfg.model.providers["codebuddy"].cli_port = 23456
    runner = CodebuddyRunner(cfg, gw_approve, registry, _AutoApprove(), store, memory)
    assert runner._pick_port() == 23456


class _AutoApprove:
    def confirm(self, action, decision):
        from axon.sandbox.gateway import Approval
        return Approval(True)


# ---- SDK 在场时：选项构建 ----

sdk_available = pytest.importorskip  # 占位避免误用

try:
    import codebuddy_agent_sdk  # noqa: F401
    HAS_SDK = True
except ImportError:
    HAS_SDK = False


@pytest.mark.skipif(not HAS_SDK, reason="codebuddy_agent_sdk 未安装（venv 无 SDK，系统 Python 有）")
def test_build_options_shape(cfg, gw_approve, registry, store, memory, session):
    runner = CodebuddyRunner(cfg, gw_approve, registry, _AutoApprove(), store, memory)
    opts = runner._build_options(codebuddy_agent_sdk, session)
    assert opts.tools == [""]                       # 内置工具全禁
    assert opts.allowed_tools == []                 # 不预授权 → can_use_tool 唯一闸门
    assert opts.setting_sources == []
    assert opts.extra_args.get("port", "").isdigit()
    assert opts.include_partial_messages is True
    assert "axon" in opts.mcp_servers
    assert opts.cwd == str(cfg.workspace)


# ---- TurnResult.note 与 CLI 打印（v0.1.2 崩溃回归） ----

def test_turn_result_note_field():
    from axon.agent.loop import TurnResult
    r = TurnResult("", "provider_error", 0, 0, [], note="CLIConnectionError: boom")
    assert r.note == "CLIConnectionError: boom"
    assert r.side_effects == []


def test_print_turn_result_note_visible(capsys):
    from rich.console import Console
    from axon.agent.loop import TurnResult
    from axon.cli import _print_turn_result
    r = TurnResult("", "provider_error", 0, 0, [], note="CLIConnectionError: boom")
    _print_turn_result(Console(width=100), r)
    out = capsys.readouterr().out
    assert "CLIConnectionError: boom" in out
    assert "排查建议" in out          # provider_error 附排查指引


def test_print_turn_result_side_effects(capsys):
    from rich.console import Console
    from axon.agent.loop import TurnResult
    from axon.cli import _print_turn_result
    r = TurnResult("done", "verified", 2, 100,
                   [{"kind": "fs_write", "target": "/ws/a.txt", "result": "ok"}])
    _print_turn_result(Console(width=100), r)
    out = capsys.readouterr().out
    assert "已变更的文件" in out and "/ws/a.txt" in out and "write" in out


# ---- runner 异常/错误路径：错误文本必须进 note，不得污染 side_effects ----

def test_run_turn_exception_to_note(cfg, gw_approve, registry, store, memory, session):
    runner = CodebuddyRunner(cfg, gw_approve, registry, _AutoApprove(), store, memory)
    runner._build_options = lambda sdk, sess: None

    class FakeClient:                    # v0.4.16：client 模式（v0.4.18：query 首发）
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def connect(self, prompt=None):
            pass

        async def receive_messages(self):
            if False:                    # pragma: no cover
                yield None

        async def interrupt(self):
            pass

        async def query(self, *a, **k):
            raise RuntimeError("boom")   # 模拟 SDK 异常

    class FakeSDK:
        CodeBuddySDKClient = FakeClient

    runner._import_sdk = lambda: FakeSDK
    r = runner.run_turn(session, "hi")
    assert r.stop_reason == "provider_error"
    assert "boom" in r.note
    assert r.side_effects == []


def test_run_turn_error_message_to_note(cfg, gw_approve, registry, store, memory, session):
    runner = CodebuddyRunner(cfg, gw_approve, registry, _AutoApprove(), store, memory)
    runner._build_options = lambda sdk, sess: None

    class ErrorMessage:          # 类名必须与 SDK 真实类名一致
        error = "auth failed"

    class FakeClient:            # v0.4.16：client 模式
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def connect(self, prompt=None):
            pass

        async def receive_messages(self):
            yield ErrorMessage()

        async def interrupt(self):
            pass

        async def query(self, *a, **k):
            pass

    class FakeSDK:
        CodeBuddySDKClient = FakeClient

    runner._import_sdk = lambda: FakeSDK
    r = runner.run_turn(session, "hi")
    assert r.stop_reason == "provider_error"
    assert "auth failed" in r.note
    assert r.side_effects == []
