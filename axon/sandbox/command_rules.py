"""命令规则引擎 + 命令安全分析器（v0.1.5 重写）。

语义（用户审定）：
1. **读/搜索类命令免询问**：先把复合命令拆成原子命令（&& || ; | & 尊重引号），
   逐个判定程序性质；全部只读且无文件重定向 → 直接放行
   （路径命中 deny 表仍硬拦——读=内容外发模型 API，防数据外泄）
2. **分级信任区**（axon.sandbox.path_policy）：
   - 全权限区 = 工作空间 + workspace/.axon/tmp → 智能体可处理所有事情
     （文件写/删/跑脚本皆可；批量删除超阈值仍询问）
   - 信任区 = 全权限区 + 项目目录 → 文件写/删免确认
   - 区外：读放行；写/删/系统级命令 → fail-closed 询问
3. **批量删除阈值**（默认 100）：删除路径数超过阈值 → 无论在哪都询问
4. 解释器/系统包管理等"效果不透明"的命令：仅有工作空间内路径证据才放行
5. deny 清单（rm -rf / 等）永远优先；白名单绕过仅跳命令规则
6. 显式 allow/ask 配置兜底；未命中 → 默认询问（fail-closed）
"""
from __future__ import annotations

import glob as _glob
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from axon.sandbox.destructive import parse_delete_command
from axon.sandbox.gateway import Action, Decision
from axon.sandbox.path_policy import (default_deny_paths, full_zones,
                                        in_zones, program_dir_norm,
                                        trusted_zones)

# ---------------------------------------------------------------- 程序分类表

_READ_ONLY_EXTRA = {"more", "where", "tree", "fc", "comp", "findstr",
                     "echo", "printf", "cls", "date", "time",
                     "tasklist", "ver", "vol", "systeminfo", "whoami", "hostname",
                     "ipconfig", "netstat", "ping", "nslookup", "chcp", "set"}
_READ_ONLY = {
    # POSIX / 核心
    "ls", "dir", "cat", "head", "tail", "more", "less", "echo", "printf", "pwd",
    "cd", "true", "false", "find", "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "findstr", "where", "which", "type", "tree", "wc", "du", "df", "stat", "file",
    "readlink", "realpath", "basename", "dirname", "sort", "uniq", "cut", "comm",
    "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "cksum", "od", "xxd",
    "hexdump", "env", "printenv", "set", "date", "cal", "whoami", "id", "groups",
    "hostname", "uname", "arch", "nproc", "lsb_release", "uptime", "free", "ps",
    "pgrep", "lscpu", "lsblk", "lsusb", "lspci", "getent", "seq",
    # Windows 内置
    "ver", "systeminfo", "tasklist", "driverquery", "ipconfig", "netstat", "route",
    "arp", "nslookup", "tracert", "path", "prompt", "vol", "wmic", "fsutil",
    # PowerShell 只读 cmdlet
    "get-childitem", "get-content", "get-item", "get-location", "select-string",
    "test-path", "select-object", "where-object", "foreach-object", "measure-object",
    "group-object", "sort-object", "unique", "get-command", "get-help", "get-process",
    "get-service", "get-date", "get-acl", "get-itemproperty",
    # git / 包管理只读子命令单独判定（见 _READ_ONLY_SUBCMD）
    "git", "npm", "pip", "pip3", "docker", "kubectl", "helm", "reg",
}

_READ_ONLY_SUBCMD = {
    "git": {"status", "diff", "log", "show", "blame", "branch", "remote", "tag",
            "ls-files", "ls-remote", "rev-parse", "describe", "shortlog", "reflog",
            "config", "grep", "whatchanged", "cat-file", "symbolic-ref", "stash"},
    "npm": {"ls", "list", "ll", "la", "view", "info", "show", "outdated", "root",
            "prefix", "explain", "search", "whoami", "audit", "fund", "doctor"},
    "pip": {"list", "show", "freeze", "check", "debug", "search"},
    "pip3": {"list", "show", "freeze", "check", "debug", "search"},
    "docker": {"ps", "images", "logs", "inspect", "stats", "top", "version", "info",
               "port", "diff", "history", "network", "volume", "system", "container",
               "image", "search"},
    "kubectl": {"get", "describe", "logs", "top", "explain", "api-resources",
                "api-versions", "cluster-info", "version", "config", "events"},
    "helm": {"list", "status", "history", "get", "show", "search", "env", "version"},
    "reg": {"query"},
}

# 这些 flag 出现即视为非只读（即便是只读子命令）
_MUTATING_FLAGS = {"-d", "-D", "-m", "--delete", "--move", "--set-upstream", "-u",
                   "--force", "-f", "--amend", "--mirror", "-x"}

_DELETE_PROGRAMS = {"rm", "del", "erase", "rmdir", "rd", "unlink", "remove-item",
                    "srm", "shred"}
_RECURSIVE_FLAGS = {"-r", "-R", "--recursive", "/s", "-rf", "-fr", "-recurse",
                    "-recursive", "-force"}

_FILE_WRITE_PROGRAMS = {
    "cp", "copy", "xcopy", "robocopy", "mv", "move", "ren", "rename", "mkdir", "md",
    "touch", "new-item", "set-content", "out-file", "add-content", "clear-content",
    "tee",
}

# 效果不透明/系统级：仅有工作空间路径证据才可能放行
_SYSTEM_MUTATE = {
    "pip", "pip3", "npm", "npx", "yarn", "pnpm", "apt", "apt-get", "yum", "dnf",
    "pacman", "brew", "choco", "scoop", "winget", "setx", "reg", "kill", "taskkill",
    "stop-process", "pkill", "shutdown", "reboot", "restart", "poweroff", "halt",
    "format", "diskpart", "mkfs", "fdisk", "dd", "chmod", "chown", "chgrp",
    "attrib", "icacls", "cacls", "setfacl", "chattr", "useradd", "userdel",
    "usermod", "passwd", "net", "sc", "systemctl", "service", "launchctl",
    "crontab", "schtasks", "ssh", "scp", "sftp", "rsync", "curl", "wget",
    "certutil", "openssl", "tar", "zip", "unzip", "gzip", "gunzip", "7z", "xz",
    "sqlite3", "mysql", "psql", "sed", "awk", "truncate", "fallocate", "sponge",
    "make", "cmake", "ninja", "gcc", "g++", "cc", "clang", "cargo", "go", "dotnet",
    "java", "javac", "mvn", "gradle",
}

# 解释器/外壳：解包分析内层；内层不可解析时按不透明处理
_INTERPRETERS = {"python", "python3", "py", "node", "deno", "bun", "ruby", "perl",
                 "php", "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "fish"}
_WRAPPERS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "fish"}

_FIND_MUTATING_FLAGS = {"-delete", "-exec", "-execdir", "-fprint", "-fprintf", "-fls"}

# ---- 解释器代码危险信号（v0.2.2 用户审定：python 不涉及删除就放行） ----
_INTERP_DELETE_RE = re.compile(
    r"\b(?:os\.remove|os\.unlink|os\.rmdir|os\.removedirs|os\.truncate"
    r"|shutil\.rmtree|send2trash|\.unlink|\.rmdir|Path\([^)]*\)\.unlink"
    r"|rmtree)\s*\(")
_INTERP_SHELL_RE = re.compile(
    r"\b(?:subprocess|os\.system|os\.popen|os\.spawn\w*|pty\.spawn"
    r"|commands\.getoutput|child_process|process\.binding)\b")
_INTERP_DYNEXEC_RE = re.compile(r"\b(?:eval|exec|compile)\s*\(")
_INTERP_SYSMUT_RE = re.compile(
    r"\b(?:os\.chmod|os\.chown|os\.rename|shutil\.move|shutil\.copy[a-z]*"
    r"|os\.mkdir|os\.makedirs|\.write_text|\.write_bytes|os\.symlink)\b"
    r"|open\s*\([^)]*['\"][wax]")
# 删除调用的字面量参数（用于计数与路径提取）：os.remove('a.txt') / Path('x').unlink()
_INTERP_DELETE_LITERAL_RE = re.compile(
    r"(?:os\.remove|os\.unlink|os\.rmdir|\.unlink|\.rmdir|send2trash)\s*\(\s*"
    r"(['\"])(.+?)\1\s*\)")
_INTERP_DELETE_LITERAL2_RE = re.compile(
    r"(?:shutil\.rmtree|rmtree)\s*\(\s*(['\"])(.+?)\1")
# 动态目标（变量/glob/循环）→ 不可静态计数
_INTERP_DYNTARGET_RE = re.compile(r"\b(?:glob\.|Path\([^)]*\)\.glob|rglob|iterdir"
                                  r"|os\.walk|listdir)\b")

# 重定向：>nul / 2>&1 / >/dev/null 不算写文件
_REDIR_CLEAN = re.compile(r"\d*>&\d*|\d*>>?\s*(?:nul|/dev/null)", re.IGNORECASE)
_REDIR_TARGET = re.compile(r">>?\s*([^\s|&;>\"]+)")
_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/|~)")
_WILDCARD_RE = re.compile(r"[*?\[]")


# ---------------------------------------------------------------- 数据结构

@dataclass
class Atom:
    program: str
    args: list[str]
    raw: str


@dataclass
class CmdAnalysis:
    kind: str                      # read_only | delete | file_write | system_mutate | unknown
    delete_count: int | None = None
    paths: list[str] = field(default_factory=list)
    has_redirection: bool = False
    has_substitution: bool = False
    atoms: list[Atom] = field(default_factory=list)
    base: str = ""
    interp_only_write: bool = False    # 变更仅来自解释器代码（无危险信号）
    interp_deletes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 分词/拆分

def _strip_quoted(s: str) -> str:
    out, quote = "", None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        out += ch
    return out


def strip_void_redirects(cmd: str) -> str:
    """剔除**无目标**重定向：`2>&1`、`>nul`、`2>nul`、`&>nul`、`2>/dev/null`。

    v0.3.1 修复：`2>&1` 里的 `&` 会被原子切分器当作命令连接符 →
    `dir … 2>&1` 被切成 `dir … 2>` + `1`，孤立 `>` 触发"写文件"判定，
    且后续 `1` 被当成路径。先剔除可一次性消除这类误判。
    """
    cmd = re.sub(r"\d*>&\d*", " ", cmd)                      # 2>&1 / >&2
    cmd = re.sub(r"&?\s*\d*>>?\s*(?:nul|/dev/null)\b", " ", cmd,
                 flags=re.IGNORECASE)                         # >nul / 2>nul / &>nul
    return cmd


def _split_atoms(cmd: str) -> list[str]:
    """按 && || ; | & 拆分复合命令（尊重引号）。"""
    atoms, cur, quote, i, n = [], "", None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
            i += 1
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            atoms.append(cur)
            cur = ""
            i += 2
            continue
        if ch in ";|&":
            atoms.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    if cur.strip():
        atoms.append(cur)
    return [a.strip() for a in atoms if a.strip()]


def _tokenize(s: str) -> list[str]:
    tokens, cur, quote = [], "", None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if cur:
                tokens.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    return tokens


def is_trusted_venv_python(prog_raw: str) -> bool:
    """解释器是否位于**程序级共享 venv** 内（v0.4.4）。

    `python -m pip install` 装到哪由解释器决定，命令行里看不出路径；
    因此用它来判断"这次装包属于信任区内的正常操作"，避免反复询问。
    """
    name = str(prog_raw).strip().strip('"').strip("'")
    if not name or "venv" not in name.lower():
        return False
    try:
        from axon.config import program_root
        p = Path(os.path.realpath(name))
        venv = program_root() / "venv"
        return str(p).lower().startswith(str(venv).lower())
    except (OSError, ValueError):
        return False


def _unwrap_inner(program: str, args: list[str]) -> str | None:
    """powershell -Command "..." / cmd /c "..." / bash -c "..." → 内层命令串。

    v0.4.4：返回前**剥掉外层引号**——否则递归分析时整串被当成"引号内容"而不切分，
    内层命令无法被判类（用户实测：`cmd /c "cd /d … & py …"` 判成 unknown 而反复询问）。
    """
    def _strip_quotes(text: str) -> str:
        t = text.strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            return t[1:-1]
        return t

    if program in ("powershell", "pwsh"):
        for i, a in enumerate(args):
            if a.lower() in ("-command", "-c") and i + 1 < len(args):
                return _strip_quotes(" ".join(args[i + 1:]))
        return None
    if program == "cmd":
        for i, a in enumerate(args):
            if a.lower() in ("/c", "/k") and i + 1 < len(args):
                return _strip_quotes(" ".join(args[i + 1:]))
        return None
    if program in ("bash", "sh", "zsh", "fish"):
        for i, a in enumerate(args):
            if a == "-c" and i + 1 < len(args):
                return _strip_quotes(" ".join(args[i + 1:]))
        return None
    return None


# ---------------------------------------------------------------- 分析器

def analyze(cmd: str, workspace) -> CmdAnalysis:
    """分析 shell 命令：拆原子命令 → 分类 → 提取路径 → 统计删除数。"""
    workspace = Path(workspace)
    cmd = strip_void_redirects(cmd)          # 先剔除无目标重定向（v0.3.1）
    a = CmdAnalysis(kind="read_only", base=str(workspace))
    a.has_redirection = ">" in _REDIR_CLEAN.sub(" ", _strip_quoted(cmd))
    stripped = _strip_quoted(cmd)
    a.has_substitution = "$(" in stripped or "`" in stripped
    if a.has_substitution:
        a.kind = "unknown"
        return a

    base = workspace
    kinds: list[str] = []
    delete_count = 0
    saw_delete = False
    unbounded_delete = False

    for atom_raw in _split_atoms(cmd):
        toks = _tokenize(atom_raw)
        if not toks:
            continue
        # 跳过 sudo / env 前缀
        i = 0
        while i < len(toks) and toks[i].lower() in ("sudo", "env", "command",
                                                    "nohup", "time"):
            i += 1
        if i >= len(toks):
            continue
        prog = os.path.basename(toks[i].replace("\\", "/")).lower()
        # 去掉 .exe/.cmd 后缀
        for suf in (".exe", ".cmd", ".bat", ".ps1", ".com"):
            if prog.endswith(suf):
                prog = prog[: -len(suf)]
        args = toks[i + 1:]
        atom = Atom(program=prog, args=args, raw=atom_raw)
        a.atoms.append(atom)

        # 程序级共享 venv 内的 `python -m pip install …` → 视为信任区内的正常装包（v0.4.4）
        if prog == "python" and len(args) >= 2 and args[0] == "-m" \
                and args[1].lower() in ("pip", "ensurepip") \
                and is_trusted_venv_python(toks[i]):
            kinds.append("read_only")
            continue

        if prog == "cd":
            for t in args:
                if t.startswith("-") and not _PATH_RE.match(t):
                    continue
                cand = _resolve_one(t, base, workspace)
                if cand is not None and Path(cand).is_absolute():
                    base = Path(cand)
            continue

        # 解释器/外壳 → 解包内层递归分析（深度 1）
        inner = _unwrap_inner(prog, args)
        if inner is not None:
            sub = analyze(inner, base)
            kinds.append(sub.kind)
            if sub.kind == "delete":
                saw_delete = True
                delete_count += max(sub.delete_count or 0, 0)
            a.paths.extend(sub.paths)
            continue

        kind = _classify_atom(prog, args, base, workspace)
        # 重定向到文件 = 写操作（2>nul 等已在 _REDIR_CLEAN 排除）
        if ">" in _REDIR_CLEAN.sub(" ", _strip_quoted(atom_raw)) and kind == "read_only":
            kind = "file_write"
            for m in _REDIR_TARGET.finditer(_strip_quoted(atom_raw)):
                t = m.group(1)
                if t.lower() in ("nul", "/dev/null"):
                    continue
                r = _resolve_one(t, base, workspace)
                if r:
                    a.paths.append(r)
        kinds.append(kind)
        if prog in _INTERPRETERS:
            # 解释器：删除计数与路径来自代码字面量（参数里的代码串不是路径）
            if kind == "delete":
                saw_delete = True
                code = _interp_code(prog, args, base, workspace) or ""
                cnt, dps = _interp_delete_info(code, base, workspace)
                a.paths.extend(dps)
                if cnt < 0:
                    unbounded_delete = True
                else:
                    delete_count += cnt
                    a.interp_deletes.extend(dps)
            elif kind == "file_write":
                a.interp_only_write = True
            continue

        if kind == "delete":
            saw_delete = True
            delete_count += _count_delete(args, base, workspace)
        if kind in ("delete", "file_write"):
            # 删除/写程序的非 flag 参数都是操作数（含裸文件名），按路径提取；
            # 但要排除 cmd 开关与引号残渣（v0.3.1）
            for t in args:
                if is_switch_token(t, prog):
                    continue
                if _WILDCARD_RE.search(t):
                    pat = t if os.path.isabs(t) else str(base / t)
                    hits = _glob.glob(pat, recursive=True)
                    a.paths.extend(hits[:1000])
                    continue
                r = _resolve_one(t, base, workspace)
                if r:
                    a.paths.append(r)
        elif kind == "read_only":
            # 读命令：路径形态 token 或真实存在的裸相对名（供 deny 检查）；
            # cmd 开关（/s /b /C:xxx）与引号残渣不是路径（v0.3.1）
            for t in args:
                if is_switch_token(t, prog):
                    continue
                if _WILDCARD_RE.search(t):
                    pat = t if os.path.isabs(t) else str(base / t)
                    hits = _glob.glob(pat, recursive=True)
                    a.paths.extend(hits[:1000])
                    continue
                if _looks_like_path(t) or os.path.exists(str(base / t)):
                    r = _resolve_one(t, base, workspace)
                    if r:
                        a.paths.append(r)
        else:
            # system_mutate / unknown：提取路径形态或真实存在的 token
            #（如 python build.py）；纯裸名不存在则视为选项值不提取
            for t in args:
                if is_switch_token(t, prog):
                    continue
                if _WILDCARD_RE.search(t):
                    pat = t if os.path.isabs(t) else str(base / t)
                    hits = _glob.glob(pat, recursive=True)
                    a.paths.extend(hits[:1000])
                    continue
                if _looks_like_path(t) or os.path.exists(str(base / t)):
                    r = _resolve_one(t, base, workspace)
                    if r:
                        a.paths.append(r)

    if saw_delete:
        a.kind = "delete"
        a.delete_count = -1 if unbounded_delete else delete_count
    elif "file_write" in kinds:
        a.kind = "file_write"
    elif "system_mutate" in kinds:
        a.kind = "system_mutate"
    elif "unknown" in kinds:
        a.kind = "unknown"
    else:
        a.kind = "read_only"
    a.base = str(base)
    return a


def _resolve_one(t: str, base: Path, workspace: Path) -> str | None:
    t = normalize_fs_path(t)        # v0.4.4：/cygdrive/d/... 等折算为本机盘符路径
    if t.startswith("~"):
        return str(Path.home() / t[2:]) if len(t) > 2 else str(Path.home())
    p = Path(t)
    if not p.is_absolute():
        p = base / p
    try:
        return str(Path(os.path.realpath(str(p))))
    except OSError:
        return str(p)


def _classify_atom(prog: str, args: list[str], base: Path | None = None,
                   workspace: Path | None = None) -> str:
    prog = prog_key(prog)          # v0.4.4：剥可执行后缀后再判类
    if prog in _SUB_SHELL_PROGRAMS and any(
            a.lower() in ("/c", "/k", "-c", "-command", "--command") for a in args):
        return "subshell"          # v0.4.4：内层命令串需递归分析（见 analyze）
    if prog in _READ_ONLY_EXTRA:
        return "read_only"          # 纯查询/读取类（v0.3.1 用户要求）
    if prog in _DELETE_PROGRAMS:
        return "delete"
    if prog == "find":
        if any(x in _FIND_MUTATING_FLAGS for x in args):
            return "file_write"
        return "read_only"
    if prog in _FILE_WRITE_PROGRAMS:
        return "file_write"
    if prog in _READ_ONLY:
        sub = _READ_ONLY_SUBCMD.get(prog)
        if sub is not None:
            # 需要只读子命令 + 无变更 flag
            positional = [x for x in args if not x.startswith("-")]
            if not positional:
                return "read_only"          # git 无子命令 → 打印 usage
            if positional[0].lower() not in sub:
                return "system_mutate"
            if any(x in _MUTATING_FLAGS for x in args):
                return "system_mutate"
        return "read_only"
    if prog in _SYSTEM_MUTATE:
        return "system_mutate"
    if prog in _INTERPRETERS:
        # v0.2.2：先做代码级危险信号扫描——无删除/无 shell 逃逸 → 直接放行
        code = _interp_code(prog, args, base, workspace)
        if code is None:
            return "unknown"                # -m 模块/无代码可析 → 不透明
        has_delete = bool(_INTERP_DELETE_RE.search(code))
        has_shell = bool(_INTERP_SHELL_RE.search(code))
        has_dynexec = bool(_INTERP_DYNEXEC_RE.search(code))
        has_sysmut = bool(_INTERP_SYSMUT_RE.search(code))
        if has_shell or has_dynexec:
            return "unknown"                # 可执行任意命令 → 保持不透明（询问）
        if has_delete:
            return "delete"                 # 交由网关按阈值/信任区判定
        if has_sysmut:
            return "file_write"             # 文件级变更（信任区内放行）
        return "read_only"                  # 计算/打印/读/网络请求 → 放行
    return "unknown"


def _interp_code(prog: str, args: list[str], base: Path | None,
                 workspace: Path | None) -> str | None:
    """提取解释器要执行的代码文本：-c/-e 内联串 → 返回；脚本文件 → 读前 512KB。

    返回 None 表示"无代码可析"（如 `python --version`、`python -m xxx`）——
    此时按不透明处理（询问）；`--version` 这类只读开关放行。
    """
    flags = {"-c", "-e", "--eval", "-Command", "-command"}
    safe_flags = {"--version", "-V", "-v", "--help", "-h"}
    if args and args[0].lower() in {f.lower() for f in safe_flags}:
        return ""                           # 只读开关 → 空代码 = 安全
    i = 0
    while i < len(args):
        a = args[i]
        if a.lower() in {f.lower() for f in flags} and i + 1 < len(args):
            return args[i + 1]              # 内联代码
        if a.startswith("-"):
            if a.lower().startswith("-m"):
                return None                 # -m 模块 → 不透明
            i += 1
            continue
        # 第一个非 flag 参数：脚本文件
        if base is None:
            return None
        script = Path(a)
        if not script.is_absolute():
            script = base / script
        try:
            if script.is_file() and script.stat().st_size <= 512 * 1024:
                return script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None                         # 脚本不存在/过大 → 不透明
    return ""                               # 无参数（如 `python`）→ 交互式，按安全处理


def _interp_delete_info(code: str, base: Path,
                        workspace: Path) -> tuple[int, list[str]]:
    """从解释器代码提取删除调用：全部为字面量 → (N, 路径表)；含动态目标 → (-1, [])。

    N 按**实际文件数**计（目录目标展开 bounded walk），避免 rmtree('大目录')
    被低估成 1 个删除而绕过批量阈值。
    """
    literals: list[str] = []
    for m in _INTERP_DELETE_LITERAL_RE.finditer(code):
        literals.append(m.group(2))
    for m in _INTERP_DELETE_LITERAL2_RE.finditer(code):
        literals.append(m.group(2))
    if _INTERP_DYNTARGET_RE.search(code):
        return -1, []                       # 变量/glob/遍历 → 不可静态计数
    # 删除调用数 > 字面量数 → 存在非字面量目标
    total_calls = len(_INTERP_DELETE_RE.findall(code))
    if total_calls > len(literals):
        return -1, []
    paths: list[str] = []
    total = 0
    cap = 10000
    for lit in literals:
        p = Path(lit)
        if not p.is_absolute():
            p = base / p
        rp = Path(os.path.realpath(str(p)))
        paths.append(str(rp))
        try:
            if rp.is_dir():
                for _root, _dirs, files in os.walk(rp):
                    total += len(files)
                    if total > cap:
                        return -1, paths
            else:
                total += 1
        except OSError:
            total += 1
    return total, paths


# Windows/cmd 风格命令：其 `/x` 形式的参数是**开关**（/s /b /C:xxx /a-d），不是路径。
# 曾把 `/s` 解析成 `D:\s`、`/C:"slide"` 解析成 `C:slide`（盘根）→ 误判"写信任区外"。
_CMD_STYLE_PROGRAMS = {"dir", "findstr", "type", "find", "where", "tree", "attrib",
                       "forfiles", "robocopy", "xcopy", "icacls", "fc", "comp",
                       "more", "sort", "tasklist", "sc", "reg", "net", "copy",
                       "move", "del", "rd", "md", "mkdir", "rmdir", "ren", "echo",
                       "set", "cd", "chdir", "call", "start", "assoc", "ver", "vol",
                       "taskkill", "systeminfo", "wmic", "netsh", "powercfg"}


_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".py")

# 会"转交"执行内层命令串的壳（`cmd /c "…"`、`bash -c "…"`）——v0.4.4
_SUB_SHELL_PROGRAMS = {"cmd", "sh", "bash", "zsh", "dash", "ksh",
                       "powershell", "pwsh", "cscript"}


def prog_key(name: str) -> str:
    """程序名归一化：取 basename、小写、剥可执行后缀。

    v0.4.4 修复（用户实测）：`cmd` 带扩展名（cmd 点 exe）时程序名不在
    `_CMD_STYLE_PROGRAMS` 集合里 → `/c` 被当成路径解析成 `C:\\c`（盘根）
    → 误报"写信任区外"。
    """
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suf in _EXE_SUFFIXES:
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def normalize_fs_path(token: str) -> str:
    """把类 Unix 风格路径折算成本机路径（v0.4.4 修复，用户实测）。

    支持（Windows 上）：
      `/cygdrive/d/Python/AI`  → `D:\\Python\\AI`（Cygwin/MSYS）
      `/mnt/d/Python`          → `D:\\Python`（WSL）
      `/d/Python/AI`           → `D:\\Python\\AI`（Git Bash 盘符写法）
    其它平台原样返回（避免误伤真实 Unix 路径）。
    """
    if os.name != "nt" or not token.startswith("/"):
        return token
    parts = [p for p in token.split("/") if p]
    if not parts:
        return token
    drive = None
    rest: list[str] = []
    if parts[0].lower() == "cygdrive" and len(parts) >= 2 and len(parts[1]) == 1:
        drive, rest = parts[1], parts[2:]
    elif parts[0].lower() == "mnt" and len(parts) >= 2 and len(parts[1]) == 1:
        drive, rest = parts[1], parts[2:]
    elif len(parts[0]) == 1 and parts[0].isalpha():
        drive, rest = parts[0], parts[1:]
    if drive is None:
        return token
    tail = "\\".join(rest)
    return f"{drive.upper()}:\\{tail}" if tail else f"{drive.upper()}:\\"


def is_switch_token(token: str, program: str = "") -> bool:
    """`/s`、`/b`、`/C:xxx`、`-flag`、带引号残渣 这类非路径参数（v0.3.1）。"""
    if not token:
        return True
    if '"' in token or "'" in token:
        return True
    if token.startswith("-") and not _PATH_RE.match(token):
        return True
    if prog_key(program) in _CMD_STYLE_PROGRAMS and re.match(r"^/[A-Za-z]", token):
        # cmd 开关形态：/s /b /a-d /C:xxx（tokenizer 会把引号转成 `/`，
        # 故这里不能用"只含一个 /"来判定）
        return True
    return False


def _looks_like_path(t: str) -> bool:
    if t.startswith("-") or not t:
        return False
    if _WILDCARD_RE.search(t):
        return True                     # glob 交给调用方展开
    return bool(_PATH_RE.match(t)) or "\\" in t or "/" in t


def _extract_paths(args: list[str], base: Path, workspace: Path) -> list[str]:
    out = []
    for t in args:
        if not _looks_like_path(t):
            continue
        if _WILDCARD_RE.search(t):
            # glob 展开（相对 base），最多 1000 个
            pat = t if os.path.isabs(t) else str(base / t)
            hits = _glob.glob(pat, recursive=True)
            out.extend(hits[:1000])
            continue
        r = _resolve_one(t, base, workspace)
        if r:
            out.append(r)
    return out


def _count_delete(args: list[str], base: Path, workspace: Path,
                  cap: int = 10000) -> int:
    recursive = any(x in _RECURSIVE_FLAGS for x in args)
    total = 0
    for t in args:
        if t.startswith("-"):
            continue
        if _WILDCARD_RE.search(t):
            pat = t if os.path.isabs(t) else str(base / t)
            hits = _glob.glob(pat, recursive=True)
            total += len(hits)
            continue
        r = _resolve_one(t, base, workspace)
        if not r:
            continue
        if os.path.isdir(r) and recursive:
            for _root, _dirs, files in os.walk(r):
                total += len(files)
                if total > cap:
                    return -1           # 超限不可数 → 视为超阈值
        elif os.path.exists(r):
            total += 1
        else:
            total += 1                  # 不存在的操作数也计数（防 "rm 占位名" 绕过）
    return total


# ---------------------------------------------------------------- Hook

class CommandRuleHook:
    name = "CommandRule"

    def __init__(self, config):
        self.config = config
        self.rules = config.sandbox.commands
        self.bypass = config.sandbox.command_bypass_whitelist
        self.threshold = config.sandbox.batch_delete_threshold
        self._deny_paths = default_deny_paths()

    def pre(self, action: Action, ctx) -> Decision | None:
        if action.kind != "shell":
            return None
        cmd = action.target.strip()

        # 1. 白名单绕过（仅跳命令规则；路径/网络/审计仍生效）
        b = _match_any(cmd, self.bypass)
        if b:
            return Decision("allow", f"command:bypass:{b}", "命令白名单绕过")

        # 2. deny 清单优先
        d = _match_any(cmd, self.rules.deny)
        if d:
            return Decision("deny", f"command:deny:{d}", f"命令命中拒绝规则 {d!r}")

        # 3. 批量删除阈值（纯删除命令，路径数超阈值 → 无论在哪都询问）
        paths = parse_delete_command(cmd)
        if paths is not None and len(paths) > self.threshold:
            return Decision("ask", f"command:batch_delete:{len(paths)}",
                            f"批量删除 {len(paths)} 个路径，超过阈值 {self.threshold}，需审批")

        # 4. 命令安全分析
        a = analyze(cmd, self.config.workspace)
        zones_full = full_zones(self.config)
        zones_trust = trusted_zones(self.config)

        # 主程序目录禁写（v0.2.0）：写/删/执行类命令触及即拒；读放行。
        # project 显式豁免（v0.2.1 回归修复）：项目建在程序目录下时其内部操作走信任区逻辑。
        prog = program_dir_norm()
        proj_norm = os.path.normcase(os.path.realpath(str(self.config.project)))
        exempt_program_check = proj_norm != prog   # project == 程序目录时不豁免
        if a.kind != "read_only":
            for p in a.paths:
                pn = os.path.normcase(os.path.realpath(str(p)))
                if pn == prog or pn.startswith(prog + os.sep):
                    if exempt_program_check and (pn == proj_norm
                                                 or pn.startswith(proj_norm + os.sep)):
                        continue   # project 子目录内 → 豁免
                    return Decision("deny", "command:deny_program_dir",
                                    "命令触及主程序目录（禁止写入）")

        if a.kind == "read_only":
            # 读/搜索免询问；但路径命中 deny 表仍硬拦（防数据外泄）
            for p in a.paths:
                if in_zones(p, self._deny_paths):
                    return Decision("deny", "command:deny_path",
                                    f"命令读取路径在拒绝清单：{p}")
            return Decision("allow", "command:read_only",
                            "只读/搜索类命令，默认放行")

        # 盘符根目录 / 系统目录：写/删类命令**一律拒绝**（v0.3.1 用户要求重点审查）；
        # 只读命令（dir D:// 等）不受影响。deny 表已覆盖多数系统目录，这里针对盘根
        # 与漏网系统路径再兜一层，并给出高危标记。
        if a.kind in ("delete", "file_write"):
            from axon.sandbox.path_policy import (is_fs_root, is_under_fs_root,
                                                    system_dir_names)
            sys_names = system_dir_names()
            for p_ in a.paths:
                if is_fs_root(p_):
                    return Decision("deny", "command:fs_root",
                                    f"命令触及盘符根目录（禁止{a.kind == 'delete' and '删除' or '写入'}）：{p_}",
                                    severity="high")
                if sys_names:
                    parts = {x.lower() for x in Path(p_).parts}
                    if parts & {n.lower() for n in sys_names}:
                        return Decision("deny", "command:system_path",
                                        f"命令触及系统目录：{p_}", severity="high")
            for p_ in a.paths:
                if is_under_fs_root(p_):
                    return Decision("ask", "command:fs_root_child",
                                    f"命令直接操作盘符根目录下的路径（高危）：{p_}",
                                    severity="high")

        if a.kind == "delete":
            if a.delete_count == -1:
                return Decision("ask", "command:delete_unbounded",
                                "删除目标无法静态判定（变量/遍历/递归），需审批")
            if a.delete_count is not None and a.delete_count > self.threshold:
                return Decision("ask", f"command:batch_delete:{a.delete_count}",
                                f"批量删除约 {a.delete_count} 个文件，超过阈值 "
                                f"{self.threshold}，需审批")
            if a.paths and all(in_zones(p, zones_trust) for p in a.paths):
                return Decision("allow", "command:zone_delete",
                                "信任区内删除（移回收站/留痕）")
            return Decision("ask", "command:delete_outside",
                            "删除信任区外的路径，需审批")

        if a.kind == "file_write":
            # 解释器代码内的写操作（无删除/无 shell 逃逸，cwd=工作空间）→ 放行
            if a.interp_only_write and not a.paths:
                return Decision("allow", "command:interp_safe",
                                "解释器脚本内的文件操作（无危险信号，cwd=工作空间）")
            if a.paths and all(in_zones(p, zones_full) for p in a.paths):
                return Decision("allow", "command:workspace_full",
                                "工作空间内文件操作（全权限区）")
            if a.paths and all(in_zones(p, zones_trust) for p in a.paths):
                return Decision("allow", "command:zone_write",
                                "项目/信任区内文件操作")
            return Decision("ask", "command:write_outside",
                            "写信任区外的路径，需审批")

        # system_mutate / unknown（效果不透明）
        if a.paths and all(in_zones(p, zones_full) for p in a.paths):
            return Decision("allow", "command:workspace_full",
                            "工作空间内执行（全权限区，路径证据充分）")
        if a.paths and all(in_zones(p, zones_trust) for p in a.paths) \
                and a.kind == "unknown":
            return Decision("allow", "command:zone_exec",
                            "信任区内执行（路径证据充分）")

        # 5. 显式配置兜底
        al = _match_any(cmd, self.rules.allow)
        if al:
            return Decision("allow", f"command:allow:{al}", f"命令命中允许规则 {al!r}")
        k = _match_any(cmd, self.rules.ask)
        if k:
            return Decision("ask", f"command:ask:{k}", f"命令命中询问规则 {k!r}")

        # 6. fail-closed
        return Decision("ask", "command:default_ask",
                        f"命令未识别为安全操作（{a.kind}），默认询问（fail-closed）")

    def post(self, action, result, ctx) -> None:
        return None


def _match_any(cmd: str, rules: list[str]) -> str | None:
    for r in rules:
        if fnmatchcase(cmd, r) or fnmatchcase(cmd, r + "*"):
            return r
    return None
