"""回归测试共享 fixtures（设计 §4.6：无 TTY，AutoApprove/AutoDeny 替身）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.agent.compaction import Compactor
from axon.agent.context import ContextBuilder
from axon.agent.verify import TurnVerifier
from axon.config import load_config
from axon.memory.store import MemoryStore
from axon.providers.base import Message, StreamEvent, ToolCall
from axon.sandbox.audit import AuditLog
from axon.sandbox.gateway import Action, Approval, Decision, SandboxGateway
from axon.sandbox.hooks import register_builtin_hooks
from axon.session.db import SessionStore, connect
from axon.tools.builtin import ToolContext, build_builtin_registry


def _q(s: str) -> str:
    return json.dumps(str(s))


@pytest.fixture
def ws(tmp_path) -> Path:
    p = tmp_path / "ws"
    p.mkdir()
    return p


@pytest.fixture
def cfg(tmp_path, ws):
    yaml_text = f"""
workspace: {_q(ws)}
project: {_q(tmp_path)}
model:
  active_provider: openai
  context_window: 8000
  session_token_budget: 100000
context:
  compact_warn_ratio: 0.8
  compact_force_ratio: 0.9
sandbox:
  paths:
    - {{ path: "<workspace>", mode: allow }}
    - {{ path: "<home>", mode: deny }}
  commands:
    allow: ["ls", "echo", "cat", "git status", "git diff", "git log"]
    ask: ["rm", "mv"]
    deny: ["rm -rf /", "shutdown"]
  network:
    local_allow: true
    remote_default: allow
    secret_scan: true
memory:
  file: {_q(tmp_path / "AXON.md")}
session:
  db: {_q(tmp_path / "sessions.db")}
"""
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    return load_config(cfg_path)


@pytest.fixture
def store(cfg) -> SessionStore:
    return SessionStore(connect(cfg.db_path))


@pytest.fixture
def audit(store) -> AuditLog:
    return AuditLog(store.conn)


@pytest.fixture
def memory(cfg) -> MemoryStore:
    return MemoryStore(cfg.memory_path, cfg.context.memory_max_tokens,
                       cfg.memory.max_entries)


@pytest.fixture
def registry(cfg, memory):
    return build_builtin_registry(ToolContext(cfg.workspace, cfg, memory))


@pytest.fixture
def gw_approve(cfg, audit) -> SandboxGateway:
    return SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                          AutoApprovePrompter())


@pytest.fixture
def gw_deny(cfg, audit) -> SandboxGateway:
    return SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit,
                          AutoDenyPrompter())


class AutoApprovePrompter:
    def confirm(self, action: Action, decision: Decision) -> Approval:
        return Approval(True)


class AutoDenyPrompter:
    def confirm(self, action: Action, decision: Decision) -> Approval:
        return Approval(False)


class FakeProvider:
    """脚本化 provider：按调用顺序返回预置 StreamEvent 列表。"""

    name = "fake"

    def __init__(self, script: list[list[StreamEvent]]):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools, *, model, timeout=180.0):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        for ev in step:
            yield ev


def tc(name: str, args: dict, cid: str = "c1") -> list[StreamEvent]:
    return [StreamEvent(kind="tool_call",
                        tool_call=ToolCall(id=cid, name=name, arguments=args))]


def txt(text: str) -> list[StreamEvent]:
    return [StreamEvent(kind="text", text=text), StreamEvent(kind="done")]


def make_call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


@pytest.fixture
def ctx_builder(cfg, memory, registry) -> ContextBuilder:
    return ContextBuilder(cfg, memory, registry)


@pytest.fixture
def compactor(cfg) -> Compactor:
    return Compactor(cfg)


@pytest.fixture
def verifier(store, audit, cfg) -> TurnVerifier:
    return TurnVerifier(store, audit, cfg)


@pytest.fixture
def session(store, cfg):
    sid = store.create_session(str(cfg.project), "openai", "fake-model")
    return store.get_session(sid)


def make_loop(cfg, provider, gateway, registry, ctx_builder, compactor,
              verifier, store):
    from axon.agent.loop import AgentLoop
    return AgentLoop(provider, gateway, registry, ctx_builder, compactor,
                     verifier, store, cfg)


def user_msg(text: str, turn: int = 1) -> Message:
    return Message(role="user", content=text, turn=turn)


def assistant_msg(text: str, turn: int = 1) -> Message:
    return Message(role="assistant", content=text, turn=turn)


def tool_msg(cid: str, name: str, content: str, turn: int = 1) -> Message:
    return Message(role="tool", content=content, tool_call_id=cid, name=name,
                   turn=turn)
