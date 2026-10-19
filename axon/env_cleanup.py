"""回合结束的临时文件清理（v0.2.2 用户审定）。

**判定原则：只清"明确约定为临时"的东西，产物一律不动。**

清理范围：
1. 程序临时目录 `program_root/tmp/**` —— 全清（这是 harness 自己的草稿区）
2. 工作空间内的明确临时模式：`*.tmp` `*.temp` `*.bak` `*.orig` `*.rej`
   `~$*`（Office 锁文件）`*.crdownload`（浏览器半成品）`Thumbs.db` `.DS_Store`
3. 工作空间内的 Python 字节码：`*.pyc` `*.pyo` 与 `__pycache__/`（本轮执行产生）

**明确不动**（视为生成产物 / 用户资产）：
- `workspace/downloads/**`（web_fetch 下载物）
- 任何不符合上述模式的用户文件；`*.log` 也不删（可能是有用的运行记录）
- 工作空间与项目目录之外的一切

安全：清理仅发生在信任区（全权限区）内；审计留痕（env_cleanup 一条）；
由 `tools.cleanup_temp_each_turn` 开关控制（默认 true）。
"""
from __future__ import annotations

import os
from pathlib import Path


def _iter_temp_files(workspace: Path, patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    if not workspace.is_dir():
        return out
    seen: set[str] = set()
    for pat in patterns:
        try:
            for p in workspace.rglob(pat):
                if p.is_file() and str(p) not in seen:
                    seen.add(str(p))
                    out.append(p)
        except OSError:
            continue
    return out


def _iter_pycache_dirs(workspace: Path) -> list[Path]:
    out = []
    try:
        for d in workspace.rglob("__pycache__"):
            if d.is_dir():
                out.append(d)
    except OSError:
        pass
    return out


def cleanup_temp(config, extra_patterns: list[str] | None = None) -> list[str]:
    """清理临时文件，返回被清理路径清单（字符串）。失败静默（不影响主流程）。"""
    from axon.config import program_root

    removed: list[str] = []

    # 1) 程序临时目录全清
    tmp_dir = program_root() / "tmp"
    if tmp_dir.is_dir():
        try:
            for p in sorted(tmp_dir.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                        removed.append(str(p))
                    elif p.is_dir():
                        p.rmdir()          # 只删空目录（保留用户可能放的层级）
                except OSError:
                    continue
        except OSError:
            pass

    # 2) 工作空间内的明确临时模式（附件目录 tmp/attach 明确保护，不清理）
    patterns = list(config.tools.temp_patterns or [])
    if extra_patterns:
        patterns.extend(extra_patterns)
    ws = Path(config.workspace)
    # v0.4.29：downloads/ 也受保护——模块 docstring 承诺"下载物不动"，
    # 旧实现只保护 tmp，下载目录里的 *.bak/~$*/Thumbs.db 会被每回合清掉
    _dl = str(getattr(config.tools, "downloads_dir", "") or "")
    protected = {(ws / "tmp").resolve()}
    if _dl and _dl.lower() not in ("none", "null"):
        protected.add((ws / _dl).resolve())
    for p in _iter_temp_files(ws, patterns):
        try:
            if any(pr in p.resolve().parents or p.resolve() == pr for pr in protected):
                continue                    # 附件/临时/下载目录受保护（v0.2.5 / v0.4.29）
            p.unlink()
            removed.append(str(p))
        except OSError:
            continue

    # 3) Python 字节码与 __pycache__
    for d in _iter_pycache_dirs(ws):
        try:
            for f in d.iterdir():
                if f.is_file() and f.suffix in (".pyc", ".pyo"):
                    f.unlink()
                    removed.append(str(f))
            d.rmdir()
            removed.append(str(d))
        except OSError:
            continue
    for pat in ("*.pyc", "*.pyo"):
        for p in _iter_temp_files(ws, [pat]):
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                continue

    return removed


def is_product(path: str, workspace: Path) -> bool:
    """是否为生成产物（不可清理）——供诊断/测试用。"""
    try:
        p = Path(os.path.realpath(path))
    except OSError:
        return False
    downloads = (workspace / "downloads").resolve()
    try:
        return p == downloads or downloads in p.parents
    except OSError:
        return False
