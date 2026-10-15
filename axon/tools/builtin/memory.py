"""memory_read / memory_write：长期记忆工具（AXON.md 治理）。"""
from __future__ import annotations

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.context import ToolContext
from axon.tools.registry import ToolResult, ToolSpec, tool_result_ok

READ_PARAMETERS = {"type": "object", "properties": {}, "required": []}

WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "remove"],
                   "description": "add=新增条目；remove=按文本精确匹配删除"},
        "text": {"type": "string", "description": "记忆条目文本（remove 时须与已有条目完全一致）"},
        "scope": {"type": "string", "enum": ["project", "global"],
                  "description": "project=仅本项目；global=跨项目", "default": "project"},
        "confidence": {"type": "string", "enum": ["verified", "inferred"],
                       "description": "verified=用户确认/已验证；inferred=推测（禁当事实引用）",
                       "default": "inferred"},
        "source": {"type": "string", "description": "来源：用户明说/任务观察/推断", "default": "任务观察"},
    },
    "required": ["action", "text"],
}


def make_read_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        block = ctx.memory_store.render_block()
        return tool_result_ok(call, block or "(长期记忆为空)",
                              meta={"path": str(ctx.memory_store.path)})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_read", target=str(ctx.memory_store.path),
                      detail={"path": str(ctx.memory_store.path)})

    return ToolSpec(
        name="memory_read",
        description="读取长期记忆（AXON.md）当前内容（受 token 预算约束的注入视图）。",
        parameters=READ_PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.READ,
        parallel_safe=True,
    )


def make_write_spec(ctx: ToolContext) -> ToolSpec:
    def handler(call, args: dict, gw) -> ToolResult:
        from axon.memory.store import MemoryBudgetError, MemoryEntry
        store = ctx.memory_store
        action = args["action"]
        if action == "remove":
            ok = store.remove_entry(args["text"])
            return tool_result_ok(call, "已删除条目" if ok else "未找到匹配条目",
                                  meta={"removed": ok})
        entry = MemoryEntry(
            scope=args.get("scope", "project"),
            confidence=args.get("confidence", "inferred"),
            text=args["text"],
            added_turn=gw.turn,
            source=args.get("source", "任务观察"),
        )
        try:
            store.add_entry(entry)
        except MemoryBudgetError as e:
            return tool_result_err(call, "model",
                                   f"记忆写入被治理拒绝: {e}；请先删除陈旧条目或精炼文本")
        except Exception as e:
            return tool_result_err(call, "model", f"记忆写入失败: {e}")
        return tool_result_ok(
            call, f"已写入记忆 [{entry.scope}/{entry.confidence}]",
            meta={"path": str(store.path), "hash": entry.hash})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_write", target=str(ctx.memory_store.path),
                      detail={"action": args.get("action", "add"),
                              "text_len": len(args.get("text", ""))})

    return ToolSpec(
        name="memory_write",
        description=("写入/删除长期记忆条目。必须标注 scope 与 confidence；"
                     "推测性内容只能标 inferred；超预算会被拒绝并提示整理。"),
        parameters=WRITE_PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.WRITE,
        mutating=True,
        parallel_safe=False,
    )


NOTE_PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["summary", "progress", "decision", "pitfall",
                          "preference", "environment", "next"],
                 "description": ("记录类型：summary=会话总结 | progress=进度 | "
                                 "decision=关键决策 | pitfall=踩过的坑（含 agent 自身错误）| "
                                 "preference=用户偏好 | environment=环境事实 | next=下一步")},
        "text": {"type": "string",
                 "description": "内容（Markdown 要点，简洁；每项一行）"},
    },
    "required": ["kind", "text"],
}


def make_note_spec(ctx) -> ToolSpec:
    """project_note：写「项目记忆」区（v0.2.3）——会话总结/决策/踩坑/偏好/环境/下一步。

    与「长期记忆」（memory_write 条目化治理）分工：本工具面向**成段落的项目上下文**，
    目标是让后续会话快速理解项目内容、逻辑与进度。
    """
    def handler(call, args: dict, gw) -> ToolResult:
        kind = str(args.get("kind", "summary"))
        text = str(args.get("text", "")).strip()
        if not text:
            return tool_result_err(call, "model", "text 不能为空")
        if kind not in ("summary", "progress", "decision", "pitfall",
                        "preference", "environment", "next"):
            return tool_result_err(call, "model", f"kind 非法: {kind}")
        store = ctx.memory_store
        try:
            store.append_project_note(kind, text)
        except Exception as e:  # noqa: BLE001
            return tool_result_err(call, "model", f"项目记忆写入失败: {e}")
        return tool_result_ok(
            call, f"已写入项目记忆 [{kind}]（{len(text)} 字）",
            meta={"path": str(store.path), "kind": kind})

    def to_action(args: dict) -> Action:
        return Action(kind="fs_write", target=str(ctx.memory_store.path),
                      detail={"action": "project_note",
                              "kind": args.get("kind"),
                              "text_len": len(args.get("text", ""))})

    return ToolSpec(
        name="project_note",
        description=("写入「项目记忆」区（会话总结/进度/关键决策/踩过的坑/用户偏好/环境事实/下一步）。"
                     "目标是让后续会话快速理解项目；踩坑记录尤其要写清"
                     "「什么操作 → 什么错误 → 正确做法」。"),
        parameters=NOTE_PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.WRITE,
        mutating=True,
        parallel_safe=False,
    )


GLOBAL_PARAMETERS = {
    "type": "object",
    "properties": {
        "section": {"type": "string",
                    "description": "小节名（如：系统与环境 / 用户 / 偏好 / 工具链与路径 / 其他）；"
                                   "不存在则自动创建"},
        "text": {"type": "string",
                 "description": "一条跨项目通用的稳定事实（一行、简洁）。"
                                "仅当事实**对所有项目都成立**时才写这里；项目专属信息用 project_note"},
    },
    "required": ["text"],
}


def make_global_spec(ctx) -> ToolSpec:
    """global_memory_write：系统级长期记忆（跨项目共享文件，v0.4.21）。"""

    def handler(call, args: dict, gw) -> ToolResult:
        store = getattr(ctx, "global_memory", None)
        if store is None:
            return tool_result_err(call, "model", "长期记忆存储不可用")
        section = str(args.get("section") or "其他")
        text = str(args.get("text") or "")
        # 经沙箱网关（单文件豁免在 PathPolicy 中声明，v0.4.21）
        act = Action(kind="fs_write", target=str(store.path),
                     detail={"action": "global_memory_add", "section": section})
        decided = gw.check(act)
        if not decided.allowed:
            return tool_result_err(call, "sandbox", f"被沙箱拒绝：{decided.reason}")
        ok, msg = store.append(section, text)
        if ok:
            return tool_result_ok(call, f"✅ {msg}",
                                  meta={"path": str(store.path), "section": section})
        return tool_result_err(call, "model", msg)

    def to_action(args: dict) -> Action:
        store = getattr(ctx, "global_memory", None)
        target = str(store.path) if store is not None else "longtermmemory.md"
        return Action(kind="fs_write", target=target,
                      detail={"action": "global_memory_add",
                              "section": args.get("section", "其他")})

    return ToolSpec(
        name="global_memory_write",
        description=("写入**系统级长期记忆**（跨项目共享文件 longtermmemory.md）。"
                     "用于所有项目通用的稳定事实：系统环境、用户信息与偏好、工具链路径等。"
                     "**项目专属**的信息不要写这里（用 project_note / memory_write）。"
                     "同一内容重复写入会被跳过。"),
        parameters=GLOBAL_PARAMETERS,
        handler=handler,
        to_action=to_action,
        risk=RiskClass.WRITE,
        mutating=True,
        parallel_safe=False,
    )
