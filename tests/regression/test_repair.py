"""回归：数据库自检与修复（v0.4.3，用户实测事故：database disk image is malformed）。"""
from __future__ import annotations

import sqlite3

from axon.repair import check_integrity, repair_database
from axon.session.db import SCHEMA


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO sessions (id, project_dir, title, provider, model, status,"
                " created_at, updated_at) VALUES ('s1','D:/p','t','openai','m','active',"
                "'2026-01-01','2026-01-01')")
    for t in range(30):
        con.execute("INSERT INTO messages (session_id, turn, role, content, created_at)"
                    " VALUES ('s1',?,'user',?, '2026-01-01')", (t, f"消息 {t}"))
    con.commit()
    con.close()


def test_check_integrity_healthy(tmp_path):
    db = tmp_path / "ok.db"
    _make_db(db)
    con = sqlite3.connect(str(db))
    healthy, detail = check_integrity(con)
    con.close()
    assert healthy and detail == "ok"


def test_repair_recovers_from_corruption(tmp_path):
    """模拟损坏（把中间数据页写成垃圾）→ 修复应尽数救回并保留备份。"""
    db = tmp_path / "broken.db"
    _make_db(db)
    raw = bytearray(db.read_bytes())
    # 破坏若干数据页首页（page_size 默认 4096，跳过第 1 页 header）
    for page in (6, 7, 8):
        start = page * 4096
        if start + 64 <= len(raw):
            raw[start:start + 64] = b"\xde\xad\xbe\xef" * 16
    db.write_bytes(bytes(raw))
    con = sqlite3.connect(str(db))
    try:
        healthy, _ = check_integrity(con)
    finally:
        con.close()
    assert healthy is False                      # 确认已损坏

    q = tmp_path / "quarantine"
    result = repair_database(db, SCHEMA, quarantine_dir=q)
    assert result["ok"] is True, result
    assert result["sessions"] >= 1
    assert result["messages"] >= 1
    assert result["backup"] and (tmp_path / "quarantine").exists()
    # 修复后的库可用且完整
    con = sqlite3.connect(str(db))
    healthy, detail = check_integrity(con)
    n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    assert healthy and n == result["messages"]


def test_repair_missing_db(tmp_path):
    r = repair_database(tmp_path / "nope.db", SCHEMA)
    assert r["ok"] is False and "不存在" in r["note"]


def test_run_repair_mode_healthy(cfg, capsys, monkeypatch, tmp_path):
    """健康库：--repair-db 报"无需修复"且不改动任何东西。"""
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    from axon.cli import run_repair_mode
    from axon.config import load_config
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(f"project: {tmp_path}\nmodel:/n  active_provider: openai\n",
                        encoding="utf-8")
    c2 = load_config(cfg_path)
    c2.db_path.parent.mkdir(parents=True, exist_ok=True)
    _make_db(c2.db_path)
    before = c2.db_path.stat().st_size
    rc = run_repair_mode(c2, None)
    out = capsys.readouterr().out
    assert rc == 0 and "无需修复" in out
    assert c2.db_path.stat().st_size == before


def test_run_repair_mode_damaged(cfg, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home2"))
    from axon.cli import run_repair_mode
    from axon.config import load_config
    cfg_path = tmp_path / "c2.yml"
    cfg_path.write_text(f"project: {tmp_path}\nmodel:/n  active_provider: openai\n",
                        encoding="utf-8")
    c2 = load_config(cfg_path)
    c2.db_path.parent.mkdir(parents=True, exist_ok=True)
    _make_db(c2.db_path)
    raw = bytearray(c2.db_path.read_bytes())
    raw[6 * 4096:6 * 4096 + 64] = b"\xff" * 64
    c2.db_path.write_bytes(bytes(raw))
    rc = run_repair_mode(c2, None)
    out = capsys.readouterr().out
    assert rc == 0 and "修复完成" in out
    con = sqlite3.connect(str(c2.db_path))
    assert check_integrity(con)[0] is True
    con.close()


def test_is_cloud_synced_detection():
    from axon.config import is_cloud_synced
    assert is_cloud_synced(r"C:/Users/x/OneDrive/proj") is True
    assert is_cloud_synced(r"C:/Users/x/Dropbox/a/b") is True
    assert is_cloud_synced(r"C:/Users/x/AppData/Local/Axon") is False


def test_migrate_uses_sqlite_snapshot(tmp_path, monkeypatch):
    """迁移必须用 SQLite **一致性快照**（旧实现复制活动库 → 库损坏）。"""
    from axon import config as cfgmod
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home" / "Axon"))
    legacy = tmp_path / "olddoc" / ".axon"
    legacy.mkdir(parents=True)
    _make_db(legacy / "sessions.db")
    (legacy / "sessions.db-wal").write_bytes(b"")     # WAL 存在也不该影响一致性
    monkeypatch.setattr(cfgmod, "legacy_roots", lambda: [legacy])
    moved = cfgmod.migrate_legacy_root()
    assert moved, "应迁移 sessions.db"
    target = cfgmod.program_root() / "sessions.db"
    assert target.exists()
    con = sqlite3.connect(str(target))
    assert check_integrity(con)[0] is True
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 30
    con.close()


def test_snapshot_refuses_damaged_source(tmp_path):
    """源库损坏 → 快照失败且不留半成品（避免把坏库带进新位置）。"""
    from axon.config import _snapshot_sqlite
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database at all" * 10)
    dst = tmp_path / "out.db"
    assert _snapshot_sqlite(bad, dst) is False
    assert not dst.exists()
