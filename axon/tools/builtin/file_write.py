"""write_file：新建/覆写文件（写前快照由 BackupHook 完成）。"""
from __future__ import annotations

import hashlib

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.builtin.file_read import resolve_path
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_ok

PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标文件路径（相对工作空间或绝对路径）"},
        "content": {"type": "string", "description": "完整文件内容（覆写式，不是追加）"},
    },
    "required": ["path", "content"],
}


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        path = resolve_path(ctx, args["path"])
        content = args["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return tool_result_ok(
            call, f"已写入 {path}（{len(content)} 字符）",
            meta={"path": str(path), "bytes": len(content.encode("utf-8")),
                  "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_write", target=str(resolve_path(ctx, args["path"])),
                      detail={"path": args["path"], "content_len": len(args.get("content", ""))})

    return ToolSpec(
        name="write_file",
        description=("写入文件（覆写）。相对路径基于工作空间；父目录不存在会自动创建。"
                     "修改前系统自动快照到 .axon/snapshots/。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.WRITE,
        mutating=True,
        parallel_safe=False,
    )
