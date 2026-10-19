"""回归：v0.4.15 插嘴改造（用户实测两项）。

① 插嘴内容与 CLI 输出拼接在一行 → 回显先 ensure_newline，独立一行
② 插嘴"回合结束后才生效" → 通过 prompt 的 AsyncIterable 通道**执行中注入**，
   SDK 在当前回合的下一个决策点处理（InterjectBridge 线程安全桥接）
"""
from __future__ import annotations

import threading

import pytest

from axon.interject import InterjectBridge, InterjectReader, wrap_interjection


# ---- ② 执行中注入 ----

def test_bridge_submit_and_get():
    import asyncio

    async def main():
        bridge = InterjectBridge()
        bridge.bind(asyncio.get_running_loop())
        assert bridge.submit("补充一句") is True
        got = await bridge.get(timeout=1.0)
        assert got == "补充一句"
        bridge.reset()
        # v0.4.29：未绑定（reset 后）不再静默失败丢弃——存入 fallback，
        # openai 路在轮次边界 take_all() 消费（插嘴永久丢失的修复）
        assert bridge.submit("x") is True
        assert bridge.take_all() == ["x"]

    asyncio.run(main())


def test_bridge_submit_from_other_thread():
    """键盘线程 submit → async 侧能收到（call_soon_threadsafe 线程安全）。"""
    import asyncio

    async def main():
        bridge = InterjectBridge()
        bridge.bind(asyncio.get_running_loop())

        def worker():
            bridge.submit("来自键盘线程")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        got = await bridge.get(timeout=1.0)
        assert got == "来自键盘线程"

    asyncio.run(main())


def test_bridge_get_timeout_returns_none():
    import asyncio

    async def main():
        bridge = InterjectBridge()
        bridge.bind(asyncio.get_running_loop())
        assert await bridge.get(timeout=0.05) is None

    asyncio.run(main())


def test_wrap_interjection_asks_for_next_action():
    text = wrap_interjection("第二页换个风格")
    assert "<user_interjection>" in text
    assert "下一个动作之前" in text          # 关键：不是等回合结束
    assert "重新规划" in text
    assert "第二页换个风格" in text


def test_prompt_stream_yields_interjection_mid_turn():
    """v0.4.16 起：插嘴**不再**走 prompt 流（CLI 会排队到回合结束，用户实测无效）。

    改为 client 模式的中断 + query 语义——此处验证 prompt 流已不含插嘴循环，
    实际中断路径见 test_v0416.py。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "axon" /
           "providers" / "codebuddy.py").read_text(encoding="utf-8")
    assert "执行中插嘴循环" not in src          # 旧循环已移除
    assert "await client.interrupt()" in src    # 新路径存在


# ---- ① 独立行回显 ----

def test_reader_submit_callback_fires():
    got: list = []
    reader = InterjectReader(on_submit=got.append)
    for ch in "你好":
        reader.feed(ch)
    reader.feed("\r")
    assert got == ["你好"]
    assert reader.current_input() == ""


def test_settle_skips_when_stream_on(cfg):
    """stream_interject 开启时，无残留不弹确认（插嘴已即时注入）。

    v0.4.29：有边界残留时会弹确认（旧实现直接 return → 残留丢失）。
    """
    from unittest.mock import MagicMock
    from axon import cli
    app = {"keyboard": MagicMock(), "console": None, "cfg": cfg}
    kb = app["keyboard"]
    kb.has_pending.return_value = False         # 无残留 → 不弹确认
    cli._settle_interjections(app, None)
    kb.pop_all.assert_not_called()


def test_settle_confirms_residual_when_stream_on(cfg, monkeypatch):
    """v0.4.29：stream_on 时的边界残留 → 弹确认注入（不再静默丢弃）。"""
    from unittest.mock import MagicMock
    from axon import cli
    app = {"keyboard": MagicMock(), "console": None, "cfg": cfg,
           "interject_bridge": None}
    kb = app["keyboard"]
    kb.has_pending.side_effect = [True, False]      # 一轮后清空
    kb.pop_all.return_value = "边界插嘴"
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    sent = []
    monkeypatch.setattr(cli, "_run_turn",
                        lambda *a, **k: sent.append(a[2]))
    cli._settle_interjections(app, None)
    assert sent and "边界插嘴" in sent[0]


def test_config_stream_interject_default(cfg):
    assert cfg.agent.stream_interject is True


def test_config_stream_interject_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (tmp_path / "c.yml").write_text(
        "agent:\n  stream_interject: false\nmodel:\n  active_provider: openai\n",
        encoding="utf-8")
    from axon.config import load_config
    assert load_config(tmp_path / "c.yml").agent.stream_interject is False
