"""快照备份 + 回收站（设计 §4.4/§4.5）。

- 修改文件前自动快照到 workspace/.axon/snapshots/<session>/<turn>/<relpath>
- 删除优先 send2trash（跨平台回收站）；失败降级 workspace/.axon/trash/
- v1 无独立 delete 工具：删除经 shell 工具的纯删除命令路由走 move_to_trash
  （设计 §4.2 工具清单 8 个，删除语义由 shell + CommandRule 批量阈值 + 本模块承担）
"""
from __future__ import annotations

import shutil
from pathlib import Path


def snapshot_file(path: Path, snapshots_dir: Path, session_id: str, turn: int,
                  workspace: Path) -> Path | None:
    """修改前快照；返回快照路径（非文件/不存在返回 None）。"""
    try:
        if not path.is_file():
            return None
        try:
            rel = path.resolve().relative_to(workspace.resolve())
            dest = snapshots_dir / session_id / str(turn) / rel
            # v0.4.29：同回合二次编辑同一文件时旧快照才是"修改前原始内容"，
            # 覆盖会丢失；已存在则加序号保留第一份
            if dest.exists():
                i = 2
                base_dest = dest
                while dest.exists():
                    dest = base_dest.with_name(f"{base_dest.stem}-{i}{base_dest.suffix}")
                    i += 1
        except ValueError:
            dest = snapshots_dir / session_id / str(turn) / ("_abs_" + path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return dest
    except OSError:
        return None


def move_to_trash(paths: list[str], trash_dir: Path, workspace: Path) -> list[str]:
    """删除优先回收站；返回实际处理的路径列表。"""
    moved: list[str] = []
    trash_dir.mkdir(parents=True, exist_ok=True)
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        if not p.exists():
            continue
        try:
            import send2trash
            send2trash.send2trash(str(p))
            moved.append(str(p))
        except Exception:
            # 降级：移至 workspace/.axon/trash/（防重名加序号）
            dest = trash_dir / p.name
            n = 1
            while dest.exists():
                dest = trash_dir / f"{p.stem}.{n}{p.suffix}"
                n += 1
            shutil.move(str(p), str(dest))
            moved.append(str(p))
    return moved
