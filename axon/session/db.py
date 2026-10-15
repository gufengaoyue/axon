"""SQLite 会话存储（设计 §4.4）：WAL 单写、四表 schema、SessionStore。

表：
  sessions          会话元信息（按 project_dir 区分项目）
  messages          工作消息集（压缩时整体重写 + compaction_events 记账）
  compaction_events 压缩/清理事件账本（warn/force、before/after tokens）
  audit_log         全链路审计（sandbox/audit.py 读写）
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from axon.agent.tokenizer import estimate_messages_tokens
from axon.providers.base import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_dir TEXT NOT NULL,
  title TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  sdk_session_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  turn INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_calls TEXT,
  tool_call_id TEXT,
  name TEXT,
  tokens_est INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS compaction_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  at_turn INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  before_tokens INTEGER NOT NULL,
  after_tokens INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_id TEXT,
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT,
  verdict TEXT NOT NULL,
  rule TEXT NOT NULL,
  result TEXT,
  created_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_INTEGRITY_CHECKED: set = set()


def _warn_if_damaged(con: sqlite3.Connection, db_path) -> None:
    """首次连接做一次 quick_check；损坏时给出**可操作提示**而不是抛栈（v0.4.3）。"""
    key = str(db_path)
    if key in _INTEGRITY_CHECKED:
        return
    _INTEGRITY_CHECKED.add(key)
    from axon.repair import check_integrity
    healthy, detail = check_integrity(con)
    if healthy:
        return
    import sys as _sys
    print("[警告] 会话数据库损坏，本次将以只读降级运行：", file=_sys.stderr)
    print(f"       文件: {db_path}", file=_sys.stderr)
    print(f"       诊断: {detail[:120]}", file=_sys.stderr)
    print("       修复: 关闭所有 Axon 窗口后运行  python -m axon --repair-db"
          "（会自动备份坏库并尽力重建）", file=_sys.stderr)
    try:                                   # 只读降级：避免继续写坏库
        con.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _warn_if_damaged(conn, db_path)        # v0.4.3：首次连接做完整性自检
    return conn


@dataclass
class Session:
    id: str
    project_dir: str
    title: str
    provider: str
    model: str
    status: str
    created_at: str
    updated_at: str


def _row_to_session(r: sqlite3.Row) -> Session:
    return Session(id=r["id"], project_dir=r["project_dir"], title=r["title"] or "",
                   provider=r["provider"], model=r["model"], status=r["status"],
                   created_at=r["created_at"], updated_at=r["updated_at"])


def _row_to_message(r: sqlite3.Row) -> Message:
    import json
    tcs = []
    if r["tool_calls"]:
        from axon.providers.base import ToolCall
        for tc in json.loads(r["tool_calls"]):
            tcs.append(ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]))
    return Message(role=r["role"], content=r["content"], tool_calls=tcs,
                   tool_call_id=r["tool_call_id"], name=r["name"], turn=r["turn"])


class SessionStore:
    """跨轮持久化与恢复；单写者（CLI 单进程），busy_timeout 兜底并发。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = __import__("threading").Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._ensure_columns()
            self.conn.commit()

    def _ensure_columns(self) -> None:
        """轻量迁移：老库补列（v0.1.1 新增 sdk_session_id）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "sdk_session_id" not in cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN sdk_session_id TEXT")

    # ---- 会话 ----
    def create_session(self, project_dir: str, provider: str, model: str,
                       title: str = "") -> str:
        sid = uuid.uuid4().hex
        now = _utcnow()
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions (id, project_dir, title, provider, model, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, str(project_dir), title, provider, model, "active", now, now))
            self.conn.commit()
        return sid

    def get_session(self, session_id: str) -> Session | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_session(r) if r else None

    def latest_session(self, project_dir: str) -> Session | None:
        r = self.conn.execute(
            "SELECT * FROM sessions WHERE project_dir=? ORDER BY updated_at DESC LIMIT 1",
            (str(project_dir),)).fetchone()
        return _row_to_session(r) if r else None

    def list_sessions_all(self, limit: int = 50) -> list[dict]:
        """跨项目会话索引（v0.2.0，参考 codebuddy -c）：含项目名/标题/消息数。"""
        rows = self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)"
            " AS msg_count FROM sessions s ORDER BY s.updated_at DESC LIMIT ?",
            (limit,)).fetchall()
        out = []
        for r in rows:
            sess = _row_to_session(r)
            out.append({
                "session": sess,
                "project_name": Path(sess.project_dir).name or sess.project_dir,
                "title": sess.title,
                "msg_count": r["msg_count"],
            })
        return out

    def set_title(self, session_id: str, title: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE sessions SET title=? WHERE id=?",
                              (title[:60], session_id))
            self.conn.commit()

    def _auto_title(self, session_id: str, content: str) -> None:
        """首条 user 消息 → 自动填充会话标题（取前 40 字）。"""
        row = self.conn.execute("SELECT title FROM sessions WHERE id=?",
                                (session_id,)).fetchone()
        if row is not None and not (row["title"] or "").strip():
            first = content.strip().splitlines()[0] if content.strip() else ""
            if first:
                self.set_title(session_id, first[:40])

    def set_status(self, session_id: str, status: str) -> None:
        """标记会话状态：active（运行中）/ ended（正常退出）。v0.2.9"""
        with self._lock:
            self.conn.execute("UPDATE sessions SET status=? WHERE id=?",
                              (status, session_id))
            self.conn.commit()

    def list_stale_sessions(self, project_dir: str, older_than_iso: str) -> list[dict]:
        """异常结束的会话：status 仍为 active 且久未更新（窗口被直接关闭/崩溃）。

        v0.2.9：正常退出会把 status 置为 ended；因此残留 active 即"没走收尾流程"，
        可据此在下次启动时补沉淀项目记忆（用户问："直接关窗口还会沉淀吗？"）。
        """
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE project_dir=? AND status='active'"
            " AND updated_at < ? ORDER BY updated_at DESC",
            (str(project_dir), older_than_iso)).fetchall()
        return [_row_to_session(r) for r in rows]

    def list_projects(self) -> list[dict]:
        """项目清单（按 project_dir 聚合；v0.2.7 /delproj 用）。

        返回 [{project_dir, name, sid_prefix, sessions, messages, last_updated}]，
        sid_prefix = 该项目**最近会话**的 id 前 8 位（可当作 project_id 使用）。
        """
        rows = self.conn.execute(
            "SELECT s.project_dir AS pd, COUNT(DISTINCT s.id) AS n,"
            " COUNT(m.id) AS msgs, MAX(s.updated_at) AS last_up,"
            " (SELECT id FROM sessions s2 WHERE s2.project_dir = s.project_dir"
            "  ORDER BY updated_at DESC LIMIT 1) AS newest"
            " FROM sessions s LEFT JOIN messages m ON m.session_id = s.id"
            " GROUP BY s.project_dir ORDER BY last_up DESC").fetchall()
        out = []
        for r in rows:
            pd = r["pd"]
            out.append({
                "project_dir": pd,
                "name": Path(pd).name or pd,
                "sid_prefix": (r["newest"] or "")[:8],
                "sessions": r["n"],
                "messages": r["msgs"],
                "last_updated": r["last_up"],
            })
        return out

    def delete_project_data(self, project_dir: str) -> dict:
        """删除某项目的全部会话数据（sessions/messages/compaction_events/audit_log）。

        只动数据库记录，**不触碰磁盘上的项目文件**（文件删除由调用方另行确认后走回收站）。
        返回删除计数。
        """
        with self._lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM sessions WHERE project_dir=?", (str(project_dir),)).fetchall()]
            if not ids:
                return {"sessions": 0, "messages": 0, "events": 0, "audit": 0}
            q = ",".join("?" * len(ids))
            n_msg = self.conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE session_id IN ({q})", ids).fetchone()[0]
            n_ev = self.conn.execute(
                f"SELECT COUNT(*) FROM compaction_events WHERE session_id IN ({q})",
                ids).fetchone()[0]
            n_au = self.conn.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE session_id IN ({q})", ids).fetchone()[0]
            self.conn.execute(f"DELETE FROM messages WHERE session_id IN ({q})", ids)
            self.conn.execute(f"DELETE FROM compaction_events WHERE session_id IN ({q})", ids)
            self.conn.execute(f"DELETE FROM audit_log WHERE session_id IN ({q})", ids)
            self.conn.execute(f"DELETE FROM sessions WHERE id IN ({q})", ids)
            self.conn.commit()
            return {"sessions": len(ids), "messages": n_msg, "events": n_ev,
                    "audit": n_au}

    def list_sessions(self, project_dir: str) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE project_dir=? ORDER BY updated_at DESC",
            (str(project_dir),)).fetchall()
        return [_row_to_session(r) for r in rows]

    def touch_session(self, session_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                              (_utcnow(), session_id))
            self.conn.commit()

    # ---- SDK 会话续接（codebuddy 路） ----
    def set_sdk_session_id(self, session_id: str, sdk_session_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE sessions SET sdk_session_id=? WHERE id=?",
                              (sdk_session_id, session_id))
            self.conn.commit()

    def get_sdk_session_id(self, session_id: str) -> str | None:
        r = self.conn.execute("SELECT sdk_session_id FROM sessions WHERE id=?",
                              (session_id,)).fetchone()
        return r["sdk_session_id"] if r else None

    # ---- 消息 ----
    def append_message(self, session_id: str, msg: Message) -> Message:
        turn = self.next_turn(session_id)
        msg.turn = turn
        import json
        tcs = json.dumps([{"id": t.id, "name": t.name, "arguments": t.arguments}
                          for t in msg.tool_calls], ensure_ascii=False) if msg.tool_calls else None
        msg.tokens_est = estimate_messages_tokens([msg])
        if msg.role == "user" and msg.content:
            self._auto_title(session_id, msg.content)
        with self._lock:
            self.conn.execute(
                "INSERT INTO messages (session_id, turn, role, content, tool_calls,"
                " tool_call_id, name, tokens_est, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, turn, msg.role, msg.content, tcs, msg.tool_call_id,
                 msg.name, msg.tokens_est, _utcnow()))
            self.conn.commit()
        return msg

    def load_messages(self, session_id: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [_row_to_message(r) for r in rows]

    def next_turn(self, session_id: str) -> int:
        r = self.conn.execute(
            "SELECT COALESCE(MAX(turn), 0) AS m FROM messages WHERE session_id=?",
            (session_id,)).fetchone()
        return int(r["m"]) + 1

    # ---- 压缩 ----
    def record_compaction(self, event) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO compaction_events (session_id, at_turn, trigger, before_tokens,"
                " after_tokens, summary, created_at) VALUES (?,?,?,?,?,?,?)",
                (event.session_id, event.at_turn, event.trigger, event.before_tokens,
                 event.after_tokens, event.summary, _utcnow()))
            self.conn.commit()

    def compact_history(self, session_id: str, new_messages: list[Message], event) -> None:
        """工作集整体重写 + 事件记账（compaction_events 是账本，消息表是工作集）。"""
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            for m in new_messages:
                self.conn.execute(
                    "INSERT INTO messages (session_id, turn, role, content, tool_calls,"
                    " tool_call_id, name, tokens_est, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session_id, m.turn or 0, m.role, m.content,
                     __import__("json").dumps(
                         [{"id": t.id, "name": t.name, "arguments": t.arguments}
                          for t in m.tool_calls], ensure_ascii=False) if m.tool_calls else None,
                     m.tool_call_id, m.name, m.tokens_est or 0, _utcnow()))
            self.conn.execute(
                "INSERT INTO compaction_events (session_id, at_turn, trigger, before_tokens,"
                " after_tokens, summary, created_at) VALUES (?,?,?,?,?,?,?)",
                (event.session_id, event.at_turn, event.trigger, event.before_tokens,
                 event.after_tokens, event.summary, _utcnow()))
            self.conn.commit()
