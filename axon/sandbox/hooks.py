"""Hook 协议 + 内置 hook 装配 + 支撑类型（设计 §4.5/§8.3）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from axon.sandbox.command_rules import CommandRuleHook
from axon.sandbox.gateway import Action, Decision
from axon.sandbox.network_policy import NetworkPolicyHook, SecretScanHook
from axon.sandbox.path_policy import PathPolicyHook
from axon.sandbox.risk import tier_of


# ---------------------------------------------------------------- 支撑类型

@dataclass
class VerifyResult:              # V 层单轮验证结论
    passed: bool
    failed_rules: list[str] = field(default_factory=list)   # 未过规则名
    advice: str = ""             # 给模型的纠正建议


@dataclass
class HookCtx:                   # Hook 执行上下文（只读）
    action: Action
    session_id: str
    turn: int
    workspace: str


@dataclass
class CompactionEvent:           # S 层压缩/清理事件（失效账本一行）
    session_id: str
    at_turn: int
    trigger: Literal["warn", "force"]
    before_tokens: int
    after_tokens: int
    summary: str = ""            # force 事件为 9 字段摘要；warn 事件为占位说明


class Prompter(Protocol):        # 人在回路确认接口（CLI 实现 + 测试替身）
    def confirm(self, action: Action, decision: Decision): ...


# ---------------------------------------------------------------- Hook 协议

class Hook(Protocol):
    name: str

    def pre(self, action: Action, ctx: HookCtx) -> Decision | None:
        """返回 Decision 表态；None = 不表态（可做旁路 side_effect）。"""
        ...

    def post(self, action: Action, result, ctx: HookCtx) -> None:
        ...


# ---------------------------------------------------------------- 内置 hooks

class RiskGradeHook:
    """风险分级 → 人在回路四档。

    升级规则：PathPolicy/CommandRule/NetworkPolicy 已表态的动作不重复升级
    （shell/network 由专门 hook 负责）；fs_* 的 allow 若档位为 red → 升 ask；
    black 一律 deny。
    """
    name = "RiskGrade"

    def __init__(self, config):
        self.config = config

    def pre(self, action: Action, ctx) -> Decision | None:
        tier = tier_of(action.kind, self.config.sandbox.risk_grading)
        if tier == "black":
            return Decision("deny", f"risk:black:{action.kind}", "动作在黑名单档，直接阻断")
        if tier == "red" and action.kind.startswith("fs_"):
            return Decision("ask", f"risk:red:{action.kind}",
                            f"{action.kind} 属高风险档，执行前需确认")
        return None  # green/yellow 放行（yellow 的留痕由 audit 承担）

    def post(self, action, result, ctx) -> None:
        return None


class BackupHook:
    """修改前自动快照（旁路 hook，返回 None 不表态）。"""
    name = "Backup"

    def __init__(self, config):
        self.config = config

    def pre(self, action: Action, ctx) -> Decision | None:
        return None

    def side_effect(self, action: Action, ctx: HookCtx) -> None:
        if not self.config.sandbox.backup_enabled:
            return
        if action.kind not in ("fs_write", "fs_edit", "fs_delete"):
            return
        from pathlib import Path
        from axon.sandbox.backup import snapshot_file
        snapshot_file(Path(action.target), self.config.snapshots_dir,
                      ctx.session_id, ctx.turn, self.config.workspace)

    def post(self, action, result, ctx) -> None:
        return None


class AuditHook:
    """审计由 SandboxGateway.execute 直接调用 AuditLog 完成；此 hook 保留扩展位。"""
    name = "Audit"

    def __init__(self, audit=None):
        self.audit = audit

    def pre(self, action: Action, ctx) -> Decision | None:
        return None

    def post(self, action, result, ctx) -> None:
        return None


def register_builtin_hooks(config, audit) -> list:
    """按设计 §4.5 管线顺序装配；deny 优先短路由网关保证。"""
    return [
        PathPolicyHook(config),
        CommandRuleHook(config),
        NetworkPolicyHook(config),
        SecretScanHook(config),
        RiskGradeHook(config),
        BackupHook(config),
        AuditHook(audit),
    ]
