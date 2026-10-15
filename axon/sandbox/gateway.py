"""SandboxGateway：所有工具调用的唯一入口（设计 §4.5）。

注册 ≠ 披露 ≠ 授权：
  - 注册：ToolRegistry 进程启动时全量注册
  - 披露：ContextBuilder 每轮固定序送给模型
  - 授权：本网关对每次调用单独判定（Hook 管线，deny 优先短路）

工具 handler 只收到绑定的 GatewayHandle，拿不到裸 fs/shell —— 安检门不可绕过。
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

from axon.sandbox.audit import AuditLog
from axon.sandbox.risk import risk_of, tier_of

if TYPE_CHECKING:  # pragma: no cover
    from axon.tools.registry import ToolResult, ToolSpec


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class Action:
    kind: Literal["fs_read", "fs_write", "fs_edit", "fs_delete", "shell", "network", "mcp"]
    target: str                       # 路径 / 命令串 / URL / mcp__server__tool
    detail: dict = field(default_factory=dict)   # canonical args 快照
    session_id: str = ""
    turn: int = 0


@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "ask", "deny"]
    rule: str                         # 命中哪条规则（审计用）
    reason: str = ""
    severity: str = ""                # "high" = 高危（盘根/系统目录等，v0.3.1 审批面板醒目标记）


@dataclass
class Approval:
    approved: bool
    remember_session: bool = False    # 会话级授权缓存键 = 命令前缀指纹（prefix/exact 由 approval_granularity 定）
    edited_detail: dict | None = None  # 用户改参数后放行


@dataclass
class GatewayHandle:
    """绑定会话上下文的网关句柄（只读上下文 + 子动作上报 + 工具内按需确认）。

    confirm：工具在"网关层无法预判、只有拿到响应/内容才知道需要审批"的场景
    （如 web_fetch 下载到风险类型文件）调用；非 TTY/未注入 → 一律 False（fail-closed）。
    """
    session_id: str
    turn: int
    workspace: str
    note_subaction: Callable[[Action], None]
    confirm: Callable[[Action, str], bool] | None = None


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def action_fingerprint(action: Action, granularity: str) -> str:
    """授权缓存键。prefix = 命令程序 + 首参数特征 / 路径取父目录；exact = 全量。

    v0.2.2 修复：旧实现把"参数个数"写进 shell 键（`shell|python|5args`），
    导致同一类命令（python -c "不同代码"）每次参数数不同 → 缓存永不命中 →
    用户按了 [a]lways 仍被反复询问。改为"程序 + 首参数特征"：
      python --version      → shell|python|--version
      python -c "<任意代码>" → shell|python|-c          ← 同类共享
      python script.py      → shell|python|@<脚本目录>
      rm a.txt              → shell|rm|@<父目录>
    """
    if granularity == "exact":
        return hashlib.sha256(
            f"{action.kind}|{action.target}|{canonical_json(action.detail)}".encode()
        ).hexdigest()
    if action.kind == "shell":
        parts = action.target.split()
        head = parts[0] if parts else ""
        second = parts[1] if len(parts) > 1 else ""
        if second.startswith("-"):
            tag = second                      # flag 类（-c/-e/--version…）
        elif second:
            # 路径类 → 取父目录（同目录内的同程序命令共享授权）
            norm = second.replace("\\", "/").strip('"\'')
            parent = norm.rsplit("/", 1)[0] if "/" in norm else "@cwd"
            tag = f"@{parent}"
        else:
            tag = "@none"
        return f"shell|{head}|{tag}"
    if action.kind in ("fs_read", "fs_write", "fs_edit", "fs_delete"):
        parent = action.target.replace("\\", "/").rsplit("/", 1)[0] if "/" in action.target else "."
        return f"{action.kind}|dir:{parent}"
    return f"{action.kind}|{action.target}"


# ---------------------------------------------------------------- 网关

class SandboxGateway:
    def __init__(self, config, hooks: list, audit: AuditLog, prompter):
        self.config = config
        self.hooks = hooks
        self.audit = audit
        self.prompter = prompter
        self.session_id = ""
        self.turn = 0
        self._auth_cache: dict[str, None] = {}
        self._lock = threading.Lock()

    # ---- 上下文绑定 ----
    def bind(self, session_id: str, turn: int) -> None:
        self.session_id = session_id
        self.turn = turn

    def clear_authorizations(self) -> None:
        self._auth_cache.clear()

    # ---- 工具内按需确认（v0.2.1） ----
    def _confirm_from_tool(self, action: Action, reason: str) -> bool:
        """工具在"拿到内容才知道要审批"时调用（如 web_fetch 命中风险文件类型）。

        走同一条 TTYPrompter/审批器；审计记 ask_approved / ask_denied；
        非 TTY 环境由 prompter 自身 fail-closed（AutoDeny）。
        """
        decision = Decision("ask", "tool:runtime_confirm", reason)
        try:
            approval = self.prompter.confirm(action, decision)
        except Exception:
            return False
        self.audit.record(action, "ask_approved" if approval.approved else "ask_denied",
                          decision.rule, "ok" if approval.approved else "blocked")
        if approval.approved and approval.remember_session:
            key = action_fingerprint(action, self.config.sandbox.approval_granularity)
            self._auth_cache[key] = None
        return bool(approval.approved)

    # ---- 判定 ----
    def check(self, action: Action) -> Decision:
        """Hook 管线（设计 §4.5 顺序）。

        语义：deny 优先短路；其余取**最严格**判定（deny > ask > allow）——
        这样 RiskGrade 的档位升级（如 workspace 内 fs_delete → 红档 ask）
        不会被 PathPolicy 的 allow 吞掉；同级别取先命中者的规则名。
        shell/network 由 CommandRule/NetworkPolicy 专职表态，RiskGrade 对其返回 None，
        白名单/域名 allow 等显式放行不受影响。
        """
        deny: Decision | None = None
        best: Decision | None = None
        rank = {"allow": 0, "ask": 1, "deny": 2}
        for hook in self.hooks:
            d = hook.pre(action, self._hook_ctx(action))
            if d is None:
                continue
            if d.verdict == "deny":
                deny = d
                break
            if best is None or rank[d.verdict] > rank[best.verdict]:
                best = d
        decision = deny or best or Decision("allow", "default", "no hook objected")
        # 会话级授权缓存：只把 ask 降级为 allow（审批是用户显式决策）
        if decision.verdict == "ask":
            key = action_fingerprint(action, self.config.sandbox.approval_granularity)
            if key in self._auth_cache:
                decision = Decision("allow", "session_cache", "会话级授权缓存命中")
        return decision

    def _hook_ctx(self, action: Action):
        from axon.sandbox.hooks import HookCtx
        return HookCtx(action=action, session_id=self.session_id, turn=self.turn,
                       workspace=str(self.config.workspace))

    # ---- 执行 ----
    def execute(self, call, spec, args: dict) -> "ToolResult":
        from axon.tools.registry import tool_result_err

        action = spec.to_action(args) if spec.to_action else self._default_action(spec, args)
        action = Action(**{**action.__dict__, "session_id": self.session_id, "turn": self.turn})

        decision = self.check(action)
        if decision.verdict == "deny":
            self.audit.record(action, "deny", decision.rule, "blocked")
            return tool_result_err(call, "permanent",
                                   f"沙箱拒绝（{decision.rule}）：{decision.reason}")

        if decision.verdict == "ask":
            approval = self.prompter.confirm(action, decision)
            self.audit.record(action, "ask_approved" if approval.approved else "ask_denied",
                              decision.rule, "ok" if approval.approved else "blocked")
            if not approval.approved:
                return tool_result_err(call, "permanent", "用户拒绝执行该操作")
            if approval.remember_session:
                key = action_fingerprint(action, self.config.sandbox.approval_granularity)
                self._auth_cache[key] = None
            if approval.edited_detail:
                action = Action(**{**action.__dict__, "detail": approval.edited_detail})
                args = approval.edited_detail

        # 步骤 3.5：副作用预备 hook（快照等，返回 None 只做旁路）
        for hook in self.hooks:
            if hasattr(hook, "side_effect"):
                hook.side_effect(action, self._hook_ctx(action))

        handle = GatewayHandle(
            session_id=self.session_id,
            turn=self.turn,
            workspace=str(self.config.workspace),
            note_subaction=self._note_subaction,
            confirm=self._confirm_from_tool,
        )
        try:
            result = spec.handler(call, args, handle)
        except Exception as e:  # 禁吞异常：转为观察回喂
            result = tool_result_err(call, "permanent", f"{type(e).__name__}: {e}")

        for hook in self.hooks:
            hook.post(action, result, self._hook_ctx(action))

        self.audit.record(action, decision.verdict, decision.rule,
                          "ok" if result.ok else "error")
        return result

    def _default_action(self, spec, args: dict) -> Action:
        kind_map = {"READ": "fs_read", "WRITE": "fs_write", "MUTATE": "fs_edit",
                    "DELETE": "fs_delete", "SHELL": "shell", "NETWORK": "network",
                    "MCP": "mcp"}
        kind = kind_map[spec.risk.value]
        target = (args.get("path") or args.get("command") or args.get("url")
                  or args.get("tool") or "")
        return Action(kind=kind, target=str(target), detail=dict(args))

    def _note_subaction(self, action: Action) -> None:
        with self._lock:
            self.audit.record(action, "allow", "subaction", "ok")

    def execute_batch(self, calls: list) -> list:
        """并行执行一批工具调用。

        规则（设计 §4.1）：parallel_safe 且非 mutating → 有界并行池；其余串行；
        读先于写执行，天然满足同 target 读写互斥；部分失败独立回喂。
        calls: list[tuple[ToolCall, ToolSpec, dict]]
        """
        if not calls:
            return []
        parallel = [c for c in calls if c[1].parallel_safe and not c[1].mutating]
        serial = [c for c in calls if not (c[1].parallel_safe and not c[1].mutating)]

        results: dict[int, object] = {}
        if parallel:
            with ThreadPoolExecutor(max_workers=min(self.config.agent.parallel_limit,
                                                    len(parallel))) as pool:
                futs = {pool.submit(self._safe_execute, c): i
                        for i, c in enumerate(calls) if c in parallel}
                for fut, idx in futs.items():
                    results[idx] = fut.result()
        for i, c in enumerate(calls):
            if c in serial:
                results[i] = self._safe_execute(c)
        return [results[i] for i in range(len(calls))]

    def _safe_execute(self, c) -> "ToolResult":
        call, spec, args = c
        try:
            return self.execute(call, spec, args)
        except Exception as e:  # 双保险：网关自身异常也转为观察
            from axon.tools.registry import tool_result_err
            return tool_result_err(call, "transient", f"gateway: {type(e).__name__}: {e}")
