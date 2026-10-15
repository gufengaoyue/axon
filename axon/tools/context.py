"""工具上下文：内置工具 make_spec 的共享依赖。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from axon.config import AXONConfig
from axon.memory.store import MemoryStore


@dataclass
class ToolContext:
    workspace: Path
    config: AXONConfig
    memory_store: MemoryStore
    global_memory: object = None       # GlobalMemoryStore（v0.4.21，可缺省）
