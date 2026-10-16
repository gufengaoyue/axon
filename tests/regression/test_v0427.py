"""回归：v0.4.27 自证死循环 + 中断后沉淀卡死（用户实测两项）。

现象（用户截图 + 描述）：
① 模型起初答对，随后在"总结"阶段陷入**自证死循环**——每轮结束都被
   `<verification_failed>` 观察消息推着重写同样的答案，模型自己都诊断出
   "这是系统干扰、重启才能消除"但无法脱身；
② 用户 Ctrl+C 后出现"正在沉淀本回合教训（信号：lesson/corrected）"，
   沉淀回合又陷进同样的思考里出不来。

根因链：
- `stuck_fired` 粘滞：打转触发后用户按 y 继续、模型换方法成功，终答仍被
  规则 5 判负 → 每轮追加 <verification_failed> → 无限自证（跑满 max_turns）
- 验证规则 1/3/4 扫**全量历史**：往回合遗留的 transient/resource 失败
  永久污染后续所有回合
- L1 沉淀不看中断：Ctrl+C 被 AgentLoop.run 吞成 user_interrupt 返回，
  repl 层照常触发沉淀 → 又开一个完整回合，继承毒上下文再次陷循环
- `_note_signals` 用**全量消息**：corrected 信号从第二回合起恒真

修复：stuck 继续后解滞 + verification_failed 连续 2 次熔断 + 规则按回合
scope + user_interrupt 跳过 L1 + 信号源改本回合增量 + 沉淀提示词隔离说明。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.loop import StuckDetector, _friendly_provider_error  # noqa: F401
from axon.agent.msgmeta import pack_tool_content
from axon.agent.verify import TurnVerifier
from axon.sandbox.hooks import VerifyResult

from tests.regression.conftest import make_loop, tc, txt

_SRC_LOOP = (Path(__file__).resolve().parents[2] / "axon" / "agent" /
             "loop.py").read_text(encoding="utf-8")
_SRC_CLI = (Path(__file__).resolve().parents[2] / "axon" / "cli.py").read_text(
    encoding="utf-8")


# ============================================================ ① stuck 解滞

def test_stuck_continue_then_success_verifies(cfg, gw_approve, registry,
                                              ctx_builder, compactor, verifier,
                                              store, session, ws):
    """核心复现：同参数 read_file 连续失败触发打转 → 用户选继续 → 模型换方法
    成功并给终答 → **必须 verified**（旧实现 stuck_fired 粘滞 → 每轮终答被
    规则 5 判负 → verification_failed 死循环到 max_turns）。"""
    (ws / "good.txt").write_text("内容", encoding="utf-8")
    provider = _ScriptProvider([
        tc("read_file", {"path": "missing.txt"}, cid="c1"),
        tc("read_file", {"path": "missing.txt"}, cid="c2"),
        tc("read_file", {"path": "missing.txt"}, cid="c3"),   # 第 3 次 → 打转触发
        tc("read_file", {"path": "good.txt"}, cid="c4"),      # 换参数成功
        txt("已完成：读取 good.txt"),
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "读个文件", on_stuck=lambda: True)   # 用户按 y
    assert result.stop_reason == "verified"
    assert "已完成" in result.final_text


def test_stuck_decline_still_breaks(cfg, gw_approve, registry, ctx_builder,
                                    compactor, verifier, store, session):
    """用户拒绝继续 → stuck_break 语义不变（修复不影响主动中断路径）。"""
    provider = _ScriptProvider([
        tc("read_file", {"path": "missing.txt"}, cid="c1"),
        tc("read_file", {"path": "missing.txt"}, cid="c2"),
        tc("read_file", {"path": "missing.txt"}, cid="c3"),
        txt("做不到"),
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    result = loop.run(session, "读个文件", on_stuck=lambda: False)  # 用户按 n
    assert result.stop_reason == "stuck_break"


# ============================================================ ② verification_failed 熔断

class _AlwaysFailVerifier:
    """验证器替身：永远判负（隔离测试熔断逻辑本身）。"""

    def verify(self, session, final_text, stuck_fired=False, scope_turns=None,
               check_effects=True):
        return VerifyResult(passed=False, failed_rules=["no_unresolved_errors"],
                            advice="工具 web_fetch 仍有未解决的 transient 类错误")


def test_verification_failed_circuit_breaker(cfg, gw_approve, registry,
                                             ctx_builder, compactor, store,
                                             session):
    """连续 2 次验证失败 → 熔断结束（旧实现无限追加观察消息刷屏 +
    模型反复自证，跑满 max_turns=50 才停）。"""
    provider = _ScriptProvider([txt("答 1"), txt("答 2"), txt("答 3")])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     None, store)
    loop.verifier = _AlwaysFailVerifier()
    result = loop.run(session, "你好")
    assert result.stop_reason == "verification_failed"
    assert result.turns_used == 2                      # 第 2 次失败即熔断，不再继续
    assert "连续 2 次" in result.note
    # <verification_failed> 观察消息只追加 1 条（第 2 次熔断没再追加）
    msgs = store.load_messages(session.id)
    vf = [m for m in msgs if m.role == "user"
          and str(m.content or "").startswith("<verification_failed>")]
    assert len(vf) == 1
    # 措辞升级：告知"系统自动校验、可说明理由后结题、不要反复重写"
    assert "系统自动规则校验" in vf[0].content
    assert "不要反复重写" in vf[0].content


def test_verification_failed_recovers_on_second_try(cfg, gw_approve, registry,
                                                    ctx_builder, compactor,
                                                    verifier, store, session, ws):
    """第 1 次失败 → 观察消息 → 模型修正后第 2 次通过 → verified（熔断不误伤
    正常的"失败→修正→通过"路径）。"""
    (ws / "ok.txt").write_text("x", encoding="utf-8")
    provider = _ScriptProvider([
        txt("先给个空的"),                       # 第 1 轮终答 → 验证失败
        tc("read_file", {"path": "ok.txt"}, cid="c1"),
        txt("已补充读取 ok.txt"),               # 第 2 轮 → 通过
    ])
    loop = make_loop(cfg, provider, gw_approve, registry, ctx_builder, compactor,
                     verifier, store)
    # 让第 1 轮 verify 失败：临时替换 verifier，第 2 次起恢复真验证器
    real = loop.verifier
    state = {"n": 0}

    class _Flaky:
        def verify(self, session, final_text, stuck_fired=False,
                   scope_turns=None, check_effects=True):
            state["n"] += 1
            if state["n"] == 1:
                return VerifyResult(passed=False,
                                    failed_rules=["final_nonempty"],
                                    advice="终答为空")
            return real.verify(session, final_text, stuck_fired=stuck_fired,
                               scope_turns=scope_turns,
                               check_effects=check_effects)

    loop.verifier = _Flaky()
    result = loop.run(session, "读文件")
    assert result.stop_reason == "verified"
    assert result.turns_used == 3                 # 失败→修正→通过，未误触熔断


# ============================================================ ③ 规则 scope

def test_verify_rules_scoped_to_turn(store, audit, cfg, session):
    """历史遗留的 transient 失败不再污染新回合（旧实现扫全量 → 之后每轮
    验证必失败 → 死循环）。"""
    from axon.providers.base import Message
    store.append_message(session.id, Message(
        role="tool", name="web_fetch",
        content=pack_tool_content("502 Bad Gateway", {"error_type": "transient"},
                                  error_type="transient")))          # turn=1（往回合）
    store.append_message(session.id, Message(
        role="assistant", content="终答"))                            # turn=2（本回合）
    v = TurnVerifier(store, audit, cfg)
    # 本回合 scope = {2} → 历史失败被挡掉 → 通过
    r_new = v.verify(session, "终答", scope_turns={2})
    assert r_new.passed, r_new.failed_rules
    # 不传 scope（旧行为/全量）→ 失败
    r_all = v.verify(session, "终答", scope_turns=None)
    assert not r_all.passed and "no_unresolved_errors" in r_all.failed_rules


def test_verify_rules_catch_current_turn_failures(store, audit, cfg, session):
    """本回合内的失败照样被抓（scope 不放水）。"""
    from axon.providers.base import Message
    store.append_message(session.id, Message(
        role="assistant", content="终答"))                            # turn=1
    store.append_message(session.id, Message(
        role="tool", name="web_fetch",
        content=pack_tool_content("502", {"error_type": "transient"},
                                  error_type="transient")))          # turn=2（本回合）
    v = TurnVerifier(store, audit, cfg)
    r = v.verify(session, "终答", scope_turns={1, 2})
    assert not r.passed and "no_unresolved_errors" in r.failed_rules


# ============================================================ ④ repl：中断不沉淀 + 增量信号

class _ScriptProvider:
    """脚本化 provider（conftest FakeProvider 的轻量本地版，支持 on_stuck）。"""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools, *, model, timeout=180.0):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        for ev in step:
            yield ev


def test_repl_skips_note_on_user_interrupt(monkeypatch, tmp_path, session):
    """user_interrupt 回合不触发 L1 沉淀（Ctrl+C 被 AgentLoop.run 吞成
    user_interrupt 返回，repl 层看不到异常——旧实现照常沉淀，又开一个
    回合继承毒上下文，用户实测"停不下来"）。"""
    from axon import cli as climod
    from axon.agent.loop import TurnResult
    from axon.cli import StatusLine

    notes: list[int] = []
    monkeypatch.setattr(climod, "_run_turn",
                        lambda *a, **k: TurnResult("", "user_interrupt", 0, 0,
                                                   note="用户中断"))
    monkeypatch.setattr(climod, "_maybe_note_turn",
                        lambda *a, **k: notes.append(1))
    monkeypatch.setattr(climod, "_maybe_summarize", lambda *a, **k: None)
    monkeypatch.setattr(climod, "_mark_ended", lambda *a, **k: None)
    monkeypatch.setattr(climod, "_recover_stale_sessions", lambda *a, **k: None)

    answers = iter(["帮我做点什么", EOFError()])

    def fake_input(*a, **k):
        v = next(answers)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr("builtins.input", fake_input)

    class _Mem:
        recover_prompt = False
        auto_summary_on_exit = False

    class _Cfg:
        project = tmp_path
        workspace = tmp_path
        memory = _Mem()
        tools = type("T", (), {"clipboard_watch": False})()

    class _Store:
        def load_messages(self, sid):
            return []

        def set_status(self, sid, st):
            pass

    app = {"cfg": _Cfg(), "console": None, "store": _Store(),
           "loop": type("L", (), {"status": StatusLine(None)})(),
           "keyboard": None, "interject_bridge": None, "clipwatch": None,
           "memory": None, "gateway": None, "audit": None, "mcp_errors": []}
    rc = climod.repl(app, session)
    assert rc == 0
    assert notes == []                      # 中断回合 → 零沉淀


def test_repl_notes_normal_turn(monkeypatch, tmp_path, session):
    """对照：正常回合有信号 → 照常沉淀（修复不误伤正常路径）。"""
    from axon import cli as climod
    from axon.agent.loop import TurnResult
    from axon.cli import StatusLine
    from axon.providers.base import Message

    notes: list[int] = []
    monkeypatch.setattr(climod, "_run_turn",
                        lambda *a, **k: TurnResult("ok", "verified", 1, 1))
    monkeypatch.setattr(climod, "_maybe_note_turn",
                        lambda *a, **k: notes.append(1))
    monkeypatch.setattr(climod, "_maybe_summarize", lambda *a, **k: None)
    monkeypatch.setattr(climod, "_mark_ended", lambda *a, **k: None)
    monkeypatch.setattr(climod, "_recover_stale_sessions", lambda *a, **k: None)

    # 回合前 1 条消息；回合中新增 2 条 user（模拟插嘴追加）→ corrected 信号
    pre = [Message(role="user", content="旧提问")]
    inc = [Message(role="user", content="任务"),
           Message(role="user", content="插嘴追加")]

    class _Store:
        def load_messages(self, sid):
            return pre + inc

        def set_status(self, sid, st):
            pass

    answers = iter(["任务", EOFError()])

    def fake_input(*a, **k):
        v = next(answers)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr("builtins.input", fake_input)

    class _Mem:
        recover_prompt = False
        auto_summary_on_exit = False

    class _Cfg:
        project = tmp_path
        workspace = tmp_path
        memory = _Mem()
        tools = type("T", (), {"clipboard_watch": False})()

    app = {"cfg": _Cfg(), "console": None, "store": _Store(),
           "loop": type("L", (), {"status": StatusLine(None)})(),
           "keyboard": None, "interject_bridge": None, "clipwatch": None,
           "memory": None, "gateway": None, "audit": None, "mcp_errors": []}
    rc = climod.repl(app, session)
    assert rc == 0
    assert notes == [1]                     # 正常回合 → 沉淀触发


def test_note_signals_uses_increment_only():
    """_note_signals 只用传入的消息片段判定（repl 已改传本回合增量）：
    单条 user → 无 corrected；两条 → corrected。"""
    from axon import cli as climod
    from axon.agent.loop import TurnResult
    from axon.providers.base import Message

    r = TurnResult("ok", "verified", 1, 1)
    assert "corrected" not in climod._note_signals(
        {}, object(), [Message(role="user", content="任务")], r)
    assert "corrected" in climod._note_signals(
        {}, object(),
        [Message(role="user", content="任务"),
         Message(role="user", content="插嘴")], r)
    # 观察消息不计入 user 数
    assert "corrected" not in climod._note_signals(
        {}, object(),
        [Message(role="user", content="任务"),
         Message(role="user", content="<verification_failed>...")], r)


def test_turn_note_prompt_isolates_observation_messages():
    """沉淀提示词显式隔离历史观察消息（防沉淀回合被 <verification_failed>
    带偏去重做修正）。"""
    from axon.cli import TURN_NOTE_PROMPT
    assert "verification_failed" in TURN_NOTE_PROMPT
    assert "忽略其内容" in TURN_NOTE_PROMPT


# ============================================================ 接线检查

def test_loop_stuck_unstick_wiring():
    assert "stuck_fired = False" in _SRC_LOOP
    assert "必须解除粘滞" in _SRC_LOOP


def test_loop_turn_start_scope_wiring():
    assert "turn_start = self.store.next_turn" in _SRC_LOOP
    assert "scope_turns={m.turn for m in" in _SRC_LOOP


def test_cli_interrupt_skip_note_wiring():
    assert 'getattr(result, "stop_reason", "") == "user_interrupt"' in _SRC_CLI
    assert "n_msgs_before" in _SRC_CLI
