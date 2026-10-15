"""edit_file：唯一匹配替换（old_string → new_string）。"""
from __future__ import annotations

import hashlib

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.builtin.file_read import resolve_path
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_err, tool_result_ok

PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标文件路径"},
        "old_string": {"type": "string", "description": "被替换的原文（必须在文件中唯一出现）"},
        "new_string": {"type": "string", "description": "替换后的新文本"},
    },
    "required": ["path", "old_string", "new_string"],
}


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        path = resolve_path(ctx, args["path"])
        old, new = args["old_string"], args["new_string"]
        if not path.is_file():
            return tool_result_err(call, "model", f"文件不存在: {path}")
        if old == new:
            return tool_result_err(call, "model", "old_string 与 new_string 相同，无需修改")
        text = path.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            return tool_result_err(
                call, "model",
                f"old_string 在 {path} 中未找到（注意空白/换行必须完全一致）；"
                "请先用 read_file 查看确切内容")
        if n > 1:
            return tool_result_err(
                call, "model",
                f"old_string 在文件中出现 {n} 次，不满足唯一匹配；"
                "请扩大 old_string 上下文范围使其唯一")
        updated = text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return tool_result_ok(
            call, f"已修改 {path}（替换 1 处）",
            meta={"path": str(path), "replaced": 1,
                  "sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest()[:16]})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_edit", target=str(resolve_path(ctx, args["path"])),
                      detail={"path": args["path"],
                              "old_len": len(args.get("old_string", "")),
                              "new_len": len(args.get("new_string", ""))})

    return ToolSpec(
        name="edit_file",
        description=("对文件做唯一匹配的文本替换。old_string 必须原样匹配（含缩进/换行）且全局唯一；"
                     "出现 0 次或多次都会报错并提示原因。修改前自动快照。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.MUTATE,
        mutating=True,
        parallel_safe=False,
    )
