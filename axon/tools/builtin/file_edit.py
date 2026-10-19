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
        # v0.4.29 P1：字节域操作——旧实现 read_text(errors="replace") 把非
        # UTF-8（GBK 等）文件的非法字节变成 U+FFFD，再以 UTF-8 写回 → 原字节
        # 永久丢失（用户无感知，快照可恢复但仍是数据破坏）。现在按字节匹配
        # 替换，文件其余部分（含其他编码字节）原样保留。
        try:
            raw = path.read_bytes()
        except OSError as e:
            return tool_result_err(call, "resource", f"读取失败: {e}")
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        n = raw.count(old_b)
        # v0.4.30：换行域对齐——write_file 在 Windows 落盘为 CRLF，read_file
        # 以 universal newlines 显示为 LF；模型用 LF 的 old_string 在字节域
        # 必然不匹配（"自写→自读→自改"闭环断裂）。0 命中时按 CRLF/LF 互换重试，
        # 命中后 new_string 用同一风格生成。
        if n == 0 and "\n" in old:
            for src_nl, dst_nl in (("\n", "\r\n"), ("\r\n", "\n")):
                alt = old.replace(src_nl, dst_nl).encode("utf-8")
                if alt in raw:
                    old_b = alt
                    new_b = new.replace(src_nl, dst_nl).encode("utf-8")
                    n = raw.count(old_b)
                    break
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
        updated = raw.replace(old_b, new_b, 1)
        try:
            path.write_bytes(updated)
        except OSError as e:
            return tool_result_err(call, "resource", f"写入失败: {e}")
        return tool_result_ok(
            call, f"已修改 {path}（替换 1 处）",
            meta={"path": str(path), "replaced": 1,
                  "sha256": hashlib.sha256(updated).hexdigest()[:16]})

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
