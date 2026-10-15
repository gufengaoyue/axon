"""进度日志（v0.2.3）：每回合结束自动沉淀项目进度到 AXON.md。

规则式、零 token——从本回合的消息与审计记录提取：
  [请求] 用户输入摘要
  [执行] 工具调用链（短名 + 去重 + 失败标记）
  [结果] 验证状态 / token（0 不显示）
  [变更] 文件写入/编辑清单（去重 + 计数 + 相对路径）
  [踩坑] 失败工具与其原因（防后续会话重复踩坑，v0.2.3）
  [产物] / [清理]（v0.2.2）

与模型手写的「长期记忆」（memory_write）和「项目记忆」（project_note 会话总结）分区共存；
`memory.auto_journal` 开关控制。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_MCP_PREFIX = "mcp__axon__"


def short_tool_name(name: str) -> str:
    return name[len(_MCP_PREFIX):] if name.startswith(_MCP_PREFIX) else name


def rel_path(path: str, workspace=None, project=None) -> str:
    """尽量显示相对路径（工作空间/项目），否则原名（截断）。"""
    try:
        p = Path(path)
        for base in (workspace, project):
            if base is None:
                continue
            try:
                return str(p.relative_to(Path(base)))
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    s = " ".join(str(path).split())
    return s if len(s) <= 80 else "..." + s[-77:]


class Journal:
    def __init__(self, memory_store, config=None):
        self.memory = memory_store
        self.config = config

    def record_turn(self, session, turn_result, messages: list, audit_rows: list,
                    cleaned: list[str] | None = None,
                    archived: list | None = None) -> str:
        """生成本回合进度条目并追加；返回条目文本。"""
        start = 0
        for i, m in enumerate(messages):
            if m.role == "user":
                start = i
        turn_msgs = messages[start:]

        # ---- 请求 ----
        user_input = ""
        for m in turn_msgs:
            if m.role == "user" and m.content:
                text = " ".join(str(m.content).split())
                if (text.startswith("<resume_context>")
                        or text.startswith("<verification")
                        or text.startswith("<interrupted_task>")
                        or "请为本次会话沉淀" in text[:200]):
                    continue        # 系统内部消息不算用户请求（v0.3.0）
                user_input = text
                break
        if len(user_input) > 100:
            user_input = user_input[:100] + "…"

        # ---- 执行（短名 + 去重 + 失败标记）与踩坑 ----
        ops: list[str] = []
        failures: list[str] = []
        pending_name = ""
        from axon.agent.msgmeta import unpack_meta
        for m in turn_msgs:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    pending_name = short_tool_name(tc.name)
                    if pending_name not in ops:
                        ops.append(pending_name)
            elif m.role == "tool":
                _, meta = unpack_meta(m.content)
                et = (meta or {}).get("error_type")
                if et:
                    name = short_tool_name(m.name or pending_name)
                    hint = " ".join(str(m.content or "").split())[:120]
                    failures.append(f"{name}: {hint}")
        ops_line = " · ".join(ops) if ops else "(无工具调用)"

        # ---- 变更（去重 + 计数 + 相对路径） ----
        ws = getattr(self.config, "workspace", None) if self.config else None
        proj = getattr(self.config, "project", None) if self.config else None
        seen: dict[str, int] = {}
        for r in audit_rows:
            if r["kind"] in ("fs_write", "fs_edit") and r["result"] == "ok" \
                    and r["verdict"] in ("allow", "ask_approved"):
                key = rel_path(r["target"], ws, proj)
                seen[key] = seen.get(key, 0) + 1
        if seen:
            items = [f"{k}×{v}" if v > 1 else k for k, v in list(seen.items())[:6]]
            change_line = " · ".join(items)
            if len(seen) > 6:
                change_line += f" …共 {len(seen)} 个文件"
        else:
            change_line = "无"

        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
        token_s = f" · token {turn_result.tokens_used}" if turn_result.tokens_used else ""
        entry = (
            f"### {ts} · 回合 {turn_result.turns_used} · {turn_result.stop_reason}\n"
            f"- 请求: {user_input or '(空)'}\n"
            f"- 执行: {ops_line}\n"
            f"- 结果: {turn_result.stop_reason}{token_s}\n"
            f"- 变更: {change_line}"
        )
        products = getattr(turn_result, "products", None) or []
        if products:
            items = " · ".join(rel_path(p["path"], ws, proj) for p in products[:6])
            entry += f"\n- 产物: {items}"
        if archived:
            entry += ("\n- 归档: " + " · ".join(rel_path(d, ws, proj)
                                               for _s, d in archived[:5]))
        acc = getattr(turn_result, "acceptance", ()) or ()
        if acc:
            _ok, acc_lines = acc
            flags = [ln for ln in acc_lines if ln.startswith(("⚠", "ℹ"))]
            entry += "\n- 自检: " + ("通过" if _ok and not flags else "；".join(acc_lines[:3]))
        if failures:
            entry += "\n- 踩坑: " + " ｜ ".join(failures[:4])
        if cleaned:
            entry += f"\n- 清理: {len(cleaned)} 个临时文件"
        self.memory.append_journal(entry)
        return entry
