"""破坏性命令解析：识别纯删除命令，供 shell 工具路由到回收站 + CommandRule 批量检测。

覆盖：rm / unlink / rmdir / del / shred（含常见 flags）。解析失败或非纯删除 → None
（按普通 shell 命令执行，仍受命令规则管控）。
"""
from __future__ import annotations

import shlex

_DELETE_CMDS = {"rm", "unlink", "rmdir", "del", "erase", "shred"}


def _strip_quoted(s: str) -> str:
    """去掉引号段（引号内的 `|`/`;` 不是命令分隔符）。"""
    out, quote = [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        out.append(ch)
    return "".join(out)


def parse_delete_command(cmd: str) -> list[str] | None:
    """返回待删路径列表；非纯删除命令返回 None。

    v0.4.29 P1：复合命令（含 `&&`/`;`/`|` 等壳元字符）一律返回 None——
    旧实现把元字符后面的 token 全当删除路径（`rm -f x.o && gcc main.c -o app`
    会误删 main.c/app 且 gcc 永不执行）；这类命令按普通 shell 执行，
    仍受 CommandRule 高危审批。Windows（posix=False）下剥除引号残渣。
    """
    posix = True
    try:
        import os
        posix = os.name != "nt"
        tokens = shlex.split(cmd, posix=posix)
    except ValueError:
        return None
    if not tokens:
        return None
    # 复合命令 → 不按纯删除路由（元字符可能是命令分隔符）。
    # v0.4.29：按**去引号后的原串**检测（shlex posix=False 会把 `a.txt;`
    # 粘成一个 token，逐 token 等值比较抓不到 `;`）
    plain = _strip_quoted(cmd)
    # v0.4.30：补单个 `&`（cmd 串行分隔符）与换行——旧实现只查 `&&`，
    # `del a.txt & del b.txt` 会把 a.txt 搬走而 del b.txt 永不执行
    if any(m in plain for m in ("&&", "||", ";", "|", "`", "$(", ">", "<",
                                "\n", "\r")):
        return None
    if "&" in plain.replace("&&", ""):
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
        # v0.4.29：Windows 开关（del /s /q /f…）不是路径——旧实现把它们当
        # 删除目标，真实文件反而漏删
        if len(t) == 2 and t[0] == "/" and t[1].isalpha():
            continue
        # v0.4.29：Windows posix=False 时引号保留在 token 内 → 剥掉，
        # 否则 `rm "my file.txt"` 永远匹配不到真实文件（静默无操作）
        paths.append(t.strip('"').strip("'"))
    return paths or None
