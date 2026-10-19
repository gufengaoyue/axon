"""数据库自检与修复（v0.4.3，事故驱动）。

背景（用户实测事故）：`sessions.db` 报 `database disk image is malformed`。
根因两条：① 改名迁移时用 `shutil.copy2` 复制**处于 WAL 模式的活动库**，
主库与 `-wal` 不匹配；② 数据目录位于个人文档，可能被云同步/企业 DLP
逐文件回滚（SQLite 三文件不同步 = 必坏）。

本模块提供：
- `check_integrity(con)`：轻量完整性检查（quick_check）
- `repair_database(path, ...)`：**备份 → 用官方 schema 重建 → 逐行搬迁 → 校验**，
  能读多少救多少（沿用本次事故的手工抢救流程，内置为一条命令）
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

TABLES = ("sessions", "messages", "compaction_events", "audit_log")


def check_integrity(con: sqlite3.Connection) -> tuple[bool, str]:
    """快速完整性检查：返回 (是否健康, 说明)。"""
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as e:
        return False, f"{type(e).__name__}: {e}"
    detail = row[0] if row else "unknown"
    return (detail == "ok"), str(detail)


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    """逐行读取，遇到坏页就停（尽量多读）。"""
    out: list[dict] = []
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(f"SELECT * FROM {table}")
        while True:
            try:
                r = cur.fetchone()
            except sqlite3.DatabaseError:
                break
            if r is None:
                break
            out.append(dict(r))
    except sqlite3.DatabaseError:
        pass
    return out


def repair_database(db_path, schema: str, quarantine_dir=None) -> dict:
    """v0.4.29：整体异常防护——"修复工具自己崩掉"与模块定位相悖，
    任何未预期异常都转成 {"ok": False, "note": ...}。"""
    """修复数据库：备份坏库 → 重建 → 搬迁数据 → 校验。

    返回 {ok, backup, repaired, sessions, messages, audit, note}
    """
    db = Path(db_path)
    if not db.exists():
        return {"ok": False, "note": f"数据库不存在: {db}"}
    qdir = Path(quarantine_dir) if quarantine_dir else db.parent / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    # ① 备份原库三件套（含 -wal/-shm，保留证据）
    backup = qdir / f"{db.name}.broken-{ts}"
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, str(backup) + suffix)
    except OSError as e:
        return {"ok": False, "note": f"备份失败（请先关闭所有 Axon 窗口）: {e}"}

    # ② 用官方 schema 建新库
    repaired = qdir / f"{db.name}.repaired-{ts}"
    if repaired.exists():
        repaired.unlink()
    out = sqlite3.connect(str(repaired))
    out.executescript(schema)

    # ③ 从坏库逐表逐行搬迁（孤儿行丢弃）
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    stats: dict[str, int] = {}
    sess_rows = _rows(src, "sessions")
    ok_ids = set()
    for r in sess_rows:
        try:
            out.execute(
                "INSERT OR REPLACE INTO sessions (id, project_dir, title, provider,"
                " model, status, sdk_session_id, created_at, updated_at)"
                " VALUES (:id,:project_dir,:title,:provider,:model,:status,"
                ":sdk_session_id,:created_at,:updated_at)", r)
            ok_ids.add(r["id"])
        except (sqlite3.Error, KeyError):
            continue
    stats["sessions"] = len(ok_ids)
    for table in ("messages", "compaction_events", "audit_log"):
        n = 0
        for r in _rows(src, table):
            if table != "sessions" and r.get("session_id") not in ok_ids:
                continue
            cols = [k for k in r if k != "id"]
            try:
                out.execute(f"INSERT INTO {table} ({','.join(cols)})"
                            f" VALUES ({','.join('?' * len(cols))})",
                            [r[c] for c in cols])
                n += 1
            except sqlite3.Error:
                continue
        stats[table] = n
    src.close()
    out.commit()
    healthy, detail = check_integrity(out)
    out.close()

    if not healthy or stats["sessions"] == 0:
        return {"ok": False, "backup": str(backup), "repaired": str(repaired),
                "note": f"重建后校验未通过（{detail}）；已保留备份与半成品，请人工处理"}

    # ④ 替换（原库已备份）
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                return {"ok": False, "backup": str(backup), "repaired": str(repaired),
                        "note": f"无法替换原库（可能仍被占用，请关闭 Axon 后重试）: {e}"}
    shutil.copy2(str(repaired), str(db))
    return {"ok": True, "backup": str(backup), "repaired": str(repaired), **stats}
