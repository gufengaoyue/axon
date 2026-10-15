"""工具注册表（设计 §4.2）：ToolSpec / ToolRegistry / tool_result_ok/err。

注册 ≠ 披露 ≠ 授权：
  - register()   进程启动全量注册（重名拒绝）
  - disclosed()  每轮送给模型的子集（v1 全量，固定注册序 → 前缀稳定）
  - 授权         每次调用经 SandboxGateway 单独判定
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass

if TYPE_CHECKING:  # pragma: no cover
    pass

ToolArgs = dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    ok: bool
    output: str                       # C 层统一截断后入窗
    error_type: Literal["transient", "model", "permanent", "resource"] | None = None
    recovery_hint: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str                         # 稳定 id，进前缀后永不改名
    description: str                  # 写"做什么/输出格式/约束/非直观参数示例"
    parameters: dict                  # JSON Schema
    handler: Callable[[ToolArgs, "object"], ToolResult]
    to_action: Callable[[ToolArgs], Action] | None = None
    risk: RiskClass = RiskClass.READ
    mutating: bool = False            # True → 强制串行
    parallel_safe: bool = True
    timeout: float = 60.0

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


def tool_result_ok(call, output: str, meta: dict | None = None) -> ToolResult:
    return ToolResult(call_id=call.id, name=call.name, ok=True, output=output,
                      meta=meta or {})


def tool_result_err(call, error_type: str, hint: str, output: str = "") -> ToolResult:
    return ToolResult(call_id=call.id, name=call.name, ok=False, output=output,
                      error_type=error_type, recovery_hint=hint)


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具重名注册: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def disclosed(self) -> list[ToolSpec]:
        """固定注册序（确定性序列化 → 前缀稳定）。"""
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs.keys())

    def openai_tools(self) -> list[dict]:
        return [s.openai_schema() for s in self.disclosed()]


def canonical_args(args: ToolArgs) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
