"""回归：v0.4.18 卡死修复（用户实测"159 秒无输出、一直转圈"）。

根因：v0.4.16 的 client 迁移写成
    async with CodeBuddySDKClient(...) as client:   # __aenter__ 已 connect()（无 prompt）
        await client.connect(prompt=...)             # 又 connect → **第二个 CLI 子进程**
两个子进程互抢 stdio → deadlock → 无任何输出、无限转圈。

修：async with 后改用 `query()`（SDK 设计意图：首个动作 query → initialize(has_prompt=True)，
CLI 不回放历史）；另加**首条消息超时护栏**（连接层卡死时不再无声转圈）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from axon.providers.codebuddy import CodebuddyRunner

_SRC = (Path(__file__).resolve().parents[2] / "axon" /
        "providers" / "codebuddy.py").read_text(encoding="utf-8")


def test_no_double_connect():
    """产品代码不得再调 client.connect(prompt=...)（会创建第二个 CLI 进程）。"""
    assert "client.connect(prompt=" not in _SRC
    assert "await client.query(prompt_input)" in _SRC


def test_first_message_timeout_guard():
    assert "等待模型响应超时" in _SRC
    assert "asyncio.wait_for(stream.__anext__()" in _SRC


def test_query_first_then_receive():
    """顺序：async with → query → receive_messages（initialize has_prompt=True 先行）。"""
    q = _SRC.index("await client.query(prompt_input)")
    r = _SRC.index("client.receive_messages().__aiter__()")
    assert q < r


def test_stuck_stream_raises_timeout_note(cfg, gw_approve, registry, store, memory,
                                          session, monkeypatch):
    """receive 永久挂起 → 首条超时命中，报"超时"而不是无声转圈。"""
    cfg.agent.model_timeout_sec = 0.2                     # 缩短以便测试

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, *a, **k):
            pass

        async def receive_messages(self):
            await asyncio.sleep(30)                       # 永久"卡住"
            if False:                                     # pragma: no cover
                yield None

        async def interrupt(self):
            pass

    class FakeSDK:
        CodeBuddySDKClient = FakeClient

    runner = CodebuddyRunner(cfg, gw_approve, registry, _Approve(), store, memory)
    runner._build_options = lambda sdk, sess: None
    runner._import_sdk = lambda: FakeSDK
    r = runner.run_turn(session, "hi")
    assert r.stop_reason == "provider_error"
    assert "超时" in r.note


class _Approve:
    def confirm(self, action, decision):
        from axon.sandbox.gateway import Approval
        return Approval(True)
