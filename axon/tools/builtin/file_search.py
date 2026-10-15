"""search_files：快速文件搜索（glob 名匹配 + 内容正则）。"""
from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.builtin.file_read import resolve_path
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_ok

_PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".axon", ".pytest_cache"}
_MAX_FILE_SIZE = 1_000_000

PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "文件名 glob，如 *.py（默认 *）", "default": "*"},
        "content": {"type": "string",
                    "description": "可选：内容正则表达式（命中则返回匹配行）", "default": ""},
        "path": {"type": "string", "description": "搜索子目录（默认工作空间根）", "default": "."},
        "max_results": {"type": "integer", "description": "结果上限（默认 50）", "default": 50},
    },
    "required": [],
}


def make_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        root = resolve_path(ctx, args.get("path") or ".")
        pattern = args.get("pattern") or "*"
        content_re = args.get("content") or ""
        max_results = int(args.get("max_results", 50))
        if not root.exists():
            return tool_result_ok(call, f"目录不存在: {root}", meta={"hits": 0})
        rx = None
        if content_re:
            try:
                rx = re.compile(content_re)
            except re.error as e:
                return tool_result_ok(call, f"正则错误: {e}", meta={"hits": 0})
        hits: list[str] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE]
            for fn in filenames:
                if len(hits) >= max_results:
                    truncated = True
                    break
                if not fnmatchcase(fn, pattern):
                    continue
                full = os.path.join(dirpath, fn)
                if rx is None:
                    hits.append(full)
                    continue
                try:
                    if os.path.getsize(full) > _MAX_FILE_SIZE:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        for ln, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{full}:{ln}:{line.rstrip()[:200]}")
                                if len(hits) >= max_results:
                                    truncated = True
                                    break
                except OSError:
                    continue
            if truncated:
                break
        out = "\n".join(hits) if hits else "(无命中)"
        if truncated:
            out += f"\n...(超过 max_results={max_results} 已截断)"
        return tool_result_ok(call, out, meta={"hits": len(hits), "root": str(root)})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_read", target=str(resolve_path(ctx, args.get("path") or ".")),
                      detail={"pattern": args.get("pattern", "*"),
                              "content": args.get("content", "")})

    return ToolSpec(
        name="search_files",
        description=("快速文件搜索：按文件名 glob 匹配，或按内容正则检索（返回 文件:行号:内容）。"
                     "自动剪枝 .git/node_modules/__pycache__/.venv 等目录；单文件超 1MB 跳过内容匹配。"),
        parameters=PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.READ,
        parallel_safe=True,
    )
