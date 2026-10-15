"""工具消息的 meta 持久化约定。

ToolResult.meta / error_type 需落库供 V 层回读校验（sha256/exit_code/error_type），
但 messages 表无 meta 列（设计 DDL 固定）→ 以尾部标记序列化进 content；
ContextBuilder 组装时剥掉，模型上下文保持干净且确定。
"""
from __future__ import annotations

import json

_MARK = "<meta>"


def pack_tool_content(output: str, meta: dict | None, error_type: str | None = None) -> str:
    payload = dict(meta or {})
    if error_type:
        payload["error_type"] = error_type
    if not payload:
        return output
    return f"{output}\n{_MARK}{json.dumps(payload, ensure_ascii=False, sort_keys=True)}</meta>"


def unpack_meta(content: str | None) -> tuple[str, dict | None]:
    if not content or _MARK not in content:
        return content or "", None
    idx = content.rfind(f"\n{_MARK}")
    if idx == -1:
        return content, None
    raw = content[idx + len(_MARK) + 1:]
    end = raw.rfind("</meta>")
    if end == -1:
        return content, None
    try:
        meta = json.loads(raw[:end])
    except ValueError:
        return content[:idx], None
    return content[:idx], meta
