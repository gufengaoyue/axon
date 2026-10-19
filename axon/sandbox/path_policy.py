"""路径策略：三态 + 最长前缀 + realpath（symlink 逃逸检测）+ 内置 deny 表。

语义（v0.1.4，用户审定）：
- 隐式信任区 = workspace + project（用户声明的工作上下文）→ allow
- 读操作（fs_read）：无规则命中 → 默认放行（绿档，不问）；deny 表仍硬拦
- 写/改/删：信任区 allow；越界 ask（fail-closed）；deny 表硬拦
- 显式配置规则（allow/ask/deny）永远优先于上述默认——用户配了就算数
- 同长规则优先级 deny > ask > allow；特异性 = 路径段数，越长越优先
"""
from __future__ import annotations

import os
from pathlib import Path

from axon.sandbox.gateway import Action, Decision

_WINDOWS_DENY = [
    "C:/Windows", "C:/Program Files", "C:/Program Files (x86)",
]
_POSIX_DENY = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/root"]


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def is_under_fs_root(path) -> bool:
    """是否**直接位于盘符根目录下**（`D://x.txt`）——v0.3.1 高危审查对象。

    与 `is_fs_root` 区分：盘根本身（`D://`）写/删=直接拒绝；
    盘根下的直接子项=高危但允许用户确认（有人确实想在根下放文件）。
    """
    try:
        p = Path(str(path))
        if not p.is_absolute():
            return False
        return p.parent.parent == p.parent and p.parent != p
    except (OSError, ValueError):
        return False


def is_fs_root(path) -> bool:
    """是否盘符根目录（`D://`、`/`）——v0.3.1 用户要求重点审查的对象。"""
    try:
        p = Path(str(path))
        if not p.is_absolute():
            return False
        return p.parent == p
    except (OSError, ValueError):
        return False


def system_dir_names() -> list[str]:
    """需要硬拦的系统目录名（Windows；与盘根一起构成"重点审查"名单）。"""
    if os.name != "nt":
        return []
    return ["Windows", "Program Files", "Program Files (x86)", "ProgramData",
            "$Recycle.Bin", "Recovery", "System Volume Information", "PerfLogs",
            "MSOCache", "Boot", "EFI"]


def default_deny_paths() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    out.extend(Path(p) for p in (_WINDOWS_DENY if os.name == "nt" else _POSIX_DENY))
    out.append(home)                 # 用户根目录默认拒绝（需要就配置开子路径）
    out.append(home / ".ssh")        # 密钥目录
    if os.name == "nt":
        out.append(home / "AppData" / "Roaming" / "Microsoft" / "Credentials")
        # 各盘的系统目录（v0.3.1：盘根/系统目录重点审查）
        for drive in ("C:", "D:"):
            for name in system_dir_names():
                out.append(Path(f"{drive}\\{name}"))
        out.append(Path(os.environ.get("SystemRoot", "C:////Windows")))
        out.append(Path(os.environ.get("ProgramData", "C:////ProgramData")))
    return out


def resolve_target(target: str, workspace: Path) -> Path:
    """相对路径 → workspace 下；随后 realpath 解析（symlink 逃逸在此现形）。"""
    p = Path(target)
    if not p.is_absolute():
        p = workspace / p
    return Path(os.path.realpath(str(p)))


# ---------------------------------------------------------------- 信任区（v0.1.5）

def full_zones(config) -> list[Path]:
    """全权限区：工作空间 + 程序级临时目录 + 程序级共享 venv（智能体可自由处理）。

    v0.4.4：把 `program_root()/venv` 纳入信任区——依赖装在共享 venv 里属正常操作，
    不应每次 `pip install` 都打断用户。**只放 venv**，不放整个程序目录（`sessions.db`
    等仍不被 agent 触碰）。
    """
    from axon.config import program_root
    root = program_root()
    zones = [config.workspace, root / "tmp", root / "venv"]
    return [Path(os.path.realpath(str(z))) for z in zones]


def trusted_zones(config) -> list[Path]:
    """信任区（免确认读写）：全权限区 + 项目目录。"""
    return full_zones(config) + [Path(os.path.realpath(str(config.project)))]


def in_zones(path, zones) -> bool:
    # v0.4.29：统一 normcase——旧实现与 PathPolicy 的 _norm 口径不一致，
    # Windows 上对"不存在路径"（写判定）可能一边 deny 命中、一边漏过
    p = os.path.normcase(os.path.realpath(str(path)))
    for z in zones:
        zn = os.path.normcase(str(z))
        if p == zn or p.startswith(zn + os.sep):
            return True
    return False


def program_dir_norm() -> str:
    """主程序目录（归一化）——v0.2.0 绝对禁写区。"""
    from axon.config import program_dir
    return os.path.normcase(os.path.realpath(str(program_dir())))


_KIND_CN = {"fs_write": "写入", "fs_edit": "修改", "fs_delete": "删除",
            "fs_read": "读取"}


def _kind_cn(kind: str) -> str:
    return _KIND_CN.get(kind, kind)


class PathPolicyHook:
    name = "PathPolicy"

    def __init__(self, config):
        self.config = config
        self.workspace = config.workspace
        self.project = config.project
        self.read_auto_allow = getattr(config.sandbox, "read_auto_allow", True)
        # 内置 deny 表（代码级，纵深防御，不可被配置删除）
        self._deny = default_deny_paths()
        # 配置规则
        self._rules = list(config.sandbox.paths)
        # 隐式信任区：workspace + project
        self._zones = [(_norm(self.workspace), "workspace"),
                       (_norm(self.project), "project")]
        # v0.4.21：系统级长期记忆文件为**单文件信任区**（仅此文件，不含其父目录）
        try:
            lt = config.memory.long_term_path()
            self._zones.append((_norm(lt), "global_memory"))
        except Exception:                       # noqa: BLE001
            pass

    def pre(self, action: Action, ctx) -> Decision | None:
        if not action.kind.startswith("fs_"):
            return None
        target = resolve_target(action.target, self.workspace)
        target_norm = os.path.normcase(str(target))

        # 主程序目录：禁写（v0.2.0 用户指令"拒绝任意文件的写入"）。
        # **project 显式豁免**（v0.2.1 回归修复）：用户的项目即使建在程序目录下
        # （如 <程序目录>/Project1），其内部读写走正常 project 信任区逻辑——
        # 禁写保护的是"程序自身的文件"（axon 包/config.yml/README 等），
        # 不是用户在程序目录旁建的数据目录。
        # 读放行：agent 查看自身源码是合理行为，且不构成数据外泄。
        proj_norm = os.path.normcase(os.path.realpath(str(self.project)))
        prog = program_dir_norm()
        in_project = (target_norm == proj_norm or target_norm.startswith(proj_norm + os.sep))
        # 豁免仅在 project 是程序目录的**真子目录**时成立；project == 程序目录
        # 本身（"把安装目录当项目用"）不豁免——那是用户明确要拒的极端用法
        exempt = in_project and proj_norm != prog
        if not exempt and (target_norm == prog or target_norm.startswith(prog + os.sep)):
            if action.kind != "fs_read":
                return Decision("deny", "path:program_dir",
                                "主程序目录禁止写入（程序与数据分离）")
            return Decision("allow", "path:program_dir_read", "主程序目录允许读取")

        candidates: list[tuple[int, int, str, str]] = []  # (specificity, rank, mode, rule)
        # rank: deny=2 > ask=1 > allow=0（同长时）
        rank = {"allow": 0, "ask": 1, "deny": 2}

        # 盘符根目录：**写/改/删一律拒绝**，读放行（v0.3.1 用户要求重点审查）。
        # 盘根不在 deny 表（否则 `dir D://` 这类无害读会被拦），故单独判定。
        if action.kind in ("fs_write", "fs_edit", "fs_delete") \
                and is_fs_root(target_norm):
            return Decision("deny", "path:fs_root",
                            f"盘符根目录禁止{_kind_cn(action.kind)}：{target}",
                            severity="high")

        # 盘根下的直接子项：高危询问（不直接拒，但必须用户点头 + 面板 ⚠ 标记）
        if action.kind in ("fs_write", "fs_edit", "fs_delete") \
                and is_under_fs_root(target_norm):
            return Decision("ask", "path:fs_root_child",
                            f"目标直接位于盘符根目录下（高危）：{target}",
                            severity="high")

        for zone_norm, zone_name in self._zones:
            if target_norm == zone_norm or target_norm.startswith(zone_norm + os.sep):
                candidates.append((len(Path(zone_norm).parts), 0, "allow",
                                   f"path:zone:{zone_name}"))

        for d in self._deny:
            dn = _norm(d)
            if target_norm == dn or target_norm.startswith(dn + os.sep):
                candidates.append((len(Path(dn).parts), 2, "deny", f"path:deny:{dn}"))

        for r in self._rules:
            rn = _norm(r.path)
            if target_norm == rn or target_norm.startswith(rn + os.sep):
                candidates.append((len(Path(rn).parts), rank[r.mode], r.mode,
                                   f"path:{r.mode}:{rn}"))

        if not candidates:
            # 无任何规则命中：读 → 默认放行（绿档）；写/改/删 → 越界询问（fail-closed）
            if action.kind == "fs_read" and self.read_auto_allow:
                return Decision("allow", "path:read_auto",
                                "读操作默认放行（绿档，无规则命中）")
            return Decision("ask", "path:unmatched", f"路径越出已配置范围：{target}")
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        spec, _, mode, rule = candidates[0]
        return Decision(mode, rule, f"路径策略命中 {rule}")

    def post(self, action, result, ctx) -> None:
        return None
