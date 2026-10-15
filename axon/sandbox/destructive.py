"""破坏性命令解析：识别纯删除命令，供 shell 工具路由到回收站 + CommandRule 批量检测。

覆盖：rm / unlink / rmdir / del / shred（含常见 flags）。解析失败或非纯删除 → None
（按普通 shell 命令执行，仍受命令规则管控）。
"""
from __future__ import annotations

import shlex

_DELETE_CMDS = {"rm", "unlink", "rmdir", "del", "erase", "shred"}


def parse_delete_command(cmd: str) -> list[str] | None:
    """返回待删路径列表；非纯删除命令返回 None。"""
    posix = True
    try:
        import os
        posix = os.name != "nt"
        tokens = shlex.split(cmd, posix=posix)
    except ValueError:
        return None
    if not tokens:
        return None
    # 跳过 env / sudo 前缀
    i = 0
    while i < len(tokens) and tokens[i] in ("sudo", "env") or (
            i < len(tokens) and "=" in tokens[i] and i == 0):
        i += 1
    if i >= len(tokens):
        return None
    prog = tokens[i].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if prog not in _DELETE_CMDS:
        return None
    paths: list[str] = []
    for t in tokens[i + 1:]:
        if t.startswith("-"):
            continue          # flags: -r -f -i -v -rf ...
        if t in ("--",):
            continue
        paths.append(t)
    return paths or None
