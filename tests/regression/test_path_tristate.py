"""回归：路径策略（设计 §4.6 test_path_tristate；v0.1.4 读操作免确认后更新）。

新语义：
- 信任区 = workspace + project → allow（读写都免确认；写仍留快照）
- 读操作无规则命中 → 自动放行（绿档）
- 写/改/删越界 → ask（fail-closed）
- deny 表（系统目录/用户根/密钥目录）对读写都硬拦
- 显式配置规则优先于默认
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from axon.sandbox.gateway import Action


def _call():
    return type("C", (), {"id": "t1", "name": "read_file"})()


def _read_spec(registry):
    return registry.get("read_file")


def test_workspace_read_allowed(gw_approve, registry, ws):
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    r = gw_approve.execute(_call(), _read_spec(registry), {"path": "a.txt"})
    assert r.ok and "hello" in r.output


def test_outside_read_auto_allowed(gw_deny, registry, tmp_path):
    """v0.1.4：越界读不再询问——gw_deny 下也能成功即证明没有走审批。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("data", encoding="utf-8")
    r = gw_deny.execute(_call(), _read_spec(registry), {"path": str(outside)})
    assert r.ok and "data" in r.output


def test_outside_write_still_asks(cfg, audit, registry, tmp_path):
    """写操作越界仍 fail-closed 询问（AutoDeny → 被拒）。

    注意：conftest 的 project=tmp_path（现为信任区），须先把 project 收窄到子目录，
    tmp_path 本身才算"越界"。
    """
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoDenyPrompter
    cfg.project = tmp_path / "proj"
    cfg.project.mkdir()
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoDenyPrompter())
    spec = registry.get("write_file")
    r = gw.execute(_call(), spec, {"path": str(tmp_path / "new.txt"), "content": "x"})
    assert not r.ok and r.error_type == "permanent"
    assert not (tmp_path / "new.txt").exists()


def test_project_zone_read_allowed(gw_deny, registry, tmp_path):
    """项目目录是隐式信任区：读免确认。"""
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    r = gw_deny.execute(_call(), _read_spec(registry),
                        {"path": str(tmp_path / "README.md")})
    assert r.ok and "# hi" in r.output


def test_project_zone_write_allowed_with_snapshot(cfg, gw_deny, registry, tmp_path):
    """项目目录写免确认（黄档），且自动留快照。"""
    target = tmp_path / "code.py"
    target.write_text("v1", encoding="utf-8")
    spec = registry.get("write_file")
    r = gw_deny.execute(_call(), spec, {"path": str(target), "content": "v2"})
    assert r.ok and target.read_text(encoding="utf-8") == "v2"
    snaps = list(cfg.snapshots_dir.rglob("*code.py"))
    assert snaps, "项目内写操作应自动快照"


def test_home_denied_for_read(gw_approve, registry):
    """deny 表对读同样硬拦（需求文档：用户根目录/密钥目录默认拒绝）。"""
    target = Path.home() / ".ssh" / "id_rsa"
    d = gw_approve.check(Action(kind="fs_read", target=str(target)))
    assert d.verdict == "deny" and "deny" in d.rule


def test_explicit_ask_rule_honored_for_read(cfg, audit, tmp_path):
    """用户显式把某路径配成 ask → 读也询问（显式配置优先于默认放行）。"""
    from axon.config import PathRule
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoDenyPrompter
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    cfg.sandbox.paths.append(PathRule(path=secret_dir, mode="ask"))
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoDenyPrompter())
    d = gw.check(Action(kind="fs_read", target=str(secret_dir / "k.txt")))
    assert d.verdict == "ask"


def test_read_auto_allow_disabled(cfg, audit, tmp_path):
    """read_auto_allow=false → 回到旧语义（越界读询问）。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    cfg.project = tmp_path / "proj"
    cfg.project.mkdir()
    cfg.sandbox.read_auto_allow = False
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="fs_read", target=str(tmp_path / "far.txt")))
    assert d.verdict == "ask" and d.rule == "path:unmatched"


def test_symlink_escape_read_allow_write_ask(cfg, audit, registry, ws, tmp_path):
    """workspace 内 symlink 指向外部 → realpath 解析后按目标判定。"""
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoDenyPrompter
    cfg.project = tmp_path / "proj"
    cfg.project.mkdir()
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoDenyPrompter())
    outside = tmp_path / "real.txt"
    outside.write_text("x", encoding="utf-8")
    link = ws / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持 symlink")
    # 读：auto allow（目标不在 deny 表）；写：ask
    d_read = gw.check(Action(kind="fs_read", target=str(link)))
    assert d_read.verdict == "allow"
    d_write = gw.check(Action(kind="fs_write", target=str(link)))
    assert d_write.verdict == "ask"


def test_longest_prefix_wins(cfg, audit, tmp_path):
    """子路径 allow 规则应压过 <home> deny（最长前缀）。"""
    from axon.config import PathRule
    from axon.sandbox.gateway import SandboxGateway
    from axon.sandbox.hooks import register_builtin_hooks
    from tests.regression.conftest import AutoApprovePrompter
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg.sandbox.paths.append(PathRule(path=docs, mode="allow"))
    gw = SandboxGateway(cfg, register_builtin_hooks(cfg, audit), audit, AutoApprovePrompter())
    d = gw.check(Action(kind="fs_write", target=str(docs / "f.txt")))
    assert d.verdict == "allow"
