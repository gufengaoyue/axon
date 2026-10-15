"""审计日志：audit_log 表读写 + CSV/JSON 导出（设计 §4.5/§13#8）。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    """所有沙箱放行/拦截动作、文件操作、命令、网络请求落账。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()

    def record(self, action, verdict: str, rule: str, result: str) -> None:
        detail = dict(action.detail or {})
        detail["_turn"] = action.turn   # 回合口径：V 层规则②按当前回合核对审批
        now = _utcnow()
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit_log (ts, session_id, kind, target, detail, verdict,"
                " rule, result, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    action.session_id,
                    action.kind,
                    action.target,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    verdict,
                    rule,
                    result,
                    now,
                ),
            )
            self.conn.commit()

    def turn_side_effects(self, session_id: str) -> list[dict]:
        """本会话已发生的副作用（mutating 类动作，供 resume/中断时展示）。"""
        rows = self.conn.execute(
            "SELECT ts, kind, target, verdict, rule, result FROM audit_log"
            " WHERE session_id=? AND kind IN ('fs_write','fs_edit','fs_delete','shell','network','mcp')"
            " AND verdict IN ('allow','ask_approved') AND result='ok'"
            " ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {"ts": r[0], "kind": r[1], "target": r[2], "verdict": r[3], "rule": r[4], "result": r[5]}
            for r in rows
        ]

    def rows(self, session_id: str | None = None) -> list[dict]:
        q = ("SELECT id, ts, session_id, kind, target, detail, verdict, rule, result"
             " FROM audit_log")
        params: tuple = ()
        if session_id:
            q += " WHERE session_id=?"
            params = (session_id,)
        q += " ORDER BY id"
        out = []
        for r in self.conn.execute(q, params).fetchall():
            out.append({
                "id": r[0], "ts": r[1], "session_id": r[2], "kind": r[3],
                "target": r[4], "detail": r[5], "verdict": r[6], "rule": r[7], "result": r[8],
            })
        return out

    def export(self, fmt: str, out_path: str, session_id: str | None = None) -> int:
        """导出审计日志；fmt = csv | json。返回导出条数。"""
        data = self.rows(session_id)
        if fmt == "json":
            with io.open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            with io.open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0].keys()) if data else
                                   ["id", "ts", "session_id", "kind", "target", "detail",
                                    "verdict", "rule", "result"])
                w.writeheader()
                w.writerows(data)
        else:
            raise ValueError(f"unsupported export format: {fmt}")
        return len(data)
