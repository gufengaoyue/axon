"""风险分级：RiskClass 枚举、Action.kind ↔ RiskClass 映射、四档人在回路。

统一口径（设计 §4.5 回修）：
  Action.kind : fs_read | fs_write | fs_edit | fs_delete | shell | network | mcp
  RiskClass   : READ | WRITE | MUTATE | DELETE | SHELL | NETWORK | MCP
  档位升级：跨目录访问由 PathPolicy 把 fs_* 升一档；批量删除超阈值由
           CommandRule 升红；本地/远程网络由 NetworkPolicy 分档。
"""
from __future__ import annotations

from enum import Enum


class RiskClass(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    MUTATE = "MUTATE"
    DELETE = "DELETE"
    SHELL = "SHELL"
    NETWORK = "NETWORK"
    MCP = "MCP"


KIND_TO_RISK: dict[str, RiskClass] = {
    "fs_read": RiskClass.READ,
    "fs_write": RiskClass.WRITE,
    "fs_edit": RiskClass.MUTATE,
    "fs_delete": RiskClass.DELETE,
    "shell": RiskClass.SHELL,
    "network": RiskClass.NETWORK,
    "mcp": RiskClass.MCP,
}


def tier_of(kind: str, grading) -> str:
    """动作 kind → 四档：green / yellow / red / black（grading=SandboxConfig.risk_grading）。"""
    if kind in grading.black_block:
        return "black"
    if kind in grading.red_confirm:
        return "red"
    if kind in grading.yellow_notify:
        return "yellow"
    return "green"  # green_auto 及未列出项默认绿（fail-open 仅对只读成立；见 path/command 层）


def risk_of(kind: str) -> RiskClass:
    return KIND_TO_RISK.get(kind, RiskClass.READ)
