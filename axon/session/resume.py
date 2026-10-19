"""会话恢复（设计 §4.4）：重放消息 + 副作用清单重建，不重放执行。

v0.2.0：sessions.db 程序级全局共享，`--resume <id前缀>` 支持**跨项目**恢复
（找到即自动切换项目上下文）；`last` 取全局最近会话。
resume 时向模型注入 <resume_context> 观察消息：中断点 turn + 已完成副作用清单
+ 未配对工具调用状态，防止非幂等 shell 被重复执行、孤立 tool_call 被静默清除。
"""
from __future__ import annotations

from axon.providers.base import Message
from axon.session.db import Session, SessionStore


# 系统内部消息（非用户真实请求）——不能当作"上次任务"
_INTERNAL_MARKERS = (
    "<user_interjection>", "<conversation_summary>",     # v0.4.30 补齐
    "<resume_context>", "<verification", "<tool_name_hint>",
    "请为本次会话沉淀", "沉淀「项目记忆」")


def is_internal_message(text: str) -> bool:
    t = (text or "").lstrip()
    return t.startswith("<interrupted_task>") or any(m in t[:200] for m in _INTERNAL_MARKERS)


def fold_side_effects(side_effects: list[dict], per_kind: int = 4, width: int = 96,
                      max_lines: int = 24) -> list[str]:
    """副作用清单折叠（v0.3.0）：压平成单行、截断、同类聚合，避免 resume 刷屏。

    用户反馈：`[shell] python -c ...` 这类记录动辄几十行把界面淹没。
    """
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in side_effects:
        k = str(e.get("kind", "?"))
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(e)
    lines: list[str] = []
    for k in order:
        items = buckets[k]
        shown = items[-per_kind:]
        for e in shown:
            t = " ".join(str(e.get("target", "")).split())
            if len(t) > width:
                t = t[: width - 3] + "..."
            lines.append(f"  - [{k}] {t} ({e.get('verdict')}, {e.get('result')})")
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(f"  - ...（[{k}] 共 {len(items)} 条，省略较早 {hidden} 条）")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"  - ...（共 {len(side_effects)} 条，"
                                     f"已截断显示；完整记录见 /audit）"]
    return lines


def extract_task_state(messages: list[Message]) -> tuple[str, bool]:
    """从会话消息推断"最近任务描述 + 是否未完成"（v0.2.5）。

    未完成信号：① 存在 <interrupted_task> 标记；② 最近一条用户请求之后
    没有任何 assistant 回复文本（中断在模型回答前）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role != "user" or not m.content:
            continue
        text = str(m.content)
        if text.startswith("<interrupted_task>"):
            req = ""
            for ln in text.splitlines():
                if ln.startswith("original_request:"):
                    req = ln.split(":", 1)[1].strip()
                    break
            return req, True
        if is_internal_message(text):
            continue        # 总结指令/观察消息不是用户任务（v0.3.0 修 last_user_request 污染）
        # 普通用户请求：看其后是否有 assistant 回复文本
        replied = any(messages[j].role == "assistant" and (messages[j].content or "").strip()
                      for j in range(i + 1, len(messages)))
        return text, not replied
    return "", False


def build_resume_message(session: Session, side_effects: list[dict],
                         pending_tool_calls: list[str],
                         last_request: str = "", unfinished: bool = False) -> Message:
    lines = [
        "<resume_context>",
        f"session: {session.id}",
        f"project: {session.project_dir}",
        f"interrupted_at: {session.updated_at}",   # v0.4.29：原字段名 turn 但填的是时间戳
    ]
    if last_request:
        lines.append(f"last_user_request: {last_request[:400]}")
    lines.append("task_status: " + ("未完成（中断/失败后恢复，请从断点续做）"
                                    if unfinished else "上一轮已完成"))
    lines.append("completed_side_effects:")
    folded = fold_side_effects(side_effects)
    lines.extend(folded if folded else ["  (无)"])
    lines.append("pending_tool_calls:")
    if pending_tool_calls:
        for t in pending_tool_calls:
            lines.append(f"  - {t}（调用已发起但结果未知，请勿重复执行，先核对现状）")
    else:
        lines.append("  (无)")
    lines.append("</resume_context>")
    lines.append("以上为恢复上下文：已发生的副作用不会重放；请基于现状继续，不要重复执行未确认的操作。")
    return Message(role="user", content="\n".join(lines))


def find_pending_tool_calls(messages: list[Message]) -> list[str]:
    """找未配对 tool_call（assistant 发起但没有对应 tool 结果）。"""
    answered = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}
    pending: list[str] = []
    for m in messages:
        if m.role == "assistant":
            for tc in m.tool_calls:
                if tc.id not in answered:
                    pending.append(f"{tc.name}({tc.id})")
    return pending


def find_session(store: SessionStore, resume_arg: str | None) -> Session | None:
    """定位会话：None/last → 全局最近；否则全局按 id 前缀匹配（跨项目）。"""
    if not resume_arg or resume_arg == "last":
        rows = store.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        return store.get_session(rows["id"]) if rows else None
    rows = store.conn.execute(
        "SELECT id FROM sessions WHERE id LIKE ? ORDER BY updated_at DESC",
        (f"{resume_arg}%",)).fetchall()
    if not rows:
        return None
    return store.get_session(rows[0]["id"])


def load_for_resume(store: SessionStore, resume_arg: str | None):
    """返回 (session, messages, resume_note)；找不到抛 ValueError（附最近会话列表）。"""
    session = find_session(store, resume_arg)
    if session is None:
        recent = store.list_sessions_all(8)
        hint = "\n".join(
            f"  {s['session'].id[:8]}  {s['project_name']}  "
            f"{(s['title'] or '(无标题)')[:30]}" for s in recent) or "  (无历史会话)"
        raise ValueError(
            f"未找到会话: {resume_arg}（全局搜索）\n最近的会话：\n{hint}\n"
            "提示: /sessions 查看全部；--resume last 恢复最近一个")
    messages = store.load_messages(session.id)
    from axon.sandbox.audit import AuditLog
    audit = AuditLog(store.conn)
    side_effects = audit.turn_side_effects(session.id)  # 恢复时展示本会话全部副作用
    pending = find_pending_tool_calls(messages)
    last_request, unfinished = extract_task_state(messages)
    return session, messages, build_resume_message(session, side_effects, pending,
                                                   last_request, unfinished)
