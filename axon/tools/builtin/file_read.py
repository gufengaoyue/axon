"""read_file：读文件内容（行切片）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_err, tool_result_ok

_MAX_CHARS = 60000

PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径（相对工作空间或绝对路径）"},
        "offset": {"type": "integer", "description": "起始行号（从 1 开始，默认 1）", "default": 1},
        "limit": {"type": "integer", "description": "最多读取行数（默认 400）", "default": 400},
    },
    "required": ["path"],
}


def resolve_path(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (ctx.workspace / p)


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        path = resolve_path(ctx, args["path"])
        if not path.is_file():
            return tool_result_err(call, "model", f"文件不存在: {path}")
        offset = max(1, int(args.get("offset", 1)))
        limit = int(args.get("limit", 400))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return tool_result_err(call, "permanent", f"读取失败: {e}")
        lines = text.splitlines()
        total = len(lines)
        chunk = lines[offset - 1: offset - 1 + limit]
        out = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start=offset))
        if len(out) > _MAX_CHARS:
            out = out[:_MAX_CHARS] + f"\n...(输出超 {_MAX_CHARS} 字符已截断，请用 offset/limit 分段读取)"
        header = f"# {path} (共 {total} 行，本次 {offset}-{offset - 1 + len(chunk)})"
        return tool_result_ok(call, f"{header}\n{out}",
                              meta={"path": str(path), "lines": total,
                                    "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_read", target=str(resolve_path(ctx, args["path"])),
                      detail={"path": args["path"]})

    return ToolSpec(
        name="read_file",
        description=("读取文件内容，返回带行号的文本。"
                     "path 相对路径基于工作空间解析；大文件用 offset/limit 分段读。"
                     "输出超过 60000 字符会截断。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.READ,
        parallel_safe=True,
    )
