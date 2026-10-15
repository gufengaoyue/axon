"""回归：风险门禁 + 网络策略 + secret scan（设计 §4.6 test_risk_gate）。"""
from __future__ import annotations

from axon.sandbox.gateway import Action


def test_fs_delete_in_workspace_is_red(gw_approve, ws):
    d = gw_approve.check(Action(kind="fs_delete", target=str(ws / "f.txt")))
    assert d.verdict == "ask" and d.rule.startswith("risk:red")


def test_shell_is_red_when_rules_pass(gw_approve):
    """echo 命中 allow，但 shell 档位由 CommandRule 表达（不再二次升级）。"""
    d = gw_approve.check(Action(kind="shell", target="echo hi"))
    assert d.verdict == "allow"


def test_black_block_denies(cfg, audit):
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.risk_grading.black_block = ["shell"]
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="shell", target="echo hi"))
    assert d.verdict == "deny"


def test_network_local_allowed(gw_approve):
    d = gw_approve.check(Action(kind="network", target="http://127.0.0.1:8000/mcp"))
    assert d.verdict == "allow" and d.rule == "network:local"


def test_network_private_allowed(gw_approve):
    d = gw_approve.check(Action(kind="network", target="http://192.168.1.10/api"))
    assert d.verdict == "allow"


def test_network_remote_asks_when_configured(cfg, audit):
    """v0.2.1：远程默认放行；显式配 remote_default=ask 才询问。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.network.remote_default = "ask"
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="network", target="https://api.example.com/v1"))
    assert d.verdict == "ask" and d.rule == "network:remote_ask"


def test_domain_deny_blocks(cfg, audit):
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.sandbox.network.domain_deny = ["evil.example.com"]
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="network", target="https://evil.example.com/x"))
    assert d.verdict == "deny" and "deny" in d.rule


def test_secret_scan_denies(gw_approve):
    url = "https://api.example.com/v1?key=" + "sk-" + "a" * 30
    d = gw_approve.check(Action(kind="network", target=url))
    assert d.verdict == "deny" and d.rule == "network:secret_scan"


def test_session_auth_cache(gw_approve):
    """同命令前缀 remember 后，二次 ask 降级为 allow。"""
    from axon.sandbox.gateway import action_fingerprint
    a1 = Action(kind="shell", target="pip install requests", session_id="s1", turn=1)
    assert gw_approve.check(a1).verdict == "ask"
    key = action_fingerprint(a1, gw_approve.config.sandbox.approval_granularity)
    gw_approve._auth_cache[key] = None
    a2 = Action(kind="shell", target="pip install flask", session_id="s1", turn=2)
    assert gw_approve.check(a2).verdict == "allow"
    assert gw_approve.check(a2).rule == "session_cache"
