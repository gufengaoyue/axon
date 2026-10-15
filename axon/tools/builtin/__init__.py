"""内置工具包：build_builtin_registry(ctx) 注册 8 个工具（设计 §4.2）。"""
from __future__ import annotations

from axon.tools.context import ToolContext
from axon.tools.registry import ToolRegistry


def build_builtin_registry(ctx: ToolContext) -> ToolRegistry:
    from axon.tools.builtin import (file_edit, file_read, file_search,
                                      file_write, memory, shell, web_fetch)
    reg = ToolRegistry()
    reg.register(file_read.make_spec(ctx))
    reg.register(file_write.make_spec(ctx))
    reg.register(file_edit.make_spec(ctx))
    reg.register(file_search.make_spec(ctx))
    reg.register(web_fetch.make_spec(ctx))
    reg.register(shell.make_spec(ctx))
    reg.register(memory.make_read_spec(ctx))
    reg.register(memory.make_write_spec(ctx))
    reg.register(memory.make_global_spec(ctx))       # v0.4.21 系统级长期记忆
    reg.register(memory.make_note_spec(ctx))
    return reg
