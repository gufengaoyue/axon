"""v0.4.14：第一步核实结论落地——CLI 本就并发执行同消息多个 tool_use
（changelog 实证："concurrently editing the same file in the same conversation turn"）。
因此：
  ① 提示词指引模型"无依赖的子任务放同一条消息的多个调用"（零代码拿到并行收益）
  ② 安全修复：并发审批互斥锁（否则两个 ask 同时 input()，用户的 y 可能答到另一个上）
"""
from __future__ import annotations

import threading

import pytest

from axon.cli import TTYPrompter
from axon.sandbox.gateway import Action, Decision


# ---- ② 审批互斥锁 ----

def test_prompter_has_lock():
    p = TTYPrompter(console=None, status=None)
    assert hasattr(p._lock, "acquire") and hasattr(p._lock, "release")


def test_confirm_serializes_concurrent_approvals(monkeypatch, capsys):
    """两个线程同时 confirm → 串行执行（第二个等第一个问完），不错串。"""
    import time
    p = TTYPrompter(console=None, status=None)
    spans: list = []

    def fake_input(prompt=""):
        t0 = time.monotonic()
        time.sleep(0.05)                     # 模拟用户思考，放大竞态窗口
        t1 = time.monotonic()
        spans.append((t0, t1))
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    act = Action(kind="shell", target="rm -rf /tmp/x")
    dec = Decision("ask", "command:write_outside", "测试")
    threads = [threading.Thread(target=p.confirm, args=(act, dec)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spans) == 2
    spans.sort()
    # 两次输入的区间**不重叠** → 确认被锁串行化（无交错/串话）
    assert spans[0][1] <= spans[1][0] + 1e-6
    assert p.confirm(act, dec).approved is True


def test_confirm_deny_still_works(monkeypatch):
    p = TTYPrompter(console=None, status=None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    from axon.sandbox.gateway import Approval
    out = p.confirm(Action(kind="shell", target="x"),
                    Decision("ask", "r", "reason"))
    assert out.approved is False


# ---- ① 提示词并行指引 ----

def test_prompt_has_parallel_guidance():
    from pathlib import Path
    from axon.prompts.system import build_system_prompt
    from axon.config import load_config
    cfg = load_config("config.yml")
    prompt = build_system_prompt(cfg)
    # 并行指引的关键要素：同一条消息多个调用 / 无依赖 / 有依赖要等
    assert "同一条消息" in prompt or "同消息" in prompt
    assert "并行" in prompt
    assert "依赖" in prompt


def test_parallel_guidance_mentions_concurrency_and_order():
    from axon.prompts.system import build_system_prompt
    from axon.config import load_config
    prompt = build_system_prompt(load_config("config.yml"))
    assert "并发" in prompt
    assert ("上游" in prompt) or ("等" in prompt and "结果" in prompt)
